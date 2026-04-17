//! Home Assistant -> MQTT + Fuseki bridge.
//!
//! This Rust implementation takes a pragmatic parity step toward the Python
//! `wactorz.fuseki` bridge:
//! - polls Home Assistant's REST `/api/states` endpoint
//! - republishes changed states to MQTT
//! - maintains three Fuseki named graphs the frontend queries:
//!   - `urn:ha:current`
//!   - `urn:ha:devices`
//!   - `urn:ha:history`

use anyhow::Result;
use async_trait::async_trait;
use reqwest::StatusCode;
use serde_json::Value;
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::mpsc;
use urlencoding::encode;

use wactorz_core::message::{ActorCommand, MessageType};
use wactorz_core::{
    Actor, ActorConfig, ActorMetrics, ActorState, ActorSystem, EventPublisher, Message,
};

const DEFAULT_OUTPUT_TOPIC: &str = "ha/state";
const GRAPH_CURRENT: &str = "urn:ha:current";
const GRAPH_HISTORY: &str = "urn:ha:history";
const GRAPH_DEVICES: &str = "urn:ha:devices";
const GRAPH_AGENTS: &str = "urn:wactorz:agents";
const POLL_SECS: u64 = 15;

const TTL_PREFIXES: &str = "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n\
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n\
@prefix sosa: <http://www.w3.org/ns/sosa/> .\n\
@prefix syn: <https://synapse.waldiez.io/ns#> .\n\n";

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
    fuseki_url: String,
    fuseki_dataset: String,
    fuseki_user: String,
    fuseki_password: String,
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
            fuseki_url: String::new(),
            fuseki_dataset: String::new(),
            fuseki_user: String::new(),
            fuseki_password: String::new(),
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

    pub fn with_fuseki_config(mut self, url: String, dataset: String) -> Self {
        if !url.is_empty() {
            self.fuseki_url = url.trim_end_matches('/').to_string();
        }
        if !dataset.is_empty() {
            self.fuseki_dataset = dataset.trim_matches('/').to_string();
        }
        self
    }

    pub fn with_fuseki_auth(mut self, user: String, password: String) -> Self {
        self.fuseki_user = user;
        self.fuseki_password = password;
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

    fn safe(raw: &str) -> String {
        raw.chars()
            .map(|c| {
                if c.is_ascii_alphanumeric() || c == '.' || c == '_' || c == '-' {
                    c
                } else {
                    '_'
                }
            })
            .collect()
    }

    fn esc(raw: &str) -> String {
        raw.replace('\\', "\\\\")
            .replace('"', "\\\"")
            .replace('\n', "\\n")
            .replace('\r', "\\r")
            .replace('\t', "\\t")
    }

    fn literal(raw: &str) -> String {
        format!("\"{}\"", Self::esc(raw))
    }

    fn entity_iri(entity_id: &str) -> String {
        format!("<urn:ha:entity:{}>", Self::safe(entity_id))
    }

    fn obs_iri(entity_id: &str, ts_ms: u64) -> String {
        format!("<urn:ha:obs:{}_{ts_ms}>", Self::safe(entity_id))
    }

    fn label_for(state: &Value, entity_id: &str) -> String {
        state
            .get("attributes")
            .and_then(|v| v.get("friendly_name"))
            .and_then(|v| v.as_str())
            .unwrap_or(entity_id)
            .to_string()
    }

    fn state_value_for(state: &Value) -> String {
        state
            .get("state")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string()
    }

    fn timestamp_for(state: &Value) -> String {
        state
            .get("last_changed")
            .and_then(|v| v.as_str())
            .or_else(|| state.get("last_updated").and_then(|v| v.as_str()))
            .unwrap_or("1970-01-01T00:00:00Z")
            .to_string()
    }

    fn domain_type(domain: &str) -> &'static str {
        match domain {
            "sensor" | "binary_sensor" | "weather" => "sosa:Sensor",
            _ => "sosa:Actuator",
        }
    }

    fn current_ttl(&self, states: &[Value]) -> String {
        let mut ttl = String::from(TTL_PREFIXES);
        for state in states {
            let Some(entity_id) = state.get("entity_id").and_then(|v| v.as_str()) else {
                continue;
            };
            let domain = entity_id.split('.').next().unwrap_or_default();
            let label = Self::label_for(state, entity_id);
            let value = Self::state_value_for(state);
            let ts = Self::timestamp_for(state);
            let entity = Self::entity_iri(entity_id);
            ttl.push_str(&format!(
                "{entity}\n  a {} ;\n  rdfs:label {} ;\n  syn:entityId {} ;\n  syn:domain {} ;\n  syn:state {} ;\n  syn:lastChanged \"{}\"^^xsd:dateTime",
                Self::domain_type(domain),
                Self::literal(&label),
                Self::literal(entity_id),
                Self::literal(domain),
                Self::literal(&value),
                Self::esc(&ts),
            ));
            if let Some(unit) = state
                .get("attributes")
                .and_then(|v| v.get("unit_of_measurement"))
                .and_then(|v| v.as_str())
            {
                ttl.push_str(&format!(" ;\n  syn:unit {}", Self::literal(unit)));
            }
            ttl.push_str(" .\n\n");
        }
        ttl
    }

    fn devices_ttl(&self, states: &[Value]) -> String {
        let mut ttl = String::from(TTL_PREFIXES);
        ttl.push_str("<urn:ha:bridge:wactorz>\n  rdfs:label \"wactorz HA bridge\" .\n\n");
        for state in states {
            let Some(entity_id) = state.get("entity_id").and_then(|v| v.as_str()) else {
                continue;
            };
            let domain = entity_id.split('.').next().unwrap_or_default();
            let label = Self::label_for(state, entity_id);
            let entity = Self::entity_iri(entity_id);
            ttl.push_str(&format!(
                "{entity}\n  a {} ;\n  rdfs:label {} ;\n  syn:entityId {} ;\n  syn:domain {} .\n\n",
                Self::domain_type(domain),
                Self::literal(&label),
                Self::literal(entity_id),
                Self::literal(domain),
            ));
        }
        ttl
    }

    fn history_ttl(&self, state: &Value, ts_ms: u64) -> Option<String> {
        let entity_id = state.get("entity_id").and_then(|v| v.as_str())?;
        let value = Self::state_value_for(state);
        let ts = Self::timestamp_for(state);
        let obs = Self::obs_iri(entity_id, ts_ms);
        let entity = Self::entity_iri(entity_id);
        Some(format!(
            "{TTL_PREFIXES}{obs}\n  a sosa:Observation ;\n  sosa:madeBySensor {entity} ;\n  sosa:hasSimpleResult {} ;\n  sosa:resultTime \"{}\"^^xsd:dateTime .\n",
            Self::literal(&value),
            Self::esc(&ts),
        ))
    }

    async fn agents_ttl(&self) -> String {
        let mut ttl = String::from(TTL_PREFIXES);
        let Some(system) = &self.system else {
            ttl.push_str(
                "<urn:wactorz:bridge:agent-registry>\n  rdfs:label \"wactorz agent registry bridge\" .\n",
            );
            return ttl;
        };
        let actors = system.registry.list().await;
        ttl.push_str(
            "<urn:wactorz:bridge:agent-registry>\n  rdfs:label \"wactorz agent registry bridge\" .\n\n",
        );
        for actor in actors {
            let iri = format!("<urn:wactorz:agent:{}>", Self::safe(&actor.name));
            let state = format!("{}", actor.state);
            ttl.push_str(&format!(
                "{iri}\n  rdfs:label {} ;\n  syn:actorId {} ;\n  syn:state {} ;\n  syn:protected \"{}\"^^xsd:boolean",
                Self::literal(&actor.name),
                Self::literal(&actor.id),
                Self::literal(&state),
                if actor.protected { "true" } else { "false" },
            ));
            if let Some(supervisor_id) = &actor.supervisor_id {
                ttl.push_str(&format!(
                    " ;\n  syn:supervisorId {}",
                    Self::literal(supervisor_id)
                ));
            }
            ttl.push_str(" .\n\n");
        }
        ttl
    }

    fn gsp_url(&self, graph: &str) -> String {
        format!(
            "{}/{}/data?graph={}",
            self.fuseki_url,
            self.fuseki_dataset,
            encode(graph)
        )
    }

    async fn replace_graph(&self, graph: &str, ttl: String) -> Result<()> {
        if self.fuseki_url.is_empty() || self.fuseki_dataset.is_empty() {
            tracing::warn!(
                "[ha-state-bridge] skipping replace_graph graph={} because Fuseki is not configured (base='{}' dataset='{}')",
                graph,
                self.fuseki_url,
                self.fuseki_dataset
            );
            return Ok(());
        }
        let target = self.gsp_url(graph);
        tracing::info!(
            "[ha-state-bridge] replace_graph graph={} target={} bytes={}",
            graph,
            target,
            ttl.len()
        );
        let mut req = self.http.put(&target).header("Content-Type", "text/turtle");
        if !self.fuseki_user.is_empty() {
            req = req.basic_auth(&self.fuseki_user, Some(&self.fuseki_password));
        }
        let resp = req.body(ttl).send().await?;
        let status = resp.status();
        tracing::info!(
            "[ha-state-bridge] replace_graph graph={} status={}",
            graph,
            status
        );
        if !matches!(
            status,
            StatusCode::OK | StatusCode::CREATED | StatusCode::NO_CONTENT
        ) {
            let body = resp.text().await.unwrap_or_default();
            anyhow::bail!("Fuseki replace_graph {graph} failed: {status} {body}");
        }
        Ok(())
    }

    async fn append_graph(&self, graph: &str, ttl: String) -> Result<()> {
        if self.fuseki_url.is_empty() || self.fuseki_dataset.is_empty() {
            tracing::warn!(
                "[ha-state-bridge] skipping append_graph graph={} because Fuseki is not configured (base='{}' dataset='{}')",
                graph,
                self.fuseki_url,
                self.fuseki_dataset
            );
            return Ok(());
        }
        let target = self.gsp_url(graph);
        tracing::info!(
            "[ha-state-bridge] append_graph graph={} target={} bytes={}",
            graph,
            target,
            ttl.len()
        );
        let mut req = self
            .http
            .post(&target)
            .header("Content-Type", "text/turtle");
        if !self.fuseki_user.is_empty() {
            req = req.basic_auth(&self.fuseki_user, Some(&self.fuseki_password));
        }
        let resp = req.body(ttl).send().await?;
        let status = resp.status();
        tracing::info!(
            "[ha-state-bridge] append_graph graph={} status={}",
            graph,
            status
        );
        if !matches!(
            status,
            StatusCode::OK | StatusCode::CREATED | StatusCode::NO_CONTENT
        ) {
            let body = resp.text().await.unwrap_or_default();
            anyhow::bail!("Fuseki append_graph {graph} failed: {status} {body}");
        }
        Ok(())
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

        self.replace_graph(GRAPH_CURRENT, self.current_ttl(&filtered))
            .await?;
        self.replace_graph(GRAPH_DEVICES, self.devices_ttl(&filtered))
            .await?;

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
                if let Some(ttl) = self.history_ttl(state, Self::now_ms()) {
                    self.append_graph(GRAPH_HISTORY, ttl).await?;
                }
                self.publish_state_change(state).await;
                self.events_seen += 1;
            }
            self.last_states.insert(entity_id.to_string(), snapshot);
        }
        Ok(())
    }

    async fn sync_agents_graph(&self) -> Result<()> {
        self.replace_graph(GRAPH_AGENTS, self.agents_ttl().await)
            .await
    }

    fn status_payload(&self) -> Value {
        serde_json::json!({
            "configured": !self.ha_url.is_empty() && !self.ha_token.is_empty(),
            "events_seen": self.events_seen,
            "last_error": self.last_error,
            "output_topic": self.output_topic,
            "domains": self.domains,
            "fuseki_url": self.fuseki_url,
            "fuseki_dataset": self.fuseki_dataset,
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
            "[ha-state-bridge] started (ha={}, fuseki={}/{}, output_topic={}, domains={:?})",
            !self.ha_url.is_empty() && !self.ha_token.is_empty(),
            self.fuseki_url,
            self.fuseki_dataset,
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
        match self.sync_agents_graph().await {
            Ok(()) => tracing::info!("[ha-state-bridge] synced agent graph"),
            Err(err) => tracing::warn!("[ha-state-bridge] agent graph sync failed: {err}"),
        }
        if self.ha_url.is_empty() || self.ha_token.is_empty() {
            self.last_error = "HA_URL/HA_TOKEN not configured".to_string();
            tracing::warn!("[ha-state-bridge] {}", self.last_error);
            return Ok(());
        }
        if self.fuseki_url.is_empty() || self.fuseki_dataset.is_empty() {
            tracing::warn!("[ha-state-bridge] Fuseki not configured; MQTT bridge will still run");
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
                    "task": format!("ha->mqtt+fuseki events_seen={}", self.events_seen),
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
                    if let Err(err) = self.sync_agents_graph().await {
                        tracing::warn!("[ha-state-bridge] agent graph sync failed: {err}");
                    }
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
