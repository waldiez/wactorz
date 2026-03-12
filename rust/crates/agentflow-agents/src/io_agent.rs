//! UI gateway agent.
//!
//! [`IOAgent`] is the bridge between the frontend and the actor system.
//! It listens on the fixed MQTT topic `io/chat` and routes messages to
//! the appropriate actor by parsing an optional `@agent-name` prefix.
//!
//! If no `@` prefix is given, the message is forwarded to `main-actor`.

use anyhow::Result;
use async_trait::async_trait;
use std::sync::Arc;
use tokio::sync::mpsc;

use agentflow_core::{
    Actor, ActorConfig, ActorMetrics, ActorState, ActorSystem, EventPublisher, Message,
};

/// The UI gateway actor.
pub struct IOAgent {
    config: ActorConfig,
    system: ActorSystem,
    state: ActorState,
    metrics: Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher: Option<EventPublisher>,
}

impl IOAgent {
    pub fn new(config: ActorConfig, system: ActorSystem) -> Self {
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config,
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

    fn now_ms() -> u64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as u64
    }

    /// Parse `@name rest` prefix. Returns `(target_name, content)`.
    fn parse_mention(text: &str) -> (&str, &str) {
        if let Some(stripped) = text.strip_prefix('@') {
            if let Some(sp) = stripped.find(' ') {
                return (&stripped[..sp], stripped[sp + 1..].trim());
            }
            // whole text is @name with no body
            return (stripped, "");
        }
        ("main-actor", text)
    }

    /// Send an error response back to the frontend via our own chat topic.
    fn publish_error(&self, error_msg: &str) {
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                agentflow_mqtt::topics::chat(&self.config.id),
                &serde_json::json!({
                    "from": self.config.name,
                    "to": "user",
                    "content": error_msg,
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
    }
}

#[async_trait]
impl Actor for IOAgent {
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
            pub_.publish(
                agentflow_mqtt::topics::spawn(&self.config.id),
                &serde_json::json!({
                    "agentId":   self.config.id,
                    "agentName": self.config.name,
                    "agentType": "gateway",
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
        Ok(())
    }

    async fn handle_message(&mut self, message: Message) -> Result<()> {
        use agentflow_core::message::MessageType;
        let content = match &message.payload {
            MessageType::Text { content } => content.clone(),
            MessageType::Task { description, .. } => description.clone(),
            _ => return Ok(()),
        };

        let (target_name, body) = Self::parse_mention(&content);
        if body.is_empty() {
            self.publish_error("Empty message — nothing to forward.");
            return Ok(());
        }

        match self.system.registry.get_by_name(target_name).await {
            Some(entry) => {
                let msg = Message::text(
                    Some(self.config.name.clone()),
                    Some(entry.id.clone()),
                    body.to_string(),
                );
                if let Err(e) = self.system.registry.send(&entry.id, msg).await {
                    let err = format!("Failed to deliver to @{target_name}: {e}");
                    tracing::warn!("{err}");
                    self.publish_error(&err);
                }
            }
            None => {
                let err = format!("Agent @{target_name} not found.");
                tracing::warn!("{err}");
                self.publish_error(&err);
            }
        }
        Ok(())
    }

    async fn on_heartbeat(&mut self) -> Result<()> {
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                agentflow_mqtt::topics::heartbeat(&self.config.id),
                &serde_json::json!({
                    "agentId": self.config.id,
                    "agentName": self.config.name,
                    "state": self.state,
                    "timestampMs": Self::now_ms(),
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
            .ok_or_else(|| anyhow::anyhow!("IOAgent already running"))?;
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
                            if let agentflow_core::message::MessageType::Command {
                                command: agentflow_core::message::ActorCommand::Stop
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
