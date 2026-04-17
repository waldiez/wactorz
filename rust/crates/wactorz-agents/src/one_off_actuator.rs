//! Ephemeral one-shot Home Assistant actuator agent (stub).
//!
//! [`OneOffActuatorAgent`] mirrors the Python `OneOffActuatorAgent`: it is
//! spawned on demand by the orchestrator to resolve and execute a single
//! natural-language Home Assistant service-call request, then terminates.
//!
//! # Current status
//!
//! This is a **stub pending full implementation**.  The run loop exits after
//! processing one message, matching the one-shot semantics of the Python
//! version.  HA resolution and LLM inference will be layered on top once the
//! relevant pipelines are available in Rust.

use anyhow::Result;
use async_trait::async_trait;
use std::sync::Arc;
use tokio::sync::mpsc;

use wactorz_core::{Actor, ActorConfig, ActorMetrics, ActorState, EventPublisher, Message};

/// Ephemeral actor that resolves and executes one-shot HA service calls.
///
/// Spawned on demand by the orchestrator.  Exits after processing a single
/// message, matching the Python implementation's lifecycle.
pub struct OneOffActuatorAgent {
    config: ActorConfig,
    state: ActorState,
    metrics: Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher: Option<EventPublisher>,
    ha_url: String,
    ha_token: String,
    task_id: String,
    reply_to_id: String,
}

impl OneOffActuatorAgent {
    /// Create a new [`OneOffActuatorAgent`] for the given `task_id`.
    ///
    /// The actor name is derived from the last 8 characters of `task_id`,
    /// matching the Python naming convention.
    pub fn new(config: ActorConfig, task_id: String, reply_to_id: String) -> Self {
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
            task_id,
            reply_to_id,
        }
    }

    /// Attach an event publisher for MQTT output.
    pub fn with_publisher(mut self, p: EventPublisher) -> Self {
        self.publisher = Some(p);
        self
    }

    /// Configure the Home Assistant base URL and long-lived access token.
    pub fn with_ha_config(mut self, url: String, token: String) -> Self {
        self.ha_url = url;
        self.ha_token = token;
        self
    }
}

#[async_trait]
impl Actor for OneOffActuatorAgent {
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
            "[one-off-actuator] OneOffActuator spawned for task {}",
            self.task_id
        );
        Ok(())
    }

    async fn handle_message(&mut self, message: Message) -> Result<()> {
        tracing::debug!(
            "[one-off-actuator] received message from {:?}: {:?}",
            message.from,
            message.payload,
        );
        // TODO: resolve natural-language request via LLM → HA service calls,
        // then send a RESULT message back to reply_to_id.
        let stub_reply = format!(
            "[stub] OneOffActuator for task {} received request (HA actuation not yet implemented)",
            self.task_id
        );
        if !self.reply_to_id.is_empty() {
            tracing::info!(
                "[one-off-actuator] would reply to {} with: {}",
                self.reply_to_id,
                stub_reply
            );
        }
        Ok(())
    }

    async fn run(&mut self) -> Result<()> {
        self.on_start().await?;
        let mut rx = self
            .mailbox_rx
            .take()
            .ok_or_else(|| anyhow::anyhow!("OneOffActuatorAgent already running"))?;

        // One-shot: process exactly one message then exit.
        if let Some(msg) = rx.recv().await {
            self.metrics.record_received();
            match self.handle_message(msg).await {
                Ok(_) => self.metrics.record_processed(),
                Err(e) => {
                    tracing::error!("[{}] {e}", self.config.name);
                    self.metrics.record_failed();
                }
            }
        }

        self.state = ActorState::Stopped;
        self.on_stop().await
    }
}
