//! Device manual / documentation lookup agent.
//!
//! [`ManualAgent`] uses an LLM to answer questions about device manuals,
//! datasheets, and technical documentation.  It can also search a local
//! document store if one is configured.

use anyhow::Result;
use async_trait::async_trait;
use std::sync::Arc;
use tokio::sync::mpsc;

use wactorz_core::{Actor, ActorConfig, ActorMetrics, ActorState, EventPublisher, Message};
use crate::llm_agent::{LlmAgent, LlmConfig};

const SYSTEM_PROMPT: &str = "\
You are a technical documentation and device manual expert. \
You help users understand how to use, configure, and troubleshoot devices and software. \
When answering:\n\
- Cite specific manual sections or page numbers when you know them\n\
- Provide step-by-step instructions when applicable\n\
- Flag safety warnings prominently\n\
- If you don't know the answer with confidence, say so clearly\n\
- Suggest searching the official manufacturer documentation if needed";

pub struct ManualAgent {
    config:     ActorConfig,
    llm:        LlmAgent,
    state:      ActorState,
    metrics:    Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher:  Option<EventPublisher>,
}

impl ManualAgent {
    pub fn new(config: ActorConfig, llm_config: LlmConfig) -> Self {
        let mut lc = llm_config;
        lc.system_prompt = Some(SYSTEM_PROMPT.to_string());
        let llm_cfg = ActorConfig::new(format!("{}-llm", config.name));
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config,
            llm:        LlmAgent::new(llm_cfg, lc),
            state:      ActorState::Initializing,
            metrics:    Arc::new(ActorMetrics::new()),
            mailbox_tx: tx,
            mailbox_rx: Some(rx),
            publisher:  None,
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
}

#[async_trait]
impl Actor for ManualAgent {
    fn id(&self)      -> String { self.config.id.clone() }
    fn name(&self)    -> &str   { &self.config.name }
    fn state(&self)   -> ActorState { self.state.clone() }
    fn metrics(&self) -> Arc<ActorMetrics> { Arc::clone(&self.metrics) }
    fn mailbox(&self) -> mpsc::Sender<Message> { self.mailbox_tx.clone() }

    async fn on_start(&mut self) -> Result<()> {
        self.state = ActorState::Running;
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                wactorz_mqtt::topics::spawn(&self.config.id),
                &serde_json::json!({
                    "agentId":   self.config.id,
                    "agentName": self.config.name,
                    "agentType": "manual",
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

        let response = self.llm.complete(&text).await
            .unwrap_or_else(|e| format!("LLM error: {e}"));

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
        let mut rx = self.mailbox_rx.take()
            .ok_or_else(|| anyhow::anyhow!("ManualAgent already running"))?;
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
