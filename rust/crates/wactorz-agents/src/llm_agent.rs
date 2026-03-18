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

use wactorz_core::{Actor, ActorConfig, ActorMetrics, ActorState, EventPublisher, Message};

/// Supported LLM provider backends.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum LlmProvider {
    #[default]
    Anthropic,
    OpenAI,
    Ollama,
    /// Google Gemini (generativelanguage.googleapis.com).
    Gemini,
    /// NVIDIA NIM (integrate.api.nvidia.com) — OpenAI-compatible.
    /// Free tier: ~1000 API calls/month per model.
    Nim,
}

impl std::fmt::Display for LlmProvider {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            LlmProvider::Anthropic => write!(f, "anthropic"),
            LlmProvider::OpenAI => write!(f, "openai"),
            LlmProvider::Ollama => write!(f, "ollama"),
            LlmProvider::Gemini => write!(f, "gemini"),
            LlmProvider::Nim => write!(f, "nim"),
        }
    }
}

/// Per-model pricing in USD per 1M tokens.
fn pricing(model: &str) -> (f64, f64) {
    match model {
        m if m.starts_with("claude-sonnet-4-6") => (3.0, 15.0),
        m if m.starts_with("claude-haiku-4-5") => (0.8, 4.0),
        m if m.starts_with("claude-opus-4-6") => (15.0, 75.0),
        m if m.starts_with("gpt-4o-mini") => (0.15, 0.6),
        m if m.starts_with("gpt-4o") => (2.5, 10.0),
        m if m.starts_with("deepseek") => (0.27, 1.10),
        m if m.contains("llama-3.3-70b") => (0.39, 0.39),
        m if m.contains("llama-3.1-8b") => (0.10, 0.10),
        m if m.starts_with("gemini-2.0-flash") => (0.10, 0.40),
        m if m.starts_with("gemini-1.5-pro") => (1.25, 5.0),
        _ => (1.0, 3.0),
    }
}

