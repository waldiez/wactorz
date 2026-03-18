//! Quality-Assurance / Safety monitor agent.
//!
//! [`QAAgent`] is a passive observer: it receives a copy of every chat
//! message flowing through the system and publishes a `system/qa-flag`
//! alert when a policy violation or malfunction is detected.
//!
//! **Checks performed (rule-based, no LLM):**
//!
//! *Content checks (every message):*
//! - Prompt-injection patterns in user messages
//! - Agent error bleed-through (`script error:`, `rhai error:`, `(no output)`, …)
//! - Raw JSON/data bleed (agent returned internal message structure)
//! - Possible PII (email-like patterns) in any direction
//!
//! *Temporal checks (on every heartbeat tick):*
//! - No-response tracking: if a user message is sent to an agent and no
//!   reply arrives within [`AGENT_RESPONSE_TIMEOUT_MS`], a `no-response`
//!   flag is raised — exactly what triggered the math-agent 45 s timeout.
//!
//! The agent does NOT block messages; it only annotates.

use anyhow::Result;
use async_trait::async_trait;
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::mpsc;

use wactorz_core::{Actor, ActorConfig, ActorMetrics, ActorState, EventPublisher, Message};

/// How long (ms) before a user→agent request with no reply is flagged.
const AGENT_RESPONSE_TIMEOUT_MS: u64 = 30_000;

pub struct QAAgent {
    config: ActorConfig,
    state: ActorState,
    metrics: Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher: Option<EventPublisher>,
    /// Pending user→agent requests awaiting a response.
    /// key = agent name (from `to` field or parsed @mention)
    /// value = (content excerpt, sent_at_ms)
    pending: HashMap<String, (String, u64)>,
}

