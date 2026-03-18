//! Main orchestrator actor.
//!
//! [`MainActor`] is the central LLM-powered orchestrator.  It:
//! 1. Receives user input and routes it to the appropriate agent
//! 2. Sends the full system context to its LLM backend
//! 3. Parses `<spawn>` blocks in the LLM's reply to dynamically create agents
//! 4. Is **protected** — it cannot be killed by external commands
//!
//! Spawn block format (JSON inside XML-like tags):
//! ```text
//! <spawn>
//! {
//!   "agent_type": "DynamicAgent",
//!   "agent_name": "data-fetcher",
//!   "script": "...",
//!   "description": "Fetches weather data"
//! }
//! </spawn>
//! ```

use anyhow::Result;
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tokio::sync::mpsc;

use wactorz_core::{
    Actor, ActorConfig, ActorMetrics, ActorState, ActorSystem, EventPublisher, Message,
};

use crate::llm_agent::{LlmAgent, LlmConfig};

fn default_agent_type() -> String {
    "DynamicAgent".into()
}

/// Parsed content of a `<spawn>` block.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SpawnDirective {
    #[serde(default = "default_agent_type")]
    pub agent_type: String,
    pub agent_name: String,
    pub script: Option<String>,
    pub description: Option<String>,
    pub config: Option<serde_json::Value>,
}

/// The central orchestrator.
pub struct MainActor {
    config: ActorConfig,
    llm: LlmAgent,
    llm_config: LlmConfig,
    system: ActorSystem,
    state: ActorState,
    metrics: Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher: Option<EventPublisher>,
}

