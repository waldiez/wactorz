//! Home Assistant -> MQTT bridge.
//!
//! - polls Home Assistant's REST `/api/states` endpoint
//! - republishes changed states to MQTT

use anyhow::Result;
use async_trait::async_trait;
use serde_json::Value;
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::mpsc;

use wactorz_core::message::{ActorCommand, MessageType};
use wactorz_core::{
    Actor, ActorConfig, ActorMetrics, ActorState, ActorSystem, EventPublisher, Message,
};

const DEFAULT_OUTPUT_TOPIC: &str = "ha/state";
const POLL_SECS: u64 = 15;

pub struct HomeAssistantStateBridgeAgent {
    config: ActorConfig,
    state: ActorState,
    metrics: Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher: Option<EventPublisher>,
    system: Option<ActorSystem>,
    http: reqwest::Client,
    ha_url: String,
    ha_token: String,
    output_topic: String,
    domains: Vec<String>,
    last_states: HashMap<String, String>,
    events_seen: u64,
    last_error: String,
}

impl HomeAssistantStateBridgeAgent {
    pub fn new(config: ActorConfig) -> Self {
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config,
            state: ActorState::Initializing,
            metrics: Arc::new(ActorMetrics::new()),
            mailbox_tx: tx,
            mailbox_rx: Some(rx),
            publisher: None,
            system: None,
            http: reqwest::Client::new(),
            ha_url: String::new(),
            ha_token: String::new(),
            output_topic: DEFAULT_OUTPUT_TOPIC.to_string(),
            domains: Vec::new(),
            last_states: HashMap::new(),
            events_seen: 0,
            last_error: String::new(),
        }
    }

    pub fn with_publisher(mut self, p: EventPublisher) -> Self {
        self.publisher = Some(p);
        self
    }

    pub fn with_system(mut self, system: ActorSystem) -> Self {
        self.system = Some(system);
        self
    }

    pub fn with_ha_config(
        mut self,
        url: String,
        token: String,
        output_topic: String,
        domains: Vec<String>,
    ) -> Self {
        if !url.is_empty() {
            self.ha_url = url.trim_end_matches('/').to_string();
        }
        if !token.is_empty() {
            self.ha_token = token;
        }
        if !output_topic.is_empty() {
            self.output_topic = output_topic;
        }
        self.domains = domains.into_iter().map(|d| d.to_lowercase()).collect();
        self
    }

    async fn fetch_states(&self) -> Result<Vec<Value>> {
        let resp = self
            .http
            .get(format!("{}/api/states", self.ha_url))
            .header("Authorization", format!("Bearer {}", self.ha_token))
            .header("Content-Type", "application/json")
            .send()
            .await?;
        let status = resp.status();
        if !status.is_success() {
            anyhow::bail!("Home Assistant states fetch failed: {status}");
        }
        Ok(resp.json::<Vec<Value>>().await?)
    }

    fn domain_allowed(&self, entity_id: &str) -> bool {
        if self.domains.is_empty() {
            return true;
        }
        let domain = entity_id
            .split('.')
            .next()
            .unwrap_or_default()
            .to_lowercase();
        self.domains.iter().any(|d| d == &domain)
    }

    async fn publish_state_change(&self, state: &Value) {
        let Some(pub_) = &self.publisher else {
            return;
        };
        let Some(entity_id) = state.get("entity_id").and_then(|v| v.as_str()) else {
            return;
        };
        let domain = entity_id.split('.').next().unwrap_or_default();
        let topic = format!("{}/{}/{}", self.output_topic, domain, entity_id);
        pub_.publish(
            topic,
            &serde_json::json!({
                "type": "home_assistant_state_change",
                "entity_id": entity_id,
                "domain": domain,
                "new_state": state,
                "timestamp": Self::now_secs(),
            }),
        );
    }

    async fn sync_once(&mut self, seed_history: bool) -> Result<()> {
        let states = self.fetch_states().await?;
        let filtered: Vec<Value> = states
            .into_iter()
            .filter(|state| {
                state
                    .get("entity_id")
                    .and_then(|v| v.as_str())
                    .map(|id| self.domain_allowed(id))
                    .unwrap_or(false)
            })
            .collect();
        tracing::info!(
            "[ha-state-bridge] sync_once fetched={} filtered={} seed_history={}",
            self.last_states.len() + filtered.len(),
            filtered.len(),
            seed_history
        );

        for state in &filtered {
            let Some(entity_id) = state.get("entity_id").and_then(|v| v.as_str()) else {
                continue;
            };
            let snapshot = serde_json::to_string(state).unwrap_or_default();
            let changed = self
                .last_states
                .get(entity_id)
                .map(|prev| prev != &snapshot)
                .unwrap_or(true);
            if seed_history || changed {
                self.publish_state_change(state).await;
                self.events_seen += 1;
            }
            self.last_states.insert(entity_id.to_string(), snapshot);
        }
        Ok(())
    }

    fn status_payload(&self) -> Value {
        serde_json::json!({
            "configured": !self.ha_url.is_empty() && !self.ha_token.is_empty(),
            "events_seen": self.events_seen,
            "last_error": self.last_error,
            "output_topic": self.output_topic,
            "domains": self.domains,
        })
    }

    fn now_ms() -> u64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as u64
    }

    fn now_secs() -> f64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs_f64()
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
            "[ha-state-bridge] started (ha={}, output_topic={}, domains={:?})",
            !self.ha_url.is_empty() && !self.ha_token.is_empty(),
            self.output_topic,
            self.domains,
        );
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                wactorz_mqtt::topics::spawn(&self.config.id),
                &serde_json::json!({
                    "agentId": self.config.id,
                    "agentName": self.config.name,
                    "agentType": "ha_state_bridge",
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
        if self.ha_url.is_empty() || self.ha_token.is_empty() {
            self.last_error = "HA_URL/HA_TOKEN not configured".to_string();
            tracing::warn!("[ha-state-bridge] {}", self.last_error);
            return Ok(());
        }
        match self.sync_once(true).await {
            Ok(()) => self.last_error.clear(),
            Err(err) => {
                self.last_error = err.to_string();
                tracing::warn!("[ha-state-bridge] initial sync failed: {err}");
            }
        }
        Ok(())
    }

    async fn handle_message(&mut self, message: Message) -> Result<()> {
        match &message.payload {
            MessageType::Text { content } if content.trim().eq_ignore_ascii_case("status") => {
                if let Some(pub_) = &self.publisher {
                    pub_.publish(
                        wactorz_mqtt::topics::chat(&self.config.id),
                        &serde_json::json!({
                            "from": self.config.name,
                            "to": message.from.as_deref().unwrap_or("user"),
                            "content": self.status_payload(),
                            "timestampMs": Self::now_ms(),
                        }),
                    );
                }
            }
            MessageType::Command {
                command: ActorCommand::Status,
            } => {
                tracing::info!(
                    "[ha-state-bridge] status requested: {}",
                    self.status_payload()
                );
            }
            _ => {}
        }
        Ok(())
    }

    async fn on_heartbeat(&mut self) -> Result<()> {
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                wactorz_mqtt::topics::heartbeat(&self.config.id),
                &serde_json::json!({
                    "agentId": self.config.id,
                    "agentName": self.config.name,
                    "state": self.state,
                    "task": format!("ha->mqtt events_seen={}", self.events_seen),
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
            .ok_or_else(|| anyhow::anyhow!("HomeAssistantStateBridgeAgent already running"))?;
        let mut hb = tokio::time::interval(std::time::Duration::from_secs(
            self.config.heartbeat_interval_secs,
        ));
        hb.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        let mut poll = tokio::time::interval(std::time::Duration::from_secs(POLL_SECS));
        poll.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            tokio::select! {
                biased;
                msg = rx.recv() => {
                    match msg {
                        None => break,
                        Some(m) => {
                            self.metrics.record_received();
                            if let MessageType::Command { command: ActorCommand::Stop } = &m.payload {
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
                _ = poll.tick() => {
                    match self.sync_once(false).await {
                        Ok(()) => self.last_error.clear(),
                        Err(err) => {
                            self.last_error = err.to_string();
                            tracing::warn!("[ha-state-bridge] sync failed: {err}");
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
