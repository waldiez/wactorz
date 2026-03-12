//! OS Control Agent — translates AI intents into Synapse OS actions.
//!
//! MainActor delegates OS-related user requests here. This agent converts
//! them into well-known MQTT topics that `synapsd` (synapse-core) subscribes
//! to and executes:
//!
//! ```text
//! User: "open the terminal"
//!   → MainActor → OsAgent (Task)
//!     → EventPublisher  os/window/open  {"app_id":"terminal"}
//!       → synapsd → event bus → Tauri shell → openWindow("terminal")
//! ```
//!
//! # Published MQTT topics (os/*)
//!
//! | Topic              | Payload                              | Effect                  |
//! |--------------------|--------------------------------------|-------------------------|
//! | `os/window/open`   | `{"app_id":"<id>"}`                  | Open app window         |
//! | `os/window/close`  | `{"window_id":"<id>"}`               | Close specific window   |
//! | `os/window/focus`  | `{"app_id":"<id>"}`                  | Focus app window        |
//! | `os/scene/set`     | `{"scene":"<name>"}`                 | Switch Babylon.js scene |
//! | `os/paradigm/set`  | `{"paradigm":"<name>"}`              | Switch shell paradigm   |
//! | `os/notify`        | `{"title":"…","body":"…"}`           | Show notification       |
//! | `os/app/launch`    | `{"app":"<name>","args":["…"]}`      | Launch native app       |
//! | `os/volume/set`    | `{"level":0.0–1.0}`                  | Set system volume       |
//! | `os/session/lock`  | `{}`                                 | Lock the session        |

use std::collections::HashMap;
use std::sync::Arc;

use agentflow_core::message::ActorCommand;
use agentflow_core::{
    Actor, ActorConfig, ActorMetrics, ActorState, EventPublisher, Message, MessageType,
};
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::sync::mpsc;
use tracing::{error, info, warn};

// ── Intent → MQTT topic mapping ───────────────────────────────────────────────

/// Parsed OS intent from a task payload.
#[derive(Debug, Deserialize)]
#[serde(tag = "intent", rename_all = "snake_case")]
pub enum OsIntent {
    OpenWindow {
        app_id: String,
    },
    CloseWindow {
        window_id: Option<String>,
        app_id: Option<String>,
    },
    FocusWindow {
        app_id: String,
    },
    SetScene {
        scene: String,
    },
    SetParadigm {
        paradigm: String,
    },
    Notify {
        title: String,
        body: String,
    },
    LaunchApp {
        app: String,
        #[serde(default)]
        args: Vec<String>,
    },
    SetVolume {
        level: f32,
    },
    LockSession,
    /// Fallback — unknown intent forwarded as-is.
    Raw {
        topic: String,
        payload: Value,
    },
}

/// Response stored as the task result.
#[derive(Debug, Serialize)]
struct OsResult {
    success: bool,
    intent: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
}

// ── Agent ─────────────────────────────────────────────────────────────────────

pub struct OsAgent {
    config: ActorConfig,
    state: ActorState,
    metrics: Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher: Option<EventPublisher>,
    /// Simple log of recent actions (last 100).
    history: Vec<String>,
}

impl OsAgent {
    pub fn new(config: ActorConfig) -> Self {
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config,
            state: ActorState::Initializing,
            metrics: Arc::new(ActorMetrics::new()),
            mailbox_tx: tx,
            mailbox_rx: Some(rx),
            publisher: None,
            history: Vec::new(),
        }
    }

    pub fn with_publisher(mut self, p: EventPublisher) -> Self {
        self.publisher = Some(p);
        self
    }

    /// Execute an OS intent by publishing to the appropriate MQTT topic.
    fn execute(&mut self, intent: OsIntent) -> OsResult {
        let (topic, payload, tag): (&str, Value, String) = match &intent {
            OsIntent::OpenWindow { app_id } => (
                "os/window/open",
                serde_json::json!({ "app_id": app_id }),
                format!("open_window:{app_id}"),
            ),
            OsIntent::CloseWindow { window_id, app_id } => (
                "os/window/close",
                serde_json::json!({ "window_id": window_id, "app_id": app_id }),
                "close_window".into(),
            ),
            OsIntent::FocusWindow { app_id } => (
                "os/window/focus",
                serde_json::json!({ "app_id": app_id }),
                format!("focus:{app_id}"),
            ),
            OsIntent::SetScene { scene } => (
                "os/scene/set",
                serde_json::json!({ "scene": scene }),
                format!("scene:{scene}"),
            ),
            OsIntent::SetParadigm { paradigm } => (
                "os/paradigm/set",
                serde_json::json!({ "paradigm": paradigm }),
                format!("paradigm:{paradigm}"),
            ),
            OsIntent::Notify { title, body } => (
                "os/notify",
                serde_json::json!({ "title": title, "body": body }),
                format!("notify:{title}"),
            ),
            OsIntent::LaunchApp { app, args } => (
                "os/app/launch",
                serde_json::json!({ "app": app, "args": args }),
                format!("launch:{app}"),
            ),
            OsIntent::SetVolume { level } => (
                "os/volume/set",
                serde_json::json!({ "level": level }),
                format!("volume:{level}"),
            ),
            OsIntent::LockSession => (
                "os/session/lock",
                serde_json::json!({}),
                "lock_session".into(),
            ),
            OsIntent::Raw { topic, payload } => {
                if let Some(ref pub_) = self.publisher {
                    pub_.publish(topic.as_str(), payload);
                    let t = topic.clone();
                    self.record_history(format!("raw:{t}"));
                    return OsResult {
                        success: true,
                        intent: format!("raw:{t}"),
                        error: None,
                    };
                }
                return OsResult {
                    success: false,
                    intent: format!("raw:{topic}"),
                    error: Some("no publisher".into()),
                };
            }
        };

        info!("[os-agent] → {topic}: {payload}");

        if let Some(ref pub_) = self.publisher {
            pub_.publish(topic, &payload);
            self.record_history(tag.clone());
            OsResult {
                success: true,
                intent: tag,
                error: None,
            }
        } else {
            error!("[os-agent] no EventPublisher attached — cannot publish {topic}");
            OsResult {
                success: false,
                intent: tag,
                error: Some("no publisher".into()),
            }
        }
    }

    fn record_history(&mut self, entry: String) {
        self.history.push(entry);
        if self.history.len() > 100 {
            self.history.remove(0);
        }
    }

    /// Parse a task payload as an [`OsIntent`].
    fn parse_intent(payload: &Value) -> Option<OsIntent> {
        serde_json::from_value(payload.clone()).ok()
    }
}