impl MainActor {
    pub fn new(config: ActorConfig, llm_config: LlmConfig, system: ActorSystem) -> Self {
        let protected_config = ActorConfig {
            protected: true,
            ..config.clone()
        };
        let llm = LlmAgent::new(
            ActorConfig::new(format!("{}-llm", config.name)),
            llm_config.clone(),
        );
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config: protected_config,
            llm,
            llm_config,
            system,
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

    /// Parse all `<spawn>...</spawn>` blocks from an LLM response string.
    pub fn parse_spawn_blocks(response: &str) -> Vec<SpawnDirective> {
        let mut out = Vec::new();
        let mut rest = response;
        while let Some(open) = rest.find("<spawn>") {
            let after = &rest[open + 7..];
            if let Some(close) = after.find("</spawn>") {
                let json_str = after[..close].trim();
                match serde_json::from_str::<SpawnDirective>(json_str) {
                    Ok(d) => out.push(d),
                    Err(e) => tracing::warn!("Bad <spawn> block: {e}"),
                }
                rest = &after[close + 8..];
            } else {
                break;
            }
        }
        out
    }

    /// Execute a parsed spawn directive via the actor system.
    async fn execute_spawn(&self, directive: SpawnDirective) -> Result<()> {
        use crate::{DynamicAgent, MonitorAgent};

        let cfg = ActorConfig::new(&directive.agent_name);
        let agent_id = cfg.id.clone();
        let agent_name = cfg.name.clone();
        let agent_type = directive.agent_type.clone();

        let description = directive.description.unwrap_or_default();
        let actor: Box<dyn wactorz_core::Actor> = match directive.agent_type.as_str() {
            "DynamicAgent" | "dynamic" => {
                let script = directive.script.unwrap_or_default();
                let mut a =
                    DynamicAgent::new(cfg, script).with_llm(self.llm_config.clone(), description);
                if let Some(pub_) = &self.publisher {
                    a = a.with_publisher(pub_.clone());
                }
                Box::new(a)
            }
            "MonitorAgent" | "monitor" => {
                let a = MonitorAgent::new(cfg, self.system.clone());
                if let Some(pub_) = &self.publisher {
                    Box::new(a.with_publisher(pub_.clone()))
                } else {
                    Box::new(a)
                }
            }
            _ => {
                tracing::warn!(
                    "Unknown agent_type '{}', defaulting to DynamicAgent",
                    directive.agent_type
                );
                let mut a = DynamicAgent::new(cfg, String::new())
                    .with_llm(self.llm_config.clone(), description);
                if let Some(pub_) = &self.publisher {
                    a = a.with_publisher(pub_.clone());
                }
                Box::new(a)
            }
        };
        self.system.spawn_actor(actor).await?;
        tracing::info!("Spawned {} as {}", agent_name, agent_type);

        // Announce to frontend immediately (don't wait for the first heartbeat)
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                wactorz_mqtt::topics::spawn(&agent_id),
                &serde_json::json!({
                    "agentId":   agent_id,
                    "agentName": agent_name,
                    "agentType": agent_type,
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
        Ok(())
    }

    fn now_ms() -> u64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as u64
    }

    /// Build the system prompt describing all currently running agents.
    async fn build_system_prompt(&self) -> String {
        let actors = self.system.registry.list().await;
        let agent_list: Vec<String> = actors
            .iter()
            .map(|e| format!("- {} (id={}, state={})", e.name, e.id, e.state))
            .collect();
        format!(
            "You are the main orchestrator of the AgentFlow multi-agent system.\n\
             \n\
             Current agents:\n\
             {agents}\n\
             \n\
             To spawn a new agent, embed a JSON block using EXACTLY this format:\n\
             <spawn>\n\
             {{\n\
               \"agent_type\": \"DynamicAgent\",\n\
               \"agent_name\": \"my-agent\",\n\
               \"description\": \"What this agent does\",\n\
               \"script\": \"fn main(msg) {{ agent_log(\\\"got: \\\" + msg); \\\"ok\\\" }}\"\n\
             }}\n\
             </spawn>\n\
             \n\
             Rules:\n\
             - \"agent_type\" must be exactly \"DynamicAgent\" or \"MonitorAgent\" (required)\n\
             - \"agent_name\" must be lowercase-hyphenated, no spaces (required)\n\
             - \"script\" MUST define a Rhai function: fn main(msg) {{ ... }}\n\
               `msg` is the PLAIN TEXT the user typed (e.g. \"3 / 4\" or \"hello\").\n\
               The function must return a string (the reply to send back).\n\
               Available API calls inside the script:\n\
                 agent_log(text)              — log a message\n\
                 agent_alert(text)            — broadcast an alert\n\
                 agent_state_get(key) → value — read persistent state\n\
                 agent_state_set(key, value)  — write persistent state\n\
             - Example math agent script:\n\
               \"fn main(msg) {{ let expr = msg.trim(); let result = eval(expr); \\\"= \\\" + result.to_string() }}\"\n\
             - Example echo agent: \"fn main(msg) {{ \\\"Echo: \\\" + msg }}\"\n\
             - Example counter agent:\n\
               \"fn main(msg) {{ let n = agent_state_get(\\\"count\\\"); let c = if n == () {{ 0 }} else {{ n }}; agent_state_set(\\\"count\\\", c + 1); \\\"Count: \\\" + (c + 1).to_string() }}\"\n\
             - Respond conversationally; include <spawn> blocks ONLY when the user explicitly asks to create a new agent.",
            agents = agent_list.join("\n")
        )
    }
}

#[async_trait]
impl Actor for MainActor {
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
                    "agentType": "orchestrator",
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
        Ok(())
    }

    async fn handle_message(&mut self, message: Message) -> Result<()> {
        use wactorz_core::message::MessageType;
        let user_text = match &message.payload {
            MessageType::Text { content } => content.clone(),
            MessageType::Task { description, .. } => description.clone(),
            _ => return Ok(()),
        };

        let system_prompt = self.build_system_prompt().await;
        let full_prompt = format!("{}\n\nUser: {}", system_prompt, user_text);
        let response = self
            .llm
            .complete(&full_prompt)
            .await
            .unwrap_or_else(|e| format!("LLM error: {e}"));

        // Parse and execute any spawn directives
        let directives = Self::parse_spawn_blocks(&response);
        for dir in directives {
            if let Err(e) = self.execute_spawn(dir).await {
                tracing::error!("Spawn failed: {e}");
            }
        }

        // Publish response to MQTT chat topic
        if let Some(pub_) = &self.publisher {
            let msg_id = wid::HLCWidGen::new("msg".to_string(), 4, 0)
                .expect("HLCWidGen init")
                .next_hlc_wid();
            pub_.publish(
                wactorz_mqtt::topics::chat(&self.config.id),
                &serde_json::json!({
                    "id": msg_id,
                    "from": self.config.name,
                    "to": "user",
                    "content": response,
                    "timestampMs": std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .unwrap_or_default().as_millis() as u64,
                }),
            );
        }
        Ok(())
    }

    async fn on_heartbeat(&mut self) -> Result<()> {
        use std::sync::atomic::Ordering;
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                wactorz_mqtt::topics::heartbeat(&self.config.id),
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
        let mut rx = self
            .mailbox_rx
            .take()
            .ok_or_else(|| anyhow::anyhow!("MainActor already running"))?;
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
