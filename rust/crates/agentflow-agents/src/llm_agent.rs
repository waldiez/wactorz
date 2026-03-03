//! LLM provider abstraction.
//!
//! [`LlmAgent`] wraps multiple large-language-model backends behind a single
//! async `complete()` interface.  Supported providers:
//! - **Anthropic** (`claude-*` models, Messages API)
//! - **OpenAI** (`gpt-*` and compatible, Chat Completions API)
//! - **Ollama** (local, OpenAI-compatible endpoint)
//!
//! The active provider and model are selected via [`LlmConfig`].

use anyhow::Result;
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tokio::sync::mpsc;

use agentflow_core::{Actor, ActorConfig, ActorMetrics, ActorState, EventPublisher, Message};

/// Supported LLM provider backends.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum LlmProvider {
    #[default]
    Anthropic,
    OpenAI,
    Ollama,
}

/// A single turn in a conversation (role + content).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatMessage {
    pub role: String,
    pub content: String,
}

/// Configuration for the LLM backend.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LlmConfig {
    pub provider: LlmProvider,
    /// Model name, e.g. `"claude-sonnet-4-6"`, `"gpt-4o"`, `"llama3"`.
    pub model: String,
    /// API key (Anthropic / OpenAI). Not needed for Ollama.
    pub api_key: Option<String>,
    /// Base URL override (useful for Ollama or proxies).
    pub base_url: Option<String>,
    /// Maximum tokens to generate.
    pub max_tokens: u32,
    /// Sampling temperature.
    pub temperature: f32,
    /// Optional system prompt.
    pub system_prompt: Option<String>,
}

impl Default for LlmConfig {
    fn default() -> Self {
        Self {
            provider: LlmProvider::Anthropic,
            model: "claude-sonnet-4-6".into(),
            api_key: None,
            base_url: None,
            max_tokens: 4096,
            temperature: 0.7,
            system_prompt: None,
        }
    }
}

/// An actor that calls an LLM provider and returns completions.
pub struct LlmAgent {
    pub(crate) config: ActorConfig,
    pub(crate) llm_config: LlmConfig,
    pub(crate) http: reqwest::Client,
    pub(crate) state: ActorState,
    pub(crate) metrics: Arc<ActorMetrics>,
    pub(crate) mailbox_tx: mpsc::Sender<Message>,
    pub(crate) mailbox_rx: Option<mpsc::Receiver<Message>>,
    /// Conversation history for multi-turn exchanges.
    pub(crate) history: Vec<ChatMessage>,
    pub(crate) publisher: Option<EventPublisher>,
}

