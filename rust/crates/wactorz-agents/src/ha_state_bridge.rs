//! Home Assistant state-bridge agent stub.
//!
//! [`HomeAssistantStateBridgeAgent`] subscribes to Home Assistant
//! `state_changed` WebSocket events and republishes them as filtered domain
//! events on a configurable MQTT output topic.
//!
//! Configuration mirrors the Python implementation:
//! - `ha_url`  — Home Assistant base URL
//! - `ha_token`  — Long-lived access token
//! - `output_topic`  — Base MQTT topic (default: `ha/state`)
//! - `domains`  — Optional allow-list of entity domains  (e.g. `  ["light", "switch", "sensor"]  `).  Empty = all.
//! - `per_entity`  — When `true` events are published to `{output_topic}/{domain}/{entity_id}`; otherwise everything goes to `{output_topic}`.
//!
//! # Current status
//!
//! **Stub pending HA WebSocket client implementation.**  The agent compiles,
//! registers in the actor system, and logs every message it receives at `DEBUG`
//! level.  Full HA WebSocket subscription, domain filtering, and MQTT
//! publishing will be layered on top once the Rust HA WebSocket client is
//! available.

use anyhow::Result;
use async_trait::async_trait;
use std::sync::Arc;
use tokio::sync::mpsc;

use wactorz_core::{Actor, ActorConfig, ActorMetrics, ActorState, EventPublisher, Message};

/// Bridges Home Assistant `state_changed` events to MQTT topics.
///
/// Not protected — can be stopped by external commands, matching the Python
/// implementation.
pub struct HomeAssistantStateBridgeAgent {
    config: ActorConfig,
    state: ActorState,
    metrics: Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher: Option<EventPublisher>,
    ha_url: String,
    ha_token: String,
    output_topic: String,
    domains: Vec<String>,
}

impl HomeAssistantStateBridgeAgent {
    /// Create a new [`HomeAssistantStateBridgeAgent`].
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
            output_topic: "ha/state".to_string(),
            domains: Vec::new(),
        }
    }

    /// Attach an [`EventPublisher`] for MQTT output.
    pub fn with_publisher(mut self, p: EventPublisher) -> Self {
        self.publisher = Some(p);
        self
    }

    /// Override HA connection details and output configuration.
    ///
    /// `domains` is an allow-list of entity domain names (e.g. `["light",
    /// "switch"]`).  An empty `Vec` means all domains are forwarded.
    pub fn with_ha_config(
        mut self,
        url: String,
        token: String,
        output_topic: String,
        domains: Vec<String>,
    ) -> Self {
        if !url.is_empty() {
            self.ha_url = url;
        }
        if !token.is_empty() {
            self.ha_token = token;
        }
        if !output_topic.is_empty() {
            self.output_topic = output_topic;
        }
        self.domains = domains;
        self
    }
}

#[async_trait]
impl Actor for HomeAssistantStateBridgeAgent {
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
            "[ha-state-bridge] HomeAssistantStateBridgeAgent started \
             (stub — HA WebSocket client implementation pending, \
             output_topic={}, domains={:?})",
            self.output_topic,
            self.domains,
        );
        Ok(())
    }

    async fn handle_message(&mut self, message: Message) -> Result<()> {
        tracing::debug!(
            "[ha-state-bridge] received message from {:?}: {:?}",
            message.from,
            message.payload,
        );
        // TODO: subscribe to HA state_changed events, apply domain filter, and
        // publish to MQTT once the Rust HA WebSocket client is ready.
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                wactorz_mqtt::topics::chat(&self.config.id),
                &serde_json::json!({
                    "from":    self.config.name,
                    "content": "HA state bridge not yet implemented",
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
            .ok_or_else(|| anyhow::anyhow!("HomeAssistantStateBridgeAgent already running"))?;
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
