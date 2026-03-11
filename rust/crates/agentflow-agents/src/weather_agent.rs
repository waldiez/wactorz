//! Weather information agent.
//!
//! [`WeatherAgent`] fetches current weather conditions on demand using the
//! free [wttr.in](https://wttr.in) service — **no API key required**.
//!
//! ## Usage (via IO bar)
//!
//! ```text
//! @weather-agent                  → weather for default location (WEATHER_DEFAULT_LOCATION or "London")
//! @weather-agent Tokyo            → weather for Tokyo
//! @weather-agent New York         → weather for New York
//! @weather-agent help             → show usage
//! ```
//!
//! The agent does **not** poll; it only fetches when it receives a message.
//! It is stoppable and pausable — consumes no resources when idle.

use anyhow::Result;
use async_trait::async_trait;
use std::sync::Arc;
use tokio::sync::mpsc;

use agentflow_core::{
    Actor, ActorConfig, ActorMetrics, ActorState, EventPublisher, Message,
};

/// Default location used when the user sends `@weather-agent` with no argument.
const DEFAULT_LOCATION_ENV: &str = "WEATHER_DEFAULT_LOCATION";
const DEFAULT_LOCATION_FALLBACK: &str = "London";

/// Idle timeout for the reqwest client.
const HTTP_TIMEOUT_SECS: u64 = 10;

pub struct WeatherAgent {
    config: ActorConfig,
    state: ActorState,
    metrics: Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher: Option<EventPublisher>,
    http: reqwest::Client,
}

impl WeatherAgent {
    pub fn new(config: ActorConfig) -> Self {
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        let http = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(HTTP_TIMEOUT_SECS))
            .user_agent("AgentFlow-WeatherAgent/1.0")
            .build()
            .unwrap_or_default();
        Self {
            config,
            state: ActorState::Initializing,
            metrics: Arc::new(ActorMetrics::new()),
            mailbox_tx: tx,
            mailbox_rx: Some(rx),
            publisher: None,
            http,
        }
    }

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

    fn default_location() -> String {
        std::env::var(DEFAULT_LOCATION_ENV)
            .unwrap_or_else(|_| DEFAULT_LOCATION_FALLBACK.to_string())
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

    async fn fetch_weather(&self, location: &str) -> Result<String> {
        // wttr.in format=j1 returns JSON with current conditions.
        let url = format!(
            "https://wttr.in/{}?format=j1",
            urlencoding(location)
        );

        let resp = self.http.get(&url).send().await?;
        if !resp.status().is_success() {
            // Fallback to one-line format on error
            let url2 = format!("https://wttr.in/{}?format=3", urlencoding(location));
            let r2 = self.http.get(&url2).send().await?;
            return Ok(r2.text().await?.trim().to_string());
        }

        let json: serde_json::Value = resp.json().await?;

        // Parse the JSON response
        let current = json
            .get("current_condition")
            .and_then(|a| a.as_array())
            .and_then(|a| a.first())
            .cloned()
            .unwrap_or_default();

        let desc = current
            .get("weatherDesc")
            .and_then(|a| a.as_array())
            .and_then(|a| a.first())
            .and_then(|v| v.get("value"))
            .and_then(|v| v.as_str())
            .unwrap_or("Unknown");

        let temp_c  = current.get("temp_C").and_then(|v| v.as_str()).unwrap_or("?");
        let temp_f  = current.get("temp_F").and_then(|v| v.as_str()).unwrap_or("?");
        let feels_c = current.get("FeelsLikeC").and_then(|v| v.as_str()).unwrap_or("?");
        let humidity = current.get("humidity").and_then(|v| v.as_str()).unwrap_or("?");
        let wind_kmph = current.get("windspeedKmph").and_then(|v| v.as_str()).unwrap_or("?");
        let wind_dir  = current.get("winddir16Point").and_then(|v| v.as_str()).unwrap_or("?");
        let uv = current.get("uvIndex").and_then(|v| v.as_str()).unwrap_or("?");
        let visibility = current.get("visibility").and_then(|v| v.as_str()).unwrap_or("?");

        // Nearest area name
        let area = json
            .get("nearest_area")
            .and_then(|a| a.as_array())
            .and_then(|a| a.first())
            .and_then(|v| v.get("areaName"))
            .and_then(|a| a.as_array())
            .and_then(|a| a.first())
            .and_then(|v| v.get("value"))
            .and_then(|v| v.as_str())
            .unwrap_or(location);

        Ok(format!(
            "**Weather in {area}**\n\n\
             🌡 **{temp_c}°C / {temp_f}°F** (feels like {feels_c}°C)\n\
             ☁ {desc}\n\
             💧 Humidity: {humidity}%\n\
             💨 Wind: {wind_kmph} km/h {wind_dir}\n\
             👁 Visibility: {visibility} km\n\
             ☀ UV index: {uv}\n\n\
             *Data: [wttr.in](https://wttr.in/{loc})*",
            loc = urlencoding(location)
        ))
    }
}

