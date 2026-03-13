//! Home Assistant integration agent.
//!
//! [`HomeAssistantAgent`] connects to a local Home Assistant instance via
//! its REST API and WebSocket event bus.  It can query entity states, call
//! services, and subscribe to state-change events.
//!
//! Configuration is read from environment variables:
//! - `HA_URL`   — Home Assistant base URL (e.g. `http://homeassistant.local:8123`)
//! - `HA_TOKEN` — Long-lived access token

use anyhow::Result;
use async_trait::async_trait;
use std::sync::Arc;
use tokio::sync::mpsc;

use agentflow_core::{Actor, ActorConfig, ActorMetrics, ActorState, EventPublisher, Message};
use crate::llm_agent::{LlmAgent, LlmConfig};

/// Home Assistant agent.
pub struct HomeAssistantAgent {
    config:   ActorConfig,
    ha_url:   String,
    ha_token: String,
    http:     reqwest::Client,
    llm:      Option<LlmAgent>,
    state:    ActorState,
    metrics:  Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher:  Option<EventPublisher>,
}

impl HomeAssistantAgent {
    pub fn new(config: ActorConfig) -> Self {
        let ha_url   = std::env::var("HA_URL").unwrap_or_default();
        let ha_token = std::env::var("HA_TOKEN").unwrap_or_default();
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config,
            ha_url,
            ha_token,
            http:       reqwest::Client::new(),
            llm:        None,
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

    pub fn with_llm(mut self, llm_config: LlmConfig) -> Self {
        let llm_cfg = ActorConfig::new(format!("{}-llm", self.config.name));
        self.llm = Some(LlmAgent::new(llm_cfg, llm_config));
        self
    }

    /// GET /api/states — return all entity states as JSON.
    async fn get_states(&self) -> Result<serde_json::Value> {
        let resp = self.http
            .get(format!("{}/api/states", self.ha_url))
            .header("Authorization", format!("Bearer {}", self.ha_token))
            .header("Content-Type", "application/json")
            .send().await?;
        Ok(resp.json().await?)
    }

    /// GET /api/states/<entity_id> — single entity state.
    async fn get_state(&self, entity_id: &str) -> Result<serde_json::Value> {
        let resp = self.http
            .get(format!("{}/api/states/{}", self.ha_url, entity_id))
            .header("Authorization", format!("Bearer {}", self.ha_token))
            .send().await?;
        Ok(resp.json().await?)
    }

    /// POST /api/services/<domain>/<service> — call a HA service.
    #[expect(dead_code)]
    async fn call_service(&self, domain: &str, service: &str, data: serde_json::Value) -> Result<serde_json::Value> {
        let resp = self.http
            .post(format!("{}/api/services/{}/{}", self.ha_url, domain, service))
            .header("Authorization", format!("Bearer {}", self.ha_token))
            .header("Content-Type", "application/json")
            .json(&data)
            .send().await?;
        Ok(resp.json().await?)
    }

    async fn process_request(&mut self, text: &str) -> String {
        // Simple keyword dispatch; LLM interprets if available
        let lower = text.to_lowercase();

        if lower.contains("states") || lower.contains("all entities") {
            match self.get_states().await {
                Ok(v) => format!("HA states: {}", serde_json::to_string_pretty(&v)
                    .unwrap_or_else(|_| v.to_string())),
                Err(e) => format!("HA error: {e}"),
            }
        } else if let Some(entity) = extract_entity_id(text) {
            match self.get_state(&entity).await {
                Ok(v) => format!("{entity}: {}", v["state"].as_str().unwrap_or("unknown")),
                Err(e) => format!("HA error: {e}"),
            }
        } else if let Some(llm) = &mut self.llm {
            let prompt = format!(
                "You are a Home Assistant expert. The user said: \"{text}\"\n\
                 Interpret this as a HA request and respond helpfully. \
                 If you need to call a service, suggest: call_service(domain, service, {{data}})."
            );
            llm.complete(&prompt).await.unwrap_or_else(|e| format!("LLM error: {e}"))
        } else {
            "I can query HA entity states. Try: 'get state light.living_room' or 'list all states'".into()
        }
    }

    fn now_ms() -> u64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as u64
    }
}

fn extract_entity_id(text: &str) -> Option<String> {
    // Look for patterns like "light.living_room", "sensor.temperature", etc.
    let words: Vec<&str> = text.split_whitespace().collect();
    words.iter().find(|w| w.contains('.') && !w.starts_with("http"))
        .map(|s| s.to_string())
}

#[async_trait]
impl Actor for HomeAssistantAgent {
    fn id(&self)      -> String       { self.config.id.clone() }
    fn name(&self)    -> &str         { &self.config.name }
    fn state(&self)   -> ActorState   { self.state.clone() }
    fn metrics(&self) -> Arc<ActorMetrics> { Arc::clone(&self.metrics) }
    fn mailbox(&self) -> mpsc::Sender<Message> { self.mailbox_tx.clone() }

    async fn on_start(&mut self) -> Result<()> {
        self.state = ActorState::Running;
        let connected = !self.ha_url.is_empty() && !self.ha_token.is_empty();
        tracing::info!("[{}] HA agent started (connected={})", self.config.name, connected);
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                agentflow_mqtt::topics::spawn(&self.config.id),
                &serde_json::json!({
                    "agentId":   self.config.id,
                    "agentName": self.config.name,
                    "agentType": "home_assistant",
                    "haConnected": connected,
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
        Ok(())
    }

    async fn handle_message(&mut self, message: Message) -> Result<()> {
        use agentflow_core::message::MessageType;
        let text = match &message.payload {
            MessageType::Text { content }        => content.clone(),
            MessageType::Task { description, .. } => description.clone(),
            _ => return Ok(()),
        };
        let response = self.process_request(&text).await;
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                agentflow_mqtt::topics::chat(&self.config.id),
                &serde_json::json!({
                    "from":        self.config.name,
                    "to":          message.from.as_deref().unwrap_or("user"),
                    "content":     response,
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
        Ok(())
    }

    async fn on_heartbeat(&mut self) -> Result<()> {
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                agentflow_mqtt::topics::heartbeat(&self.config.id),
                &serde_json::json!({
                    "agentId":   self.config.id,
                    "agentName": self.config.name,
                    "state":     self.state,
                    "haUrl":     self.ha_url,
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
        Ok(())
    }

    async fn run(&mut self) -> Result<()> {
        self.on_start().await?;
        let mut rx = self.mailbox_rx.take()
            .ok_or_else(|| anyhow::anyhow!("HomeAssistantAgent already running"))?;
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
                            } = &m.payload { break; }
                            match self.handle_message(m).await {
                                Ok(_)  => self.metrics.record_processed(),
                                Err(e) => { tracing::error!("[{}] {e}", self.config.name); self.metrics.record_failed(); }
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
