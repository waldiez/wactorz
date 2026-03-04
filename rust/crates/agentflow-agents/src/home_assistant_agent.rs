//! HomeAssistantAgent — queries Home Assistant for device/entity information.
//!
//! Named **HA** in the codebase. Connects to the Home Assistant REST API
//! (`HA_URL` / `HOME_ASSISTANT_URL`) using a long-lived access token
//! (`HA_TOKEN` / `HOME_ASSISTANT_TOKEN`).
//!
//! This agent provides hardware discovery and listing. For LLM-powered
//! hardware *recommendations*, send the output to `main-actor`.
//!
//! ## Commands
//!
//! | Command | Description |
//! |---------|-------------|
//! | `devices` | List all devices with their entities |
//! | `entities` | List all entity IDs |
//! | `search <keyword>` | Find devices/entities matching keyword |
//! | `state <entity_id>` | Get current state of an entity |
//! | `domains` | List all entity domains (light, switch, sensor, …) |
//! | `status` | HA connection status and summary |
//! | `help` | Show this help |
//!
//! ## Environment
//!
//! - `HA_URL` / `HOME_ASSISTANT_URL` — Home Assistant base URL (e.g. `http://homeassistant:8123`)
//! - `HA_TOKEN` / `HOME_ASSISTANT_TOKEN` — Long-lived access token

use anyhow::Result;
use async_trait::async_trait;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::{mpsc, Mutex};

use agentflow_core::{
    Actor, ActorConfig, ActorMetrics, ActorState, EventPublisher, Message,
};

// ── Cache ─────────────────────────────────────────────────────────────────────

const CACHE_TTL_SECS: u64 = 30;

#[derive(Debug, Clone, Default)]
struct DeviceCache {
    devices: Vec<serde_json::Value>,
    entities: Vec<serde_json::Value>,
    cached_at: Option<Instant>,
}

impl DeviceCache {
    fn is_fresh(&self) -> bool {
        self.cached_at
            .map(|t| t.elapsed() < Duration::from_secs(CACHE_TTL_SECS))
            .unwrap_or(false)
    }
}

// ── Agent ─────────────────────────────────────────────────────────────────────

/// Home Assistant device-discovery agent.
pub struct HomeAssistantAgent {
    config: ActorConfig,
    state: ActorState,
    metrics: Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher: Option<EventPublisher>,
    ha_url: String,
    ha_token: String,
    cache: Arc<Mutex<DeviceCache>>,
}

