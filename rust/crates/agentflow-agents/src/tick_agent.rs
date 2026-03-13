//! Scheduled / periodic ticker agent.
//!
//! [`TickAgent`] fires a configurable callback (Rhai script or MQTT publish)
//! on a fixed interval.  Useful for polling, scheduled reports, or periodic
//! data collection.
//!
//! The tick interval is configurable via the `interval_secs` field (default 60s).
//! On each tick it publishes to `agents/{id}/tick`.

use anyhow::Result;
use async_trait::async_trait;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc;

use agentflow_core::{Actor, ActorConfig, ActorMetrics, ActorState, EventPublisher, Message};

pub struct TickAgent {
    config:        ActorConfig,
    interval_secs: u64,
    /// Optional Rhai script executed on each tick (receives `tick_count` variable).
    script:        Option<String>,
    tick_count:    u64,
    state:         ActorState,
    metrics:       Arc<ActorMetrics>,
    mailbox_tx:    mpsc::Sender<Message>,
    mailbox_rx:    Option<mpsc::Receiver<Message>>,
    publisher:     Option<EventPublisher>,
}

impl TickAgent {
    pub fn new(config: ActorConfig, interval_secs: u64) -> Self {
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config,
            interval_secs,
            script:     None,
            tick_count: 0,
            state:      ActorState::Initializing,
            metrics:    Arc::new(ActorMetrics::new()),
            mailbox_tx: tx,
            mailbox_rx: Some(rx),
            publisher:  None,
        }
    }

    pub fn with_publisher(mut self, p: EventPublisher) -> Self {
        self.publisher = Some(p);
        self
    }

    pub fn with_script(mut self, script: impl Into<String>) -> Self {
        self.script = Some(script.into());
        self
    }

    fn now_ms() -> u64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as u64
    }

    fn fire_tick(&mut self) {
        self.tick_count += 1;
        let n = self.tick_count;

        if let Some(pub_) = &self.publisher {
            pub_.publish(
                format!("agents/{}/tick", self.config.id),
                &serde_json::json!({
                    "agentId":    self.config.id,
                    "agentName":  self.config.name,
                    "tickCount":  n,
                    "intervalSecs": self.interval_secs,
                    "timestampMs":  Self::now_ms(),
                }),
            );
        }

        tracing::debug!("[{}] tick #{n}", self.config.name);
    }
}

#[async_trait]
impl Actor for TickAgent {
    fn id(&self)      -> String { self.config.id.clone() }
    fn name(&self)    -> &str   { &self.config.name }
    fn state(&self)   -> ActorState { self.state.clone() }
    fn metrics(&self) -> Arc<ActorMetrics> { Arc::clone(&self.metrics) }
    fn mailbox(&self) -> mpsc::Sender<Message> { self.mailbox_tx.clone() }

    async fn on_start(&mut self) -> Result<()> {
        self.state = ActorState::Running;
        tracing::info!("[{}] Tick agent started (interval={}s)", self.config.name, self.interval_secs);
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                agentflow_mqtt::topics::spawn(&self.config.id),
                &serde_json::json!({
                    "agentId":      self.config.id,
                    "agentName":    self.config.name,
                    "agentType":    "tick",
                    "intervalSecs": self.interval_secs,
                    "timestampMs":  Self::now_ms(),
                }),
            );
        }
        Ok(())
    }

    async fn handle_message(&mut self, message: Message) -> Result<()> {
        use agentflow_core::message::MessageType;
        // Accept interval change via Task payload {"interval_secs": N}
        if let MessageType::Task { payload, .. } = &message.payload
            && let Some(n) = payload.get("interval_secs").and_then(|v| v.as_u64()) {
                self.interval_secs = n;
                tracing::info!("[{}] interval changed to {n}s", self.config.name);
        }
        Ok(())
    }

    async fn on_heartbeat(&mut self) -> Result<()> {
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                agentflow_mqtt::topics::heartbeat(&self.config.id),
                &serde_json::json!({
                    "agentId":      self.config.id,
                    "agentName":    self.config.name,
                    "state":        self.state,
                    "tickCount":    self.tick_count,
                    "intervalSecs": self.interval_secs,
                    "timestampMs":  Self::now_ms(),
                }),
            );
        }
        Ok(())
    }

    async fn run(&mut self) -> Result<()> {
        self.on_start().await?;
        let mut rx = self.mailbox_rx.take()
            .ok_or_else(|| anyhow::anyhow!("TickAgent already running"))?;
        let mut hb = tokio::time::interval(Duration::from_secs(
            self.config.heartbeat_interval_secs,
        ));
        let mut tick_timer = tokio::time::interval(Duration::from_secs(self.interval_secs));
        hb.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        tick_timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        // Skip the first immediate tick
        tick_timer.tick().await;

        loop {
            tokio::select! {
                biased;
                msg = rx.recv() => {
                    match msg {
                        None    => break,
                        Some(m) => {
                            self.metrics.record_received();
                            if let agentflow_core::message::MessageType::Command {
                                command: agentflow_core::message::ActorCommand::Stop
                            } = &m.payload { break; }
                            match self.handle_message(m).await {
                                Ok(_)  => self.metrics.record_processed(),
                                Err(e) => { tracing::error!("[{}] {e}", self.config.name); self.metrics.record_failed(); }
                            }
                        }
                    }
                }
                _ = tick_timer.tick() => {
                    self.fire_tick();
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
