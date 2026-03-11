//! ML inference base agent.
//!
//! [`MlAgent`] provides a base implementation for machine-learning agents that
//! run local inference (e.g. ONNX, PyTorch via candle, or HTTP microservices).
//!
//! Subclasses override [`MlAgent::infer`] to implement model-specific logic.
//! Results are published to the MQTT `agents/{id}/detections` topic.

use anyhow::Result;
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tokio::sync::mpsc;

use agentflow_core::{Actor, ActorConfig, ActorMetrics, ActorState, EventPublisher, Message};

/// A generic inference result (can be subclassed via config).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InferenceResult {
    /// Model-specific label/class.
    pub label: String,
    /// Confidence score in `[0.0, 1.0]`.
    pub confidence: f32,
    /// Arbitrary model-specific metadata.
    pub metadata: serde_json::Value,
}

/// Backend selection for the ML agent.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MlBackend {
    /// Remote HTTP microservice (POST JSON, receive JSON).
    HttpService { url: String },
    /// ONNX runtime (local file path to `.onnx` model).
    Onnx { model_path: String },
    /// candle (Rust-native PyTorch-like) — for future use.
    Candle { model_path: String },
}

impl Default for MlBackend {
    fn default() -> Self {
        MlBackend::HttpService {
            url: "http://localhost:5000/infer".into(),
        }
    }
}

/// Configuration for an ML agent.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MlConfig {
    pub backend: MlBackend,
    /// Confidence threshold below which results are discarded.
    pub confidence_threshold: f32,
    /// Maximum batch size for inference.
    pub batch_size: usize,
}

impl Default for MlConfig {
    fn default() -> Self {
        Self {
            backend: MlBackend::default(),
            confidence_threshold: 0.5,
            batch_size: 1,
        }
    }
}

/// Base ML inference actor.
pub struct MlAgent {
    config: ActorConfig,
    ml_config: MlConfig,
    http: reqwest::Client,
    state: ActorState,
    metrics: Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher: Option<EventPublisher>,
}

impl MlAgent {
    pub fn new(config: ActorConfig, ml_config: MlConfig) -> Self {
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config,
            ml_config,
            http: reqwest::Client::new(),
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

    /// Run inference on a raw input payload.
    ///
    /// Override this method in specialised ML agents.
    pub async fn infer(&self, input: &serde_json::Value) -> Result<Vec<InferenceResult>> {
        match &self.ml_config.backend {
            MlBackend::HttpService { url } => self.infer_http(url, input).await,
            MlBackend::Onnx { model_path } => {
                anyhow::bail!("ONNX backend not yet implemented (model: {model_path})")
            }
            MlBackend::Candle { model_path } => {
                anyhow::bail!("Candle backend not yet implemented (model: {model_path})")
            }
        }
    }

    async fn infer_http(
        &self,
        url: &str,
        input: &serde_json::Value,
    ) -> Result<Vec<InferenceResult>> {
        let resp = self.http.post(url).json(input).send().await?;
        if !resp.status().is_success() {
            let s = resp.status();
            let t = resp.text().await.unwrap_or_default();
            anyhow::bail!("ML service {s}: {t}");
        }
        let mut results: Vec<InferenceResult> = resp.json().await?;
        results.retain(|r| r.confidence >= self.ml_config.confidence_threshold);
        Ok(results)
    }
}

#[async_trait]
impl Actor for MlAgent {
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

    async fn handle_message(&mut self, message: Message) -> Result<()> {
        use agentflow_core::message::MessageType;
        let input = match &message.payload {
            MessageType::Task { payload, .. } => payload.clone(),
            MessageType::Text { content } => serde_json::Value::String(content.clone()),
            _ => return Ok(()),
        };
        let results = self.infer(&input).await?;
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                agentflow_mqtt::topics::detections(&self.config.id),
                &serde_json::json!({ "results": results }),
            );
        }
        Ok(())
    }

    async fn run(&mut self) -> Result<()> {
        self.on_start().await?;
        self.state = ActorState::Running;
        let mut rx = self
            .mailbox_rx
            .take()
            .ok_or_else(|| anyhow::anyhow!("MlAgent already running"))?;
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
