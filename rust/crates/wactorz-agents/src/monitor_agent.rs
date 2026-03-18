//! System health monitor agent.
//!
//! [`MonitorAgent`] polls all registered actors every [`POLL_INTERVAL_SECS`]
//! seconds.  If an actor's last heartbeat exceeds [`TIMEOUT_SECS`], an alert
//! is broadcast to the MQTT `system/health` topic.
//!
//! Like [`MainActor`], the monitor is **protected** from external kill commands.

use anyhow::Result;
use async_trait::async_trait;
use std::sync::Arc;
use tokio::sync::mpsc;

use wactorz_core::{Actor, ActorConfig, ActorMetrics, ActorState, ActorSystem, EventPublisher, Message};

/// How often the monitor sweeps all actors (seconds).
pub const POLL_INTERVAL_SECS: u64 = 15;

/// How many seconds without a heartbeat before an alert is raised (seconds).
pub const TIMEOUT_SECS: u64 = 60;

/// Health status of a single actor at a given point in time.
#[derive(Debug, Clone)]
pub struct ActorHealthReport {
    pub actor_id: String,
    pub actor_name: String,
    pub state: ActorState,
    pub last_heartbeat_secs_ago: u64,
    pub is_stale: bool,
}

/// The health monitor actor.
pub struct MonitorAgent {
    config: ActorConfig,
    system: ActorSystem,
    state: ActorState,
    metrics: Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher: Option<EventPublisher>,
}

impl MonitorAgent {
    pub fn new(config: ActorConfig, system: ActorSystem) -> Self {
        let protected_config = ActorConfig {
            protected: true,
            ..config.clone()
        };
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config: protected_config,
            system,
            state: ActorState::Initializing,
            metrics: Arc::new(ActorMetrics::new()),
            mailbox_tx: tx,
            mailbox_rx: Some(rx),
            publisher: None,
        }
    }

    /// Attach an EventPublisher for MQTT output.
    pub fn with_publisher(mut self, p: EventPublisher) -> Self {
        self.publisher = Some(p);
        self
    }

    /// Sweep all registered actors and return health reports.
    pub async fn poll_health(&self) -> Vec<ActorHealthReport> {
        use std::sync::atomic::Ordering;
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        self.system
            .registry
            .list()
            .await
            .into_iter()
            .filter(|e| e.name != self.config.name)
            .map(|e| {
                let last = e.metrics.last_message_at.load(Ordering::Relaxed);
                let age = if last == 0 { 0 } else { now.saturating_sub(last) };
                ActorHealthReport {
                    actor_id: e.id.clone(),
                    actor_name: e.name,
                    state: e.state,
                    last_heartbeat_secs_ago: age,
                    is_stale: age > TIMEOUT_SECS && last != 0,
                }
            })
            .collect()
    }

    /// Broadcast an alert for stale actors via MQTT.
    /// Publish per-actor alerts and the system/health summary.
    ///
    /// Called on every heartbeat poll so HA sensors always get a fresh value,
    /// regardless of whether any actors are stale.
    async fn publish_health(&self, reports: &[ActorHealthReport]) {
        let Some(pub_) = &self.publisher else { return };

        let stale: Vec<_> = reports.iter().filter(|r| r.is_stale).collect();

        // Per-actor alert for each stale actor
        for report in &stale {
            pub_.publish(
                wactorz_mqtt::topics::alert(&report.actor_id),
                &serde_json::json!({
                    "agentId": report.actor_id,
                    "agentName": report.actor_name,
                    "severity": "warning",
                    "message": format!("No heartbeat for {}s", report.last_heartbeat_secs_ago),
                    "timestampMs": std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .unwrap_or_default().as_millis() as u64,
                }),
            );
        }

        // Always publish system/health so HA sensors stay current
        pub_.publish(
            wactorz_mqtt::topics::SYSTEM_HEALTH,
            &serde_json::json!({
                "active_agents": reports.len(),
                "stale_count": stale.len(),
                "timestampMs": std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap_or_default().as_millis() as u64,
            }),
        );
    }
}

#[async_trait]
impl Actor for MonitorAgent {
    fn id(&self) -> String {
        self.config.id.clone()
    }
    fn name(&self) -> &str {
        &self.config.name
    }
    fn state(&self) -> ActorState {
        self.state.clone()
    }
    fn metrics(&self) -> Arc<ActorMetrics> {
        Arc::clone(&self.metrics)
    }
    fn mailbox(&self) -> mpsc::Sender<Message> {
        self.mailbox_tx.clone()
    }
    fn is_protected(&self) -> bool {
        self.config.protected
    }

    async fn on_start(&mut self) -> Result<()> {
        self.state = ActorState::Running;
        if let Some(pub_) = &self.publisher {
            let now_ms = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_millis() as u64;
            pub_.publish(
                wactorz_mqtt::topics::spawn(&self.config.id),
                &serde_json::json!({
                    "agentId":   self.config.id,
                    "agentName": self.config.name,
                    "agentType": "monitor",
                    "timestampMs": now_ms,
                }),
            );
        }
        Ok(())
    }

    async fn handle_message(&mut self, message: Message) -> Result<()> {
        use wactorz_core::message::MessageType;
        if let MessageType::Command {
            command: wactorz_core::message::ActorCommand::Status,
        } = &message.payload
        {
            let reports = self.poll_health().await;
            tracing::info!(
                "[monitor] {} actors, {} stale",
                reports.len(),
                reports.iter().filter(|r| r.is_stale).count()
            );
        }
        Ok(())
    }

    async fn on_heartbeat(&mut self) -> Result<()> {
        let reports = self.poll_health().await;
        self.publish_health(&reports).await;

        // Publish own heartbeat so the dashboard shows our beat count
        if let Some(pub_) = &self.publisher {
            let now_ms = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_millis() as u64;
            pub_.publish(
                wactorz_mqtt::topics::heartbeat(&self.config.id),
                &serde_json::json!({
                    "agentId":   self.config.id,
                    "agentName": self.config.name,
                    "state":     self.state,
                    "timestampMs": now_ms,
                }),
            );
        }
        Ok(())
    }

    async fn run(&mut self) -> Result<()> {
        self.on_start().await?;
        let mut rx = self
            .mailbox_rx
            .take()
            .ok_or_else(|| anyhow::anyhow!("MonitorAgent already running"))?;
        let mut hb = tokio::time::interval(std::time::Duration::from_secs(
            self.config.heartbeat_interval_secs,
        ));
        hb.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            tokio::select! {
                biased;
                msg = rx.recv() => {
                    match msg {
                        None => break,
                        Some(m) => {
                            self.metrics.record_received();
                            if let wactorz_core::message::MessageType::Command {
                                command: wactorz_core::message::ActorCommand::Stop
                            } = &m.payload {
                                break;
                            }
                            match self.handle_message(m).await {
                                Ok(_) => self.metrics.record_processed(),
                                Err(e) => {
                                    tracing::error!("[{}] {e}", self.config.name);
                                    self.metrics.record_failed();
                                }
                            }
                        }
                    }
                }
                _ = hb.tick() => {
                    self.metrics.record_heartbeat();
                    if let Err(e) = self.on_heartbeat().await {
                        tracing::error!("[{}] heartbeat: {e}", self.config.name);
                    }
                }
            }
        }
        self.state = ActorState::Stopped;
        self.on_stop().await
    }
}