/// Minimal URL percent-encoding for location names.
fn urlencoding(s: &str) -> String {
    s.chars()
        .flat_map(|c| match c {
            ' ' => vec!['+'],
            c if c.is_alphanumeric() || matches!(c, '-' | '_' | '.' | ',') => vec![c],
            c => format!("%{:02X}", c as u32).chars().collect(),
        })
        .collect()
}

#[async_trait]
impl Actor for WeatherAgent {
    fn id(&self)       -> String       { self.config.id.clone() }
    fn name(&self)     -> &str         { &self.config.name }
    fn state(&self)    -> ActorState   { self.state.clone() }
    fn metrics(&self)  -> Arc<ActorMetrics> { Arc::clone(&self.metrics) }
    fn mailbox(&self)  -> mpsc::Sender<Message> { self.mailbox_tx.clone() }
    fn is_protected(&self) -> bool     { self.config.protected }

    async fn on_start(&mut self) -> Result<()> {
        self.state = ActorState::Running;
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                agentflow_mqtt::topics::spawn(&self.config.id),
                &serde_json::json!({
                    "agentId":   self.config.id,
                    "agentName": self.config.name,
                    "agentType": "data",
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
        Ok(())
    }

    async fn handle_message(&mut self, message: Message) -> Result<()> {
        use agentflow_core::message::MessageType;

        let content = match &message.payload {
            MessageType::Text { content } => content.trim().to_string(),
            MessageType::Task { description, .. } => description.trim().to_string(),
            _ => return Ok(()),
        };

        // Strip @weather-agent prefix if present
        let arg = content
            .strip_prefix("@weather-agent")
            .unwrap_or(&content)
            .trim()
            .to_string();

        match arg.to_lowercase().as_str() {
            "" => {
                let loc = Self::default_location();
                let typing = format!("🌦 Fetching weather for **{loc}**…");
                self.reply(&typing);
                match self.fetch_weather(&loc).await {
                    Ok(report) => self.reply(&report),
                    Err(e) => self.reply(&format!("⚠ Could not fetch weather: {e}")),
                }
            }
            "help" => {
                let default = Self::default_location();
                self.reply(&format!(
                    "**WeatherAgent** — current conditions via wttr.in (no API key needed)\n\n\
                     ```\n\
                     @weather-agent              # {default} (default)\n\
                     @weather-agent Tokyo\n\
                     @weather-agent New York\n\
                     @weather-agent 48.8566,2.3522  # coordinates\n\
                     ```\n\
                     Set `WEATHER_DEFAULT_LOCATION` in `.env` to change the default."
                ));
            }
            location => {
                let typing = format!("🌦 Fetching weather for **{location}**…");
                self.reply(&typing);
                match self.fetch_weather(location).await {
                    Ok(report) => self.reply(&report),
                    Err(e) => self.reply(&format!("⚠ Could not fetch weather for '{location}': {e}")),
                }
            }
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
            .ok_or_else(|| anyhow::anyhow!("WeatherAgent already running"))?;
        let mut hb = tokio::time::interval(std::time::Duration::from_secs(
            self.config.heartbeat_interval_secs,
        ));
        hb.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            tokio::select! {
                biased;
                msg = rx.recv() => match msg {
                    None => break,
                    Some(m) => {
                        self.metrics.record_received();
                        if let agentflow_core::message::MessageType::Command {
                            command: agentflow_core::message::ActorCommand::Stop
                        } = &m.payload { break; }
                        match self.handle_message(m).await {
                            Ok(_)  => self.metrics.record_processed(),
                            Err(e) => {
                                tracing::error!("[{}] {e}", self.config.name);
                                self.metrics.record_failed();
                            }
                        }
                    }
                },
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
