//! Pre-built agent recipe library (stub).
//!
//! [`CatalogAgent`] is a **protected** system agent that acts as a registry of
//! ready-made [`crate::dynamic_agent::DynamicAgent`] recipes.  In the Python
//! backend it spawns catalog agents on demand and injects their manifests into
//! the main actor's knowledge base.
//!
//! # Current status
//!
//! This is a **stub pending full implementation**.  It compiles, registers in
//! the actor system, and logs every message it receives at `DEBUG` level.
//! The spawn / list / info command handling will be layered on top once the
//! Rust dynamic-agent pipeline is ready.

use anyhow::Result;
use async_trait::async_trait;
use std::sync::Arc;
use tokio::sync::mpsc;

use wactorz_core::{Actor, ActorConfig, ActorMetrics, ActorState, EventPublisher, Message};

/// Pre-built agent recipe library.
///
/// Holds a catalog of ready-made [`crate::dynamic_agent::DynamicAgent`] recipes
/// and spawns them on request.  Protected so it cannot be killed by external
/// commands.
pub struct CatalogAgent {
    config: ActorConfig,
    state: ActorState,
    metrics: Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher: Option<EventPublisher>,
}

impl CatalogAgent {
    /// Create a new [`CatalogAgent`].
    ///
    /// `config.protected` is forced to `true` regardless of the value passed
    /// in, matching the Python implementation.
    pub fn new(config: ActorConfig) -> Self {
        let protected_config = ActorConfig {
            protected: true,
            ..config.clone()
        };
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config: protected_config,
            state: ActorState::Initializing,
            metrics: Arc::new(ActorMetrics::new()),
            mailbox_tx: tx,
            mailbox_rx: Some(rx),
            publisher: None,
        }
    }

    pub fn with_publisher(mut self, p: EventPublisher) -> Self {
        self.publisher = Some(p);
        self
    }
}

#[async_trait]
impl Actor for CatalogAgent {
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
        tracing::info!("[catalog] CatalogAgent started (stub — full implementation pending)");
        Ok(())
    }

    async fn handle_message(&mut self, message: Message) -> Result<()> {
        tracing::debug!(
            "[catalog] received message from {:?}: {:?}",
            message.from,
            message.payload,
        );
        // TODO: parse spawn / list / info commands and delegate to DynamicAgent
        // pipeline once the Rust dynamic-agent spawn infrastructure is ready.
        Ok(())
    }

    async fn run(&mut self) -> Result<()> {
        self.on_start().await?;
        let mut rx = self
            .mailbox_rx
            .take()
            .ok_or_else(|| anyhow::anyhow!("CatalogAgent already running"))?;
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