#[async_trait]
impl Actor for OsAgent {
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

    async fn on_start(&mut self) -> anyhow::Result<()> {
        self.state = ActorState::Running;
        info!("[os-agent] started — ready for OS commands");
        Ok(())
    }

    async fn handle_message(&mut self, msg: Message) -> anyhow::Result<()> {
        self.metrics.record_received();

        match msg.payload {
            MessageType::Task {
                task_id,
                ref payload,
                ..
            } => {
                let result = match Self::parse_intent(payload) {
                    Some(intent) => self.execute(intent),
                    None => {
                        warn!("[os-agent] unrecognised intent payload: {payload}");
                        OsResult {
                            success: false,
                            intent: "unknown".into(),
                            error: Some(format!("cannot parse intent: {payload}")),
                        }
                    }
                };

                // Reply to sender if a reply address is set.
                if let Some(from) = msg.from {
                    let reply = Message::new(
                        Some(self.config.id.clone()),
                        Some(from),
                        MessageType::TaskResult {
                            task_id,
                            success: result.success,
                            result: serde_json::to_value(&result).unwrap_or_default(),
                        },
                    );
                    let _ = self.mailbox_tx.try_send(reply);
                }

                self.metrics.record_processed();
            }

            MessageType::Command { command } => match command {
                ActorCommand::Status => {
                    info!("[os-agent] status: {} actions recorded", self.history.len());
                }
                ActorCommand::Stop if !self.config.protected => {
                    self.state = ActorState::Stopped;
                }
                _ => {}
            },

            _ => {
                self.metrics.record_processed();
            }
        }

        Ok(())
    }

    async fn on_heartbeat(&mut self) -> anyhow::Result<()> {
        self.metrics.record_heartbeat();
        info!(
            "[os-agent] heartbeat — {} actions in history",
            self.history.len()
        );
        Ok(())
    }

    async fn on_stop(&mut self) -> anyhow::Result<()> {
        self.state = ActorState::Stopped;
        info!("[os-agent] stopped");
        Ok(())
    }

    async fn run(&mut self) -> anyhow::Result<()> {
        self.on_start().await?;

        let mut rx = self
            .mailbox_rx
            .take()
            .ok_or_else(|| anyhow::anyhow!("[os-agent] already running"))?;

        let mut interval = tokio::time::interval(std::time::Duration::from_secs(
            self.config.heartbeat_interval_secs,
        ));
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

        loop {
            if self.state == ActorState::Stopped {
                break;
            }

            tokio::select! {
                biased;
                msg = rx.recv() => {
                    match msg {
                        Some(m) => self.handle_message(m).await?,
                        None    => break,
                    }
                }
                _ = interval.tick() => {
                    self.on_heartbeat().await?;
                }
            }
        }

        self.on_stop().await
    }
}

// ── Intent registry ───────────────────────────────────────────────────────────
// Useful for MainActor's system prompt construction.

pub fn intent_descriptions() -> HashMap<&'static str, &'static str> {
    let mut m = HashMap::new();
    m.insert("open_window", r#"{"intent":"open_window","app_id":"<id>"}"#);
    m.insert(
        "close_window",
        r#"{"intent":"close_window","app_id":"<id>"}"#,
    );
    m.insert(
        "focus_window",
        r#"{"intent":"focus_window","app_id":"<id>"}"#,
    );
    m.insert(
        "set_scene",
        r#"{"intent":"set_scene","scene":"home|greeter|ambient|voice|launcher"}"#,
    );
    m.insert(
        "set_paradigm",
        r#"{"intent":"set_paradigm","paradigm":"win11|mobile|minimal|nebula"}"#,
    );
    m.insert("notify", r#"{"intent":"notify","title":"…","body":"…"}"#);
    m.insert(
        "launch_app",
        r#"{"intent":"launch_app","app":"firefox","args":[]}"#,
    );
    m.insert("set_volume", r#"{"intent":"set_volume","level":0.7}"#);
    m.insert("lock_session", r#"{"intent":"lock_session"}"#);
    m
}
