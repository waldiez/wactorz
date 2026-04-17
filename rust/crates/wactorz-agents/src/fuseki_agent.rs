//! Apache Jena Fuseki SPARQL agent.
//!
//! [`FusekiAgent`] executes SPARQL queries and updates against an Apache
//! Jena Fuseki endpoint.  It also supports LLM-assisted query generation.
//!
//! Configuration (env vars):
//! - `FUSEKI_URL`     — Fuseki base URL (default: `http://fuseki:3030`)
//! - `FUSEKI_DATASET` — Dataset path   (default: `/ds`)

use anyhow::Result;
use async_trait::async_trait;
use std::sync::Arc;
use tokio::sync::mpsc;

use crate::llm_agent::{LlmAgent, LlmConfig};
use wactorz_core::{Actor, ActorConfig, ActorMetrics, ActorState, EventPublisher, Message};

pub struct FusekiAgent {
    config: ActorConfig,
    fuseki_url: String,
    dataset: String,
    http: reqwest::Client,
    llm: Option<LlmAgent>,
    state: ActorState,
    metrics: Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher: Option<EventPublisher>,
}

impl FusekiAgent {
    pub fn new(config: ActorConfig) -> Self {
        let fuseki_url =
            std::env::var("FUSEKI_URL").unwrap_or_else(|_| "http://fuseki:3030".into());
        let dataset = std::env::var("FUSEKI_DATASET").unwrap_or_else(|_| "/ds".into());
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config,
            fuseki_url,
            dataset,
            http: reqwest::Client::new(),
            llm: None,
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

    /// Override the Fuseki URL and dataset instead of relying on environment variables.
    pub fn with_fuseki_config(
        mut self,
        url: impl Into<String>,
        dataset: impl Into<String>,
    ) -> Self {
        let url = url.into();
        let dataset = dataset.into();
        if !url.is_empty() {
            self.fuseki_url = url;
        }
        if !dataset.is_empty() {
            self.dataset = dataset;
        }
        self
    }

    pub fn with_llm(mut self, llm_config: LlmConfig) -> Self {
        let llm_cfg = ActorConfig::new(format!("{}-llm", self.config.name));
        self.llm = Some(LlmAgent::new(llm_cfg, llm_config));
        self
    }

    /// Execute a SPARQL SELECT query; returns JSON results.
    async fn sparql_query(&self, query: &str) -> Result<serde_json::Value> {
        let url = format!("{}{}/query", self.fuseki_url, self.dataset);
        let resp = self
            .http
            .post(&url)
            .header("Content-Type", "application/sparql-query")
            .header("Accept", "application/sparql-results+json")
            .body(query.to_string())
            .send()
            .await?;
        if !resp.status().is_success() {
            let s = resp.status();
            let t = resp.text().await.unwrap_or_default();
            anyhow::bail!("Fuseki {s}: {t}");
        }
        Ok(resp.json().await?)
    }

    /// Execute a SPARQL UPDATE statement.
    async fn sparql_update(&self, update: &str) -> Result<()> {
        let url = format!("{}{}/update", self.fuseki_url, self.dataset);
        let resp = self
            .http
            .post(&url)
            .header("Content-Type", "application/sparql-update")
            .body(update.to_string())
            .send()
            .await?;
        if !resp.status().is_success() {
            let s = resp.status();
            let t = resp.text().await.unwrap_or_default();
            anyhow::bail!("Fuseki update {s}: {t}");
        }
        Ok(())
    }

    async fn process(&mut self, text: &str) -> String {
        // If input looks like SPARQL, run it directly
        let trimmed = text.trim().to_uppercase();
        if trimmed.starts_with("SELECT")
            || trimmed.starts_with("ASK")
            || trimmed.starts_with("CONSTRUCT")
        {
            match self.sparql_query(text).await {
                Ok(v) => format!(
                    "SPARQL results:\n{}",
                    serde_json::to_string_pretty(&v).unwrap_or_else(|_| v.to_string())
                ),
                Err(e) => format!("Fuseki error: {e}"),
            }
        } else if trimmed.starts_with("INSERT")
            || trimmed.starts_with("DELETE")
            || trimmed.starts_with("WITH")
        {
            match self.sparql_update(text).await {
                Ok(()) => "Update executed successfully.".into(),
                Err(e) => format!("Fuseki update error: {e}"),
            }
        } else if let Some(llm) = &mut self.llm {
            let prompt = format!(
                "You are a SPARQL/RDF expert connected to a Fuseki endpoint at {}{}\n\
                 The user asked: \"{text}\"\n\
                 Generate a SPARQL query or respond helpfully. \
                 If you generate a query, wrap it in ```sparql ... ``` fences.",
                self.fuseki_url, self.dataset
            );
            llm.complete(&prompt)
                .await
                .unwrap_or_else(|e| format!("LLM error: {e}"))
        } else {
            format!(
                "Provide a SPARQL query (SELECT/ASK/INSERT/DELETE) to execute against {}{}",
                self.fuseki_url, self.dataset
            )
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
impl Actor for FusekiAgent {
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

    async fn on_start(&mut self) -> Result<()> {
        self.state = ActorState::Running;
        tracing::info!(
            "[{}] Fuseki agent → {}{}",
            self.config.name,
            self.fuseki_url,
            self.dataset
        );
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                wactorz_mqtt::topics::spawn(&self.config.id),
                &serde_json::json!({
                    "agentId":   self.config.id,
                    "agentName": self.config.name,
                    "agentType": "fuseki",
                    "fusekiUrl": self.fuseki_url,
                    "dataset":   self.dataset,
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
        Ok(())
    }

    async fn handle_message(&mut self, message: Message) -> Result<()> {
        use wactorz_core::message::MessageType;
        let text = match &message.payload {
            MessageType::Text { content } => content.clone(),
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
                    "fusekiUrl": self.fuseki_url,
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
            .ok_or_else(|| anyhow::anyhow!("FusekiAgent already running"))?;
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