/// Calculate cost in nano-USD from token counts and model name.
pub fn calc_cost_nano_usd(model: &str, input_tokens: u64, output_tokens: u64) -> u64 {
    let (in_price, out_price) = pricing(model);
    let cost_usd =
        (input_tokens as f64 * in_price + output_tokens as f64 * out_price) / 1_000_000.0;
    (cost_usd * 1_000_000_000.0) as u64
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
    /// Consecutive API errors since last success — WIK monitors this via MQTT.
    pub(crate) consecutive_errors: u32,
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
            consecutive_errors: 0,
        }
    }

    /// Attach an EventPublisher for MQTT output.
    pub fn with_publisher(mut self, p: EventPublisher) -> Self {
        self.publisher = Some(p);
        self
    }

    /// Send a prompt to the configured LLM provider and return the completion.
    /// Also records token usage and cost in the actor metrics.
    pub async fn complete(&self, prompt: &str) -> Result<String> {
        let (text, input_tok, output_tok) = match self.llm_config.provider {
            LlmProvider::Anthropic => self.complete_anthropic(prompt).await?,
            LlmProvider::OpenAI | LlmProvider::Ollama => {
                self.complete_openai_compat(prompt, None).await?
            }
            LlmProvider::Nim => {
                let base = "https://integrate.api.nvidia.com/v1";
                self.complete_openai_compat(prompt, Some(base)).await?
            }
            LlmProvider::Gemini => self.complete_gemini(prompt).await?,
        };
        let cost_nano = calc_cost_nano_usd(&self.llm_config.model, input_tok, output_tok);
        self.metrics
            .record_llm_usage(input_tok, output_tok, cost_nano);
        Ok(text)
    }

    fn now_ms() -> u64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as u64
    }

    /// Publish a provider error to `system/llm/error` so WIK can react.
    fn publish_llm_error(&self, error: &str) {
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                wactorz_mqtt::topics::SYSTEM_LLM_ERROR,
                &serde_json::json!({
                    "provider":          self.llm_config.provider.to_string(),
                    "model":             self.llm_config.model,
                    "error":             error,
                    "consecutiveErrors": self.consecutive_errors + 1,
                    "timestampMs":       Self::now_ms(),
                }),
            );
        }
    }

    /// Returns `(text, input_tokens, output_tokens)`.
    async fn complete_anthropic(&self, prompt: &str) -> Result<(String, u64, u64)> {
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
        let text = json["content"][0]["text"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("unexpected Anthropic response: {json}"))?
            .to_string();
        let input_tok = json["usage"]["input_tokens"].as_u64().unwrap_or(0);
        let output_tok = json["usage"]["output_tokens"].as_u64().unwrap_or(0);
        Ok((text, input_tok, output_tok))
    }

    /// Returns `(text, input_tokens, output_tokens)`.
    async fn complete_gemini(&self, prompt: &str) -> Result<(String, u64, u64)> {
        let api_key = self
            .llm_config
            .api_key
            .as_deref()
            .ok_or_else(|| anyhow::anyhow!("LLM_API_KEY not set for Gemini"))?;

        let model = &self.llm_config.model;
        let url = format!(
            "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent?key={}",
            model, api_key
        );

        let mut contents: Vec<serde_json::Value> = self
            .history
            .iter()
            .map(|m| {
                let role = if m.role == "assistant" {
                    "model"
                } else {
                    "user"
                };
                serde_json::json!({ "role": role, "parts": [{ "text": m.content }] })
            })
            .collect();
        contents.push(serde_json::json!({
            "role": "user",
            "parts": [{ "text": prompt }]
        }));

        let mut body = serde_json::json!({ "contents": contents });
        if let Some(sys) = &self.llm_config.system_prompt {
            body["systemInstruction"] = serde_json::json!({ "parts": [{ "text": sys }] });
        }

        let resp = self.http.post(&url).json(&body).send().await?;
        if !resp.status().is_success() {
            let s = resp.status();
            let t = resp.text().await.unwrap_or_default();
            anyhow::bail!("Gemini {s}: {t}");
        }
        let json: serde_json::Value = resp.json().await?;
        let text = json["candidates"][0]["content"]["parts"][0]["text"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("unexpected Gemini response: {json}"))?
            .to_string();
        let input_tok = json["usageMetadata"]["promptTokenCount"]
            .as_u64()
            .unwrap_or(0);
        let output_tok = json["usageMetadata"]["candidatesTokenCount"]
            .as_u64()
            .unwrap_or(0);
        Ok((text, input_tok, output_tok))
    }

    /// OpenAI-compatible endpoint (OpenAI, Ollama, NIM).
    /// `base_url_override` takes precedence over `llm_config.base_url`.
    /// Returns `(text, input_tokens, output_tokens)`.
    async fn complete_openai_compat(
        &self,
        prompt: &str,
        base_url_override: Option<&str>,
    ) -> Result<(String, u64, u64)> {
        let base = base_url_override
            .or(self.llm_config.base_url.as_deref())
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
            "model":       self.llm_config.model,
            "messages":    msgs,
            "max_tokens":  self.llm_config.max_tokens,
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
        let text = json["choices"][0]["message"]["content"]
            .as_str()
            .ok_or_else(|| anyhow::anyhow!("unexpected response: {json}"))?
            .to_string();
        let input_tok = json["usage"]["prompt_tokens"].as_u64().unwrap_or(0);
        let output_tok = json["usage"]["completion_tokens"].as_u64().unwrap_or(0);
        Ok((text, input_tok, output_tok))
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
        use wactorz_core::message::MessageType;

        // ── WIK hot-swap: task_id "wik/switch" carries new provider config ──────
        if let MessageType::Task {
            task_id, payload, ..
        } = &message.payload
            && task_id == "wik/switch"
        {
            let provider_str = payload
                .get("provider")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let new_provider = match provider_str {
                "anthropic" => LlmProvider::Anthropic,
                "openai" => LlmProvider::OpenAI,
                "gemini" => LlmProvider::Gemini,
                "ollama" => LlmProvider::Ollama,
                "nim" => LlmProvider::Nim,
                other => {
                    tracing::warn!(
                        "[{}] wik/switch: unknown provider '{other}'",
                        self.config.name
                    );
                    return Ok(());
                }
            };
            let reason = payload
                .get("reason")
                .and_then(|v| v.as_str())
                .unwrap_or("WIK switch");
            tracing::info!(
                "[{}] ⚡ provider switch: {} → {provider_str} ({reason})",
                self.config.name,
                self.llm_config.provider,
            );
            self.llm_config.provider = new_provider;
            if let Some(model) = payload.get("model").and_then(|v| v.as_str()) {
                self.llm_config.model = model.to_string();
            }
            if let Some(key) = payload.get("apiKey").and_then(|v| v.as_str()) {
                self.llm_config.api_key = Some(key.to_string());
            }
            if let Some(url) = payload.get("baseUrl").and_then(|v| v.as_str()) {
                self.llm_config.base_url = Some(url.to_string());
            }
            self.consecutive_errors = 0;
            return Ok(());
        }

        let prompt = match &message.payload {
            MessageType::Text { content } => content.clone(),
            MessageType::Task { description, .. } => description.clone(),
            _ => return Ok(()),
        };

        match self.complete(&prompt).await {
            Ok(reply_text) => {
                self.consecutive_errors = 0;
                self.history.push(ChatMessage {
                    role: "user".into(),
                    content: prompt,
                });
                self.history.push(ChatMessage {
                    role: "assistant".into(),
                    content: reply_text.clone(),
                });
                if let Some(sender_id) = message.from {
                    tracing::debug!(
                        "[{}] generated reply ({} chars)",
                        self.config.name,
                        reply_text.len()
                    );
                    let reply =
                        Message::text(Some(self.config.id.clone()), Some(sender_id), reply_text);
                    let _ = reply;
                }
            }
            Err(e) => {
                self.consecutive_errors += 1;
                let err_str = e.to_string();
                tracing::error!(
                    "[{}] LLM error (consecutive: {}) — {err_str}",
                    self.config.name,
                    self.consecutive_errors
                );
                self.publish_llm_error(&err_str);
                return Err(e);
            }
        }
        Ok(())
    }

    async fn on_heartbeat(&mut self) -> Result<()> {
        // use std::sync::atomic::Ordering;
        if let Some(pub_) = &self.publisher {
            let snap = self.metrics.snapshot();
            pub_.publish(
                wactorz_mqtt::topics::heartbeat(&self.config.id),
                &serde_json::json!({
                    "agentId":         self.config.id,
                    "agentName":       self.config.name,
                    "state":           self.state,
                    "provider":        self.llm_config.provider.to_string(),
                    "model":           self.llm_config.model,
                    "llmInputTokens":  snap.llm_input_tokens,
                    "llmOutputTokens": snap.llm_output_tokens,
                    "llmCostUsd":      snap.llm_cost_usd,
                    "restartCount":    snap.restart_count,
                    "sequence":        snap.heartbeats,
                    "timestampMs":     std::time::SystemTime::now()
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
                            if let wactorz_core::message::MessageType::Command {
                                command: wactorz_core::message::ActorCommand::Stop
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
