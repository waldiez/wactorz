//! Home Assistant actuator agent stub.
//!
//! [`HomeAssistantActuatorAgent`] is a reactive MQTT-triggered actuator that
//! subscribes to one or more MQTT detection topics, optionally evaluates Home
//! Assistant entity conditions, and calls HA services via a persistent
//! WebSocket connection.
//!
//! In the Python backend one instance is created per external automation:
//!
//! ```text
//! DynamicAgent (sensor) → MQTT topic → HomeAssistantActuatorAgent → HA service call
//! ```
//!
//! # Current status
//!
//! **Stub pending HA WebSocket client implementation.**  The agent compiles,
//! registers in the actor system, and logs every message it receives at `DEBUG`
//! level.  Full MQTT subscription, condition evaluation, and HA service calls
//! will be layered on top once the Rust HA WebSocket client is available.

use anyhow::Result;
use async_trait::async_trait;
use std::sync::Arc;
use tokio::sync::mpsc;

use wactorz_core::{Actor, ActorConfig, ActorMetrics, ActorState, EventPublisher, Message};

/// Reactive actuator that subscribes to MQTT topics and calls HA services.
///
/// Not protected — can be stopped by external commands, matching the Python
/// implementation where each actuator is tied to one automation rule.
pub struct HomeAssistantActuatorAgent {
    config: ActorConfig,
    state: ActorState,
    metrics: Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher: Option<EventPublisher>,
    ha_url: String,
    ha_token: String,
}

impl HomeAssistantActuatorAgent {
    /// Create a new [`HomeAssistantActuatorAgent`].
    ///
    /// `config.protected` is left as provided (defaults to `false`).
    pub fn new(config: ActorConfig) -> Self {
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config,
            state: ActorState::Initializing,
            metrics: Arc::new(ActorMetrics::new()),
            mailbox_tx: tx,
            mailbox_rx: Some(rx),
            publisher: None,
            ha_url: String::new(),
            ha_token: String::new(),
        }
    }

    /// Attach an [`EventPublisher`] for MQTT output.
    pub fn with_publisher(mut self, p: EventPublisher) -> Self {
        self.publisher = Some(p);
        self
    }

    /// Override the HA URL and token.
    pub fn with_ha_config(mut self, url: String, token: String) -> Self {
        if !url.is_empty() {
            self.ha_url = url;
        }
        if !token.is_empty() {
            self.ha_token = token;
        }
        self
    }
}

#[async_trait]
impl Actor for HomeAssistantActuatorAgent {
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
        tracing::info!(
            "[ha-actuator] HomeAssistantActuatorAgent started (stub — HA WebSocket client implementation pending)"
        );
        Ok(())
    }

    async fn handle_message(&mut self, message: Message) -> Result<()> {
        tracing::debug!(
            "[ha-actuator] received message from {:?}: {:?}",
            message.from,
            message.payload,
        );
        // TODO: parse detection payloads, evaluate HA conditions, and call HA
        // services once the Rust HA WebSocket client is ready.
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                wactorz_mqtt::topics::chat(&self.config.id),
                &serde_json::json!({
                    "from":    self.config.name,
                    "content": "HA actuator not yet implemented",
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
            .ok_or_else(|| anyhow::anyhow!("HomeAssistantActuatorAgent already running"))?;
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
                                command: wactorz_core::message::ActorCommand::Stop,
                            } = &m.payload
                            {
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
