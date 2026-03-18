//! LLM-powered task planning agent.
//!
//! [`PlannerAgent`] decomposes multi-step goals into ordered action plans.
//! It emits each step as a structured MQTT message so other agents can
//! execute them sequentially or in parallel.

use anyhow::Result;
use async_trait::async_trait;
use std::sync::Arc;
use tokio::sync::mpsc;

use crate::llm_agent::{LlmAgent, LlmConfig};
use wactorz_core::{Actor, ActorConfig, ActorMetrics, ActorState, EventPublisher, Message};

const SYSTEM_PROMPT: &str = "\
You are a task planning expert. Given a goal, break it into clear, ordered steps.\n\
Output ONLY a numbered list, one step per line. Each step should be atomic and actionable.\n\
Example:\n\
1. Collect current weather data for the target city\n\
2. Retrieve the 5-day forecast\n\
3. Summarise findings in a user-friendly format\n\
Keep steps concise. Do not include explanation or preamble.";

pub struct PlannerAgent {
    config: ActorConfig,
    llm: LlmAgent,
    state: ActorState,
    metrics: Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher: Option<EventPublisher>,
}

impl PlannerAgent {
    pub fn new(config: ActorConfig, llm_config: LlmConfig) -> Self {
        let mut lc = llm_config;
        lc.system_prompt = Some(SYSTEM_PROMPT.to_string());
        let llm_cfg = ActorConfig::new(format!("{}-llm", config.name));
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config,
            llm: LlmAgent::new(llm_cfg, lc),
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

    /// Parse numbered-list plan response into a `Vec<String>`.
    fn parse_steps(text: &str) -> Vec<String> {
        text.lines()
            .filter_map(|line| {
                let l = line.trim();
                // Strip leading "1." / "1)" / "- " etc.
                let stripped = l
                    .trim_start_matches(|c: char| c.is_ascii_digit())
                    .trim_start_matches(['.', ')', ' '])
                    .trim();
                if stripped.is_empty() {
                    None
                } else {
                    Some(stripped.to_string())
                }
            })
            .collect()
    }

    fn now_ms() -> u64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as u64
    }
}

#[async_trait]
impl Actor for PlannerAgent {
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
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                wactorz_mqtt::topics::spawn(&self.config.id),
                &serde_json::json!({
                    "agentId":   self.config.id,
                    "agentName": self.config.name,
                    "agentType": "planner",
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
        Ok(())
    }

    async fn handle_message(&mut self, message: Message) -> Result<()> {
        use wactorz_core::message::MessageType;
        let goal = match &message.payload {
            MessageType::Text { content } => content.clone(),
            MessageType::Task { description, .. } => description.clone(),
            _ => return Ok(()),
        };

        let plan_text = self
            .llm
            .complete(&goal)
            .await
            .unwrap_or_else(|e| format!("LLM error: {e}"));

        let steps = Self::parse_steps(&plan_text);

        if let Some(pub_) = &self.publisher {
            // Publish the full plan as a chat message
            pub_.publish(
                wactorz_mqtt::topics::chat(&self.config.id),
                &serde_json::json!({
                    "from":        self.config.name,
                    "to":          message.from.as_deref().unwrap_or("user"),
                    "content":     plan_text,
                    "steps":       steps,
                    "goal":        goal,
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
        Ok(())
    }

    async fn on_heartbeat(&mut self) -> Result<()> {
        if let Some(pub_) = &self.publisher {
            let snap = self.llm.metrics().snapshot();
            pub_.publish(
                wactorz_mqtt::topics::heartbeat(&self.config.id),
                &serde_json::json!({
                    "agentId":         self.config.id,
                    "agentName":       self.config.name,
                    "state":           self.state,
                    "llmInputTokens":  snap.llm_input_tokens,
                    "llmOutputTokens": snap.llm_output_tokens,
                    "llmCostUsd":      snap.llm_cost_usd,
                    "timestampMs":     Self::now_ms(),
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
            .ok_or_else(|| anyhow::anyhow!("PlannerAgent already running"))?;
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