impl QAAgent {
    pub fn new(config: ActorConfig) -> Self {
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config,
            state: ActorState::Initializing,
            metrics: Arc::new(ActorMetrics::new()),
            mailbox_tx: tx,
            mailbox_rx: Some(rx),
            publisher: None,
            pending: HashMap::new(),
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

    /// Inspect a chat message's fields and return `Some((category, severity))` if flagged.
    fn check_payload_fields(from: &str, content: &str) -> Option<(String, &'static str)> {
        if content.is_empty() {
            return None;
        }
        let lower = content.to_lowercase();

        // ── Prompt-injection (user → agent direction) ──────────────────────────
        if from == "user" || from.is_empty() {
            const INJECTION: &[&str] = &[
                "ignore previous instructions",
                "ignore your previous",
                "forget all previous",
                "forget your instructions",
                "you are now",
                "pretend you are",
                "act as if you are",
                "disregard all",
                "override your instructions",
                "new persona",
                "system prompt",
                "jailbreak",
                "dan mode",
            ];
            for pat in INJECTION {
                if lower.contains(pat) {
                    return Some((format!("prompt-injection (matched: {pat})"), "warning"));
                }
            }
        }

        // ── Agent error / malfunction (agent → user direction) ────────────────
        if from != "user" && !from.is_empty() {
            const ERRORS: &[&str] = &[
                "script error:",
                "llm error:",
                "rhai error:",
                "panicked at",
                "thread 'main' panicked",
                "(no output)", // DynamicAgent fallback when script returns nothing
                "script not compiled", // compile step was skipped or failed
            ];
            for pat in ERRORS {
                if lower.contains(pat) {
                    return Some((format!("agent-error-exposed ({pat})"), "error"));
                }
            }

            // Raw JSON / data bleed — agent returned internal message structure.
            // Heuristic: content is valid JSON object/array and longer than 20 chars.
            let trimmed = content.trim_start();
            if (trimmed.starts_with('{') || trimmed.starts_with('[')) && trimmed.len() > 20 {
                if serde_json::from_str::<serde_json::Value>(trimmed).is_ok() {
                    return Some(("raw-data-bleed".into(), "warning"));
                }
            }
        }

        // ── PII — email-like pattern (any direction) ───────────────────────────
        // Avoid false-positives on @mention tokens (word starts with @)
        for word in content.split_whitespace() {
            if word.starts_with('@') {
                continue;
            }
            if let Some(at_pos) = word.find('@') {
                let after = &word[at_pos + 1..];
                // email: has a dot, reasonable length, no slashes (not a URL fragment)
                if after.contains('.') && after.len() >= 4 && !after.contains('/') {
                    return Some(("pii-possible-email".into(), "info"));
                }
            }
        }

        None
    }

    fn publish_flag(&self, category: &str, severity: &str, from: &str, excerpt: &str) {
        if let Some(pub_) = &self.publisher {
            let snippet = if excerpt.len() > 80 {
                &excerpt[..80]
            } else {
                excerpt
            };
            pub_.publish(
                "system/qa-flag",
                &serde_json::json!({
                    "agentId":   self.config.id,
                    "agentName": self.config.name,
                    "from":      from,
                    "category":  category,
                    "severity":  severity,
                    "excerpt":   snippet,
                    "message":   format!("[QA/{category}] from={from}: {snippet}"),
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
    }
}

#[async_trait]
impl Actor for QAAgent {
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
                wactorz_mqtt::topics::spawn(&self.config.id),
                &serde_json::json!({
                    "agentId":   self.config.id,
                    "agentName": self.config.name,
                    "agentType": "guardian",
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
        Ok(())
    }

    async fn handle_message(&mut self, message: Message) -> Result<()> {
        use wactorz_core::message::MessageType;
        let payload_json = match &message.payload {
            MessageType::Text { content } => content.clone(),
            _ => return Ok(()),
        };

        // Parse the inner chat payload JSON once
        let val: serde_json::Value = match serde_json::from_str(&payload_json) {
            Ok(v) => v,
            Err(_) => return Ok(()),
        };
        let from = val.get("from").and_then(|v| v.as_str()).unwrap_or("");
        let to = val.get("to").and_then(|v| v.as_str()).unwrap_or("");
        let content = val.get("content").and_then(|v| v.as_str()).unwrap_or("");

        if content.is_empty() {
            return Ok(());
        }

        // ── No-response tracking ───────────────────────────────────────────────
        if from == "user" || from.is_empty() {
            // Determine the target agent:
            //   1. `to` field when it's a direct agent (not the io gateway)
            //   2. Fallback: @mention at the start of content
            let target = if !to.is_empty() && to != "io-agent" {
                Some(to.to_string())
            } else {
                content
                    .split_whitespace()
                    .next()
                    .filter(|w| w.starts_with('@'))
                    .map(|w| w[1..].to_string())
            };
            if let Some(agent_name) = target {
                let excerpt = if content.len() > 60 {
                    &content[..60]
                } else {
                    content
                };
                self.pending
                    .insert(agent_name, (excerpt.to_string(), Self::now_ms()));
            }
        } else if to == "user" || to.is_empty() {
            // Agent replying to the user — resolve the pending entry for this agent
            if !from.is_empty() {
                self.pending.remove(from);
            }
        }

        // ── Content checks ─────────────────────────────────────────────────────
        if let Some((category, severity)) = Self::check_payload_fields(from, content) {
            tracing::warn!("[QA] flag: {category} | from={from} | {:.60}", content);
            self.publish_flag(&category, severity, from, content);
        }

        Ok(())
    }

    async fn on_heartbeat(&mut self) -> Result<()> {
        // ── Check for unresponsive agents ──────────────────────────────────────
        let now = Self::now_ms();
        let stale: Vec<(String, String)> = self
            .pending
            .iter()
            .filter(|(_, (_, sent_at))| now.saturating_sub(*sent_at) >= AGENT_RESPONSE_TIMEOUT_MS)
            .map(|(agent, (excerpt, _))| (agent.clone(), excerpt.clone()))
            .collect();

        for (agent, excerpt) in stale {
            tracing::warn!("[QA] no-response: agent={agent} | excerpt={:.60}", excerpt);
            self.publish_flag("no-response", "warning", &agent, &excerpt);
            self.pending.remove(&agent);
        }

        // ── Publish heartbeat ──────────────────────────────────────────────────
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                wactorz_mqtt::topics::heartbeat(&self.config.id),
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
            .ok_or_else(|| anyhow::anyhow!("QAAgent already running"))?;
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
                        if let wactorz_core::message::MessageType::Command {
                            command: wactorz_core::message::ActorCommand::Stop
                        } = &m.payload { break; }
                        match self.handle_message(m).await {
                            Ok(_) => self.metrics.record_processed(),
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