impl HomeAssistantAgent {
    /// Create a new `HomeAssistantAgent`.
    /// Reads `HA_URL`/`HOME_ASSISTANT_URL` and `HA_TOKEN`/`HOME_ASSISTANT_TOKEN` from env.
    pub fn new(config: ActorConfig) -> Self {
        let ha_url = std::env::var("HA_URL")
            .or_else(|_| std::env::var("HOME_ASSISTANT_URL"))
            .unwrap_or_default()
            .trim_end_matches('/')
            .to_string();
        let ha_token = std::env::var("HA_TOKEN")
            .or_else(|_| std::env::var("HOME_ASSISTANT_TOKEN"))
            .unwrap_or_default();

        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config,
            state: ActorState::Initializing,
            metrics: Arc::new(ActorMetrics::new()),
            mailbox_tx: tx,
            mailbox_rx: Some(rx),
            publisher: None,
            ha_url,
            ha_token,
            cache: Arc::new(Mutex::new(DeviceCache::default())),
        }
    }

    /// Attach an [`EventPublisher`] for MQTT output.
    pub fn with_publisher(mut self, p: EventPublisher) -> Self {
        self.publisher = Some(p);
        self
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    fn now_ms() -> u64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as u64
    }

    fn reply(&self, content: &str) {
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                agentflow_mqtt::topics::chat(&self.config.id),
                &serde_json::json!({
                    "from":        self.config.name,
                    "to":          "user",
                    "content":     content,
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
    }

    fn is_configured(&self) -> bool {
        !self.ha_url.is_empty() && !self.ha_token.is_empty()
    }

    // ── HA REST helpers ───────────────────────────────────────────────────────

    async fn ha_get(&self, path: &str) -> Result<serde_json::Value, String> {
        let url = format!("{}{path}", self.ha_url);
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(10))
            .build()
            .map_err(|e| format!("HTTP client error: {e}"))?;

        let resp = client
            .get(&url)
            .header("Authorization", format!("Bearer {}", self.ha_token))
            .header("Content-Type", "application/json")
            .send()
            .await
            .map_err(|e| format!("Request to {url} failed: {e}"))?;

        if !resp.status().is_success() {
            return Err(format!(
                "HA returned HTTP {} for {path}",
                resp.status().as_u16()
            ));
        }

        resp.json::<serde_json::Value>()
            .await
            .map_err(|e| format!("JSON parse error: {e}"))
    }

    async fn refresh_cache(&self) -> Result<(), String> {
        let devices_val = self.ha_get("/api/config/device_registry/list").await?;
        let entities_val = self.ha_get("/api/config/entity_registry/list").await?;

        let devices = devices_val.as_array().cloned().unwrap_or_default();
        let entities = entities_val.as_array().cloned().unwrap_or_default();

        let mut cache = self.cache.lock().await;
        cache.devices = devices;
        cache.entities = entities;
        cache.cached_at = Some(Instant::now());
        Ok(())
    }

    async fn ensure_cache(&self) -> Result<(), String> {
        let fresh = {
            let c = self.cache.lock().await;
            c.is_fresh()
        };
        if !fresh {
            self.refresh_cache().await?;
        }
        Ok(())
    }

    // ── Command handlers ──────────────────────────────────────────────────────

    async fn cmd_status(&self) {
        if !self.is_configured() {
            self.reply(
                "**Home Assistant Agent**\n\n\
                ⚠ Not configured.\n\n\
                Set `HA_URL` and `HA_TOKEN` environment variables to connect to Home Assistant.",
            );
            return;
        }

        match self.ha_get("/api/").await {
            Ok(info) => {
                let version = info
                    .get("version")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown");
                let location = info
                    .get("location_name")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown");
                self.reply(&format!(
                    "**Home Assistant Status**\n\n\
                    ✓ Connected to `{url}`\n\
                    📍 Location: {location}\n\
                    🔖 Version: {version}",
                    url = self.ha_url,
                ));
            }
            Err(e) => {
                self.reply(&format!(
                    "**Home Assistant Status**\n\n\
                    ✗ Cannot connect to `{url}`:\n```\n{e}\n```",
                    url = self.ha_url,
                ));
            }
        }
    }

    async fn cmd_devices(&self) {
        if !self.is_configured() {
            self.reply("HA not configured. Set `HA_URL` and `HA_TOKEN`.");
            return;
        }
        if let Err(e) = self.ensure_cache().await {
            self.reply(&format!("✗ {e}"));
            return;
        }
        let cache = self.cache.lock().await;
        if cache.devices.is_empty() {
            self.reply("No devices found in Home Assistant.");
            return;
        }
        let mut lines = vec![format!("**Devices ({}):**\n", cache.devices.len())];
        for d in cache.devices.iter().take(25) {
            let name = d.get("name").and_then(|v| v.as_str()).unwrap_or("?");
            let mfr = d.get("manufacturer").and_then(|v| v.as_str()).unwrap_or("");
            let model = d.get("model").and_then(|v| v.as_str()).unwrap_or("");
            let area = d.get("area_id").and_then(|v| v.as_str()).unwrap_or("");
            let mut line = format!("- **{name}**");
            if !mfr.is_empty() || !model.is_empty() {
                line.push_str(&format!(" ({mfr} {model})"));
            }
            if !area.is_empty() {
                line.push_str(&format!(" — area: {area}"));
            }
            lines.push(line);
        }
        if cache.devices.len() > 25 {
            lines.push(format!("… and {} more", cache.devices.len() - 25));
        }
        self.reply(&lines.join("\n"));
    }

    async fn cmd_entities(&self) {
        if !self.is_configured() {
            self.reply("HA not configured. Set `HA_URL` and `HA_TOKEN`.");
            return;
        }
        if let Err(e) = self.ensure_cache().await {
            self.reply(&format!("✗ {e}"));
            return;
        }
        let cache = self.cache.lock().await;
        if cache.entities.is_empty() {
            self.reply("No entities found in Home Assistant.");
            return;
        }
        let mut lines = vec![format!("**Entities ({}):**\n", cache.entities.len())];
        for e in cache.entities.iter().take(30) {
            let eid = e.get("entity_id").and_then(|v| v.as_str()).unwrap_or("?");
            let name = e
                .get("original_name")
                .or_else(|| e.get("name"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            if name.is_empty() {
                lines.push(format!("- `{eid}`"));
            } else {
                lines.push(format!("- `{eid}` — {name}"));
            }
        }
        if cache.entities.len() > 30 {
            lines.push(format!("… and {} more. Use `search <keyword>` to filter.", cache.entities.len() - 30));
        }
        self.reply(&lines.join("\n"));
    }

    async fn cmd_domains(&self) {
        if !self.is_configured() {
            self.reply("HA not configured. Set `HA_URL` and `HA_TOKEN`.");
            return;
        }
        if let Err(e) = self.ensure_cache().await {
            self.reply(&format!("✗ {e}"));
            return;
        }
        let cache = self.cache.lock().await;
        let mut domains: std::collections::BTreeMap<String, usize> = std::collections::BTreeMap::new();
        for e in &cache.entities {
            if let Some(eid) = e.get("entity_id").and_then(|v| v.as_str()) {
                if let Some(dot) = eid.find('.') {
                    *domains.entry(eid[..dot].to_string()).or_insert(0) += 1;
                }
            }
        }
        if domains.is_empty() {
            self.reply("No entity domains found.");
            return;
        }
        let mut lines = vec![format!("**Entity Domains ({}):**\n", domains.len())];
        for (domain, count) in &domains {
            lines.push(format!("- `{domain}` — {count} entities"));
        }
        self.reply(&lines.join("\n"));
    }

    async fn cmd_search(&self, keyword: &str) {
        if !self.is_configured() {
            self.reply("HA not configured. Set `HA_URL` and `HA_TOKEN`.");
            return;
        }
        if keyword.is_empty() {
            self.reply("Usage: `search <keyword>`");
            return;
        }
        if let Err(e) = self.ensure_cache().await {
            self.reply(&format!("✗ {e}"));
            return;
        }
        let kw = keyword.to_lowercase();
        let cache = self.cache.lock().await;

        let mut matches = vec![];
        for e in &cache.entities {
            let eid = e.get("entity_id").and_then(|v| v.as_str()).unwrap_or("");
            let name = e
                .get("original_name")
                .or_else(|| e.get("name"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            if eid.to_lowercase().contains(&kw) || name.to_lowercase().contains(&kw) {
                matches.push((eid.to_string(), name.to_string()));
            }
        }

        if matches.is_empty() {
            self.reply(&format!("No entities matching `{keyword}`.\n\nUse `domains` to see available entity types."));
            return;
        }

        let mut lines = vec![format!("**Search: `{keyword}`** — {} match(es):\n", matches.len())];
        for (eid, name) in matches.iter().take(20) {
            if name.is_empty() {
                lines.push(format!("- `{eid}`"));
            } else {
                lines.push(format!("- `{eid}` — {name}"));
            }
        }
        if matches.len() > 20 {
            lines.push(format!("… and {} more", matches.len() - 20));
        }
        self.reply(&lines.join("\n"));
    }

    async fn cmd_state(&self, entity_id: &str) {
        if !self.is_configured() {
            self.reply("HA not configured. Set `HA_URL` and `HA_TOKEN`.");
            return;
        }
        if entity_id.is_empty() {
            self.reply("Usage: `state <entity_id>`  e.g. `state light.living_room`");
            return;
        }
        match self.ha_get(&format!("/api/states/{entity_id}")).await {
            Ok(s) => {
                let state_val = s.get("state").and_then(|v| v.as_str()).unwrap_or("unknown");
                let friendly = s
                    .get("attributes")
                    .and_then(|a| a.get("friendly_name"))
                    .and_then(|v| v.as_str())
                    .unwrap_or(entity_id);
                let unit = s
                    .get("attributes")
                    .and_then(|a| a.get("unit_of_measurement"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                let updated = s.get("last_updated").and_then(|v| v.as_str()).unwrap_or("");
                self.reply(&format!(
                    "**{friendly}** (`{entity_id}`)\n\nState: **{state_val}**{unit_str}\n\nLast updated: {updated}",
                    unit_str = if unit.is_empty() { String::new() } else { format!(" {unit}") },
                ));
            }
            Err(e) => {
                self.reply(&format!("✗ Could not get state for `{entity_id}`: {e}"));
            }
        }
    }

    async fn dispatch(&self, text: &str) {
        let tokens: Vec<&str> = text.split_whitespace().collect();
        let prefix_stripped = {
            let lower = text.to_lowercase();
            if lower.starts_with("@ha-agent") || lower.starts_with("@ha_agent") {
                text.splitn(2, char::is_whitespace)
                    .nth(1)
                    .unwrap_or("")
                    .trim()
            } else {
                text.trim()
            }
        };
        let tokens: Vec<&str> = prefix_stripped.split_whitespace().collect();
        match tokens.as_slice() {
            [] | ["help" | "?"] => {
                self.reply(
                    "**HomeAssistantAgent** — HA device discovery\n\n\
                     | Command | Description |\n\
                     |---------|-------------|\n\
                     | `status` | HA connection status |\n\
                     | `devices` | List all devices |\n\
                     | `entities` | List all entities |\n\
                     | `domains` | List entity domains |\n\
                     | `search <keyword>` | Search entities/devices |\n\
                     | `state <entity_id>` | Get entity state |\n\
                     | `help` | Show this message |\n\n\
                     _For hardware recommendations, use `@main-actor`._",
                );
            }
            ["status"] => self.cmd_status().await,
            ["devices"] => self.cmd_devices().await,
            ["entities"] => self.cmd_entities().await,
            ["domains"] => self.cmd_domains().await,
            ["search", rest @ ..] => self.cmd_search(&rest.join(" ")).await,
            ["state", entity_id] => self.cmd_state(entity_id).await,
            [cmd, ..] => {
                self.reply(&format!("Unknown command: `{cmd}`. Type `help`."));
            }
        }
    }
}

// ── Actor impl ────────────────────────────────────────────────────────────────

#[async_trait]
impl Actor for HomeAssistantAgent {
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
                    "agentType": "home-assistant",
                    "configured": self.is_configured(),
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
        tracing::info!(
            "[ha-agent] started — configured: {}, url: {}",
            self.is_configured(),
            if self.ha_url.is_empty() { "<not set>" } else { &self.ha_url }
        );
        Ok(())
    }

    async fn handle_message(&mut self, message: Message) -> Result<()> {
        use agentflow_core::message::MessageType;
        let text = match &message.payload {
            MessageType::Text { content } => content.trim().to_string(),
            MessageType::Task { description, .. } => description.trim().to_string(),
            _ => return Ok(()),
        };
        if text.is_empty() {
            return Ok(());
        }
        self.dispatch(&text).await;
        self.metrics.record_processed();
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
            .ok_or_else(|| anyhow::anyhow!("HomeAssistantAgent already running"))?;
        let mut hb = tokio::time::interval(Duration::from_secs(
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
                                command: agentflow_core::message::ActorCommand::Stop,
                            } = &m.payload
                            {
                                break;
                            }
                            if let Err(e) = self.handle_message(m).await {
                                tracing::error!("[ha-agent] {e}");
                                self.metrics.record_failed();
                            }
                        }
                    }
                }
                _ = hb.tick() => {
                    self.metrics.record_heartbeat();
                    if let Err(e) = self.on_heartbeat().await {
                        tracing::error!("[ha-agent] heartbeat: {e}");
                    }
                }
            }
        }
        self.state = ActorState::Stopped;
        self.on_stop().await
    }
}
