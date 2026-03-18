//! Smart cities data integration agent.
//!
//! [`SmartCitiesAgent`] aggregates urban data from multiple open APIs:
//! traffic, air quality, public transport, and energy consumption.
//! It synthesises data into city health summaries and publishes them to MQTT.

use anyhow::Result;
use async_trait::async_trait;
use std::sync::Arc;
use tokio::sync::mpsc;

use wactorz_core::{Actor, ActorConfig, ActorMetrics, ActorState, EventPublisher, Message};
use crate::llm_agent::{LlmAgent, LlmConfig};

pub struct SmartCitiesAgent {
    config:    ActorConfig,
    city:      String,
    http:      reqwest::Client,
    llm:       Option<LlmAgent>,
    state:     ActorState,
    metrics:   Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher: Option<EventPublisher>,
}

impl SmartCitiesAgent {
    pub fn new(config: ActorConfig, city: impl Into<String>) -> Self {
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config,
            city:      city.into(),
            http:      reqwest::Client::new(),
            llm:       None,
            state:     ActorState::Initializing,
            metrics:   Arc::new(ActorMetrics::new()),
            mailbox_tx: tx,
            mailbox_rx: Some(rx),
            publisher: None,
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

    /// Fetch air quality index from open-meteo (no API key needed).
    async fn fetch_air_quality(&self) -> Result<serde_json::Value> {
        // Use geocoding first, then air quality
        let geo_url = format!(
            "https://geocoding-api.open-meteo.com/v1/search?name={}&count=1&format=json",
            urlencoding::encode(&self.city)
        );
        let geo: serde_json::Value = self.http.get(&geo_url).send().await?.json().await?;
        let lat = geo["results"][0]["latitude"].as_f64().unwrap_or(51.5);
        let lon = geo["results"][0]["longitude"].as_f64().unwrap_or(-0.1);

        let aq_url = format!(
            "https://air-quality-api.open-meteo.com/v1/air-quality?\
             latitude={lat}&longitude={lon}\
             &hourly=pm10,pm2_5,carbon_monoxide,nitrogen_dioxide,ozone\
             &forecast_days=1"
        );
        Ok(self.http.get(&aq_url).send().await?.json().await?)
    }

    async fn process(&mut self, query: &str) -> String {
        let lower = query.to_lowercase();

        if lower.contains("air") || lower.contains("quality") || lower.contains("pollution") {
            match self.fetch_air_quality().await {
                Ok(v) => {
                    let pm25 = v["hourly"]["pm2_5"][0].as_f64().unwrap_or(0.0);
                    let pm10 = v["hourly"]["pm10"][0].as_f64().unwrap_or(0.0);
                    let no2  = v["hourly"]["nitrogen_dioxide"][0].as_f64().unwrap_or(0.0);
                    format!(
                        "Air quality for {}:\n  PM2.5: {:.1} μg/m³\n  PM10:  {:.1} μg/m³\n  NO₂:   {:.1} μg/m³",
                        self.city, pm25, pm10, no2
                    )
                }
                Err(e) => format!("Air quality fetch error: {e}"),
            }
        } else if let Some(llm) = &mut self.llm {
            let prompt = format!(
                "You are a smart cities data analyst for {}. \
                 The user asks: \"{query}\"\n\
                 Answer with available urban data insights.",
                self.city
            );
            llm.complete(&prompt).await.unwrap_or_else(|e| format!("LLM error: {e}"))
        } else {
            format!("Smart cities agent for {}. Ask about air quality, traffic, or energy.", self.city)
        }
    }

    fn now_ms() -> u64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as u64
    }
}

#[async_trait]
impl Actor for SmartCitiesAgent {
    fn id(&self)      -> String { self.config.id.clone() }
    fn name(&self)    -> &str   { &self.config.name }
    fn state(&self)   -> ActorState { self.state.clone() }
    fn metrics(&self) -> Arc<ActorMetrics> { Arc::clone(&self.metrics) }
    fn mailbox(&self) -> mpsc::Sender<Message> { self.mailbox_tx.clone() }

    async fn on_start(&mut self) -> Result<()> {
        self.state = ActorState::Running;
        tracing::info!("[{}] Smart cities agent for '{}'", self.config.name, self.city);
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                wactorz_mqtt::topics::spawn(&self.config.id),
                &serde_json::json!({
                    "agentId":   self.config.id,
                    "agentName": self.config.name,
                    "agentType": "smart_cities",
                    "city":      self.city,
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
        Ok(())
    }

    async fn handle_message(&mut self, message: Message) -> Result<()> {
        use wactorz_core::message::MessageType;
        let text = match &message.payload {
            MessageType::Text { content }        => content.clone(),
            MessageType::Task { description, .. } => description.clone(),
            _ => return Ok(()),
        };
        let response = self.process(&text).await;
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                wactorz_mqtt::topics::chat(&self.config.id),
                &serde_json::json!({
                    "from":      self.config.name,
                    "to":        message.from.as_deref().unwrap_or("user"),
                    "content":   response,
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
        Ok(())
    }

    async fn on_heartbeat(&mut self) -> Result<()> {
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                wactorz_mqtt::topics::heartbeat(&self.config.id),
                &serde_json::json!({
                    "agentId":   self.config.id,
                    "agentName": self.config.name,
                    "state":     self.state,
                    "city":      self.city,
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
        Ok(())
    }

    async fn run(&mut self) -> Result<()> {
        self.on_start().await?;
        let mut rx = self.mailbox_rx.take()
            .ok_or_else(|| anyhow::anyhow!("SmartCitiesAgent already running"))?;
        let mut hb = tokio::time::interval(std::time::Duration::from_secs(
            self.config.heartbeat_interval_secs,
        ));
        hb.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            tokio::select! {
                biased;
                msg = rx.recv() => {
                    match msg {
                        None    => break,
                        Some(m) => {
                            self.metrics.record_received();
                            if let wactorz_core::message::MessageType::Command {
                                command: wactorz_core::message::ActorCommand::Stop
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