impl LlmAgent {
    pub fn new(config: ActorConfig, llm_config: LlmConfig) -> Self {
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config,
            llm_config,
            http: reqwest::Client::new(),
            state: ActorState::Initializing,
            metrics: Arc::new(ActorMetrics::new()),
            mailbox_tx: tx,
            mailbox_rx: Some(rx),
            history: Vec::new(),
            publisher: None,
        }
    }

    /// Attach an EventPublisher for MQTT output.
    pub fn with_publisher(mut self, p: EventPublisher) -> Self {
        self.publisher = Some(p);
        self
    }

    /// Send a prompt to the configured LLM provider and return the completion.
    pub async fn complete(&self, prompt: &str) -> Result<String> {
        match self.llm_config.provider {
            LlmProvider::Anthropic => self.complete_anthropic(prompt).await,
            LlmProvider::OpenAI | LlmProvider::Ollama => self.complete_openai_compat(prompt).await,
        }
    }

    async fn complete_anthropic(&self, prompt: &str) -> Result<String> {
        let api_key = self
            .llm_config
            .api_key
            .as_deref()
            .ok_or_else(|| anyhow::anyhow!("LLM_API_KEY not set for Anthropic"))?;

        let mut messages = serde_json::json!([]);
        for m in &self.history {
            messages
                .as_array_mut()
                .unwrap()
                .push(serde_json::json!({"role": m.role, "content": m.content}));
        }
        messages
            .as_array_mut()
            .unwrap()
            .push(serde_json::json!({"role": "user", "content": prompt}));

        let mut body = serde_json::json!({
            "model": self.llm_config.model,
            "max_tokens": self.llm_config.max_tokens,
            "messages": messages,
        });
        if let Some(sys) = &self.llm_config.system_prompt {
            body["system"] = serde_json::Value::String(sys.clone());
        }

        let resp = self
            .http
            .post("https://api.anthropic.com/v1/messages")
            .header("x-api-key", api_key)
            .header("anthropic-version", "2023-06-01")
            .json(&body)
            .send()
            .await?;

        if !resp.status().is_success() {
            let s = resp.status();
            let t = resp.text().await.unwrap_or_default();
            anyhow::bail!("Anthropic {s}: {t}");
        }
        let json: serde_json::Value = resp.json().await?;
        Ok(json["content"][0]["text"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("unexpected Anthropic response: {json}"))?
            .to_string())
    }

    async fn complete_openai_compat(&self, prompt: &str) -> Result<String> {
        let base = self
            .llm_config
            .base_url
            .as_deref()
            .unwrap_or("https://api.openai.com/v1");

        let mut msgs = Vec::new();
        if let Some(sys) = &self.llm_config.system_prompt {
            msgs.push(serde_json::json!({"role": "system", "content": sys}));
        }
        for m in &self.history {
            msgs.push(serde_json::json!({"role": m.role, "content": m.content}));
        }
        msgs.push(serde_json::json!({"role": "user", "content": prompt}));

        let body = serde_json::json!({
            "model": self.llm_config.model,
            "messages": msgs,
            "max_tokens": self.llm_config.max_tokens,
            "temperature": self.llm_config.temperature,
        });

        let mut req = self
            .http
            .post(format!("{base}/chat/completions"))
            .json(&body);
        if let Some(key) = &self.llm_config.api_key {
            req = req.header("Authorization", format!("Bearer {key}"));
        }
        let resp = req.send().await?;
        if !resp.status().is_success() {
            let s = resp.status();
            let t = resp.text().await.unwrap_or_default();
            anyhow::bail!("OpenAI-compat {s}: {t}");
        }
        let json: serde_json::Value = resp.json().await?;
        Ok(json["choices"][0]["message"]["content"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("unexpected response: {json}"))?
            .to_string())
    }
}

#[async_trait]
impl Actor for LlmAgent {
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
        let prompt = match &message.payload {
            MessageType::Text { content } => content.clone(),
            MessageType::Task { description, .. } => description.clone(),
            _ => return Ok(()),
        };
        let reply_text = self.complete(&prompt).await?;
        // Store in history
        self.history.push(ChatMessage {
            role: "user".into(),
            content: prompt,
        });
        self.history.push(ChatMessage {
            role: "assistant".into(),
            content: reply_text.clone(),
        });
        // Reply to sender
        if let Some(sender_id) = message.from {
            tracing::debug!(
                "[{}] generated reply ({} chars)",
                self.config.name,
                reply_text.len()
            );
            let reply = Message::text(Some(self.config.id.clone()), Some(sender_id), reply_text);
            // Can't route back without registry; caller handles routing
            let _ = reply; // caller will pick this up via a shared channel in a full impl
        }
        Ok(())
    }

    async fn on_heartbeat(&mut self) -> Result<()> {
        use std::sync::atomic::Ordering;
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                agentflow_mqtt::topics::heartbeat(&self.config.id),
                &serde_json::json!({
                    "agentId": self.config.id,
                    "agentName": self.config.name,
                    "state": self.state,
                    "sequence": self.metrics.heartbeats.load(Ordering::Relaxed),
                    "timestampMs": std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .unwrap_or_default().as_millis() as u64,
                }),
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
            .ok_or_else(|| anyhow::anyhow!("LlmAgent already running"))?;
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
