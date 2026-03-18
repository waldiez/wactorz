//! Inter-actor message types.
//!
//! All communication between actors flows through typed [`Message`] values
//! delivered via async [`tokio::sync::mpsc`] channels (the mailbox).
//!
//! This mirrors the Python `dict`-based message protocol but adds compile-time
//! type safety via [`MessageType`].

use serde::{Deserialize, Serialize};

/// Discriminated union of all message payloads that actors exchange.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum MessageType {
    /// Plain text request/response.
    Text { content: String },

    /// Request the recipient to perform a task and reply.
    Task {
        task_id: String,
        description: String,
        /// Arbitrary JSON payload for the task.
        payload: serde_json::Value,
    },

    /// Reply to a previously received `Task`.
    TaskResult {
        task_id: String,
        success: bool,
        result: serde_json::Value,
    },

    /// Heartbeat / keep-alive signal.
    Heartbeat { sequence: u64 },

    /// Actor lifecycle command.
    Command { command: ActorCommand },

    /// Alert/error broadcast.
    Alert {
        severity: AlertSeverity,
        message: String,
        context: serde_json::Value,
    },

    /// Spawn request: ask an orchestrator to create a new agent.
    SpawnRequest {
        agent_type: String,
        agent_name: String,
        config: serde_json::Value,
    },

    /// Confirmation that a spawn completed.
    SpawnResult {
        agent_name: String,
        agent_id: String,
        success: bool,
        error: Option<String>,
    },
}

/// Commands that can be sent to any actor.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ActorCommand {
    /// Gracefully stop the actor.
    Stop,
    /// Temporarily pause message processing.
    Pause,
    /// Resume after a pause.
    Resume,
    /// Request an immediate status report.
    Status,
}

/// Severity levels for alert messages.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AlertSeverity {
    Info,
    Warning,
    Error,
    Critical,
}

/// A message envelope routed between actors.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Message {
    /// Unique WID for this message.
    pub id: String,
    /// Sender's actor WID (`None` for system-generated messages).
    pub from: Option<String>,
    /// Recipient actor WID (`None` means broadcast).
    pub to: Option<String>,
    /// Unix timestamp (milliseconds) when the message was created.
    pub timestamp_ms: u64,
    /// The actual payload.
    pub payload: MessageType,
}

impl Message {
    /// Construct a new message with a fresh WID and current timestamp.
    pub fn new(from: Option<String>, to: Option<String>, payload: MessageType) -> Self {
        let id = wid::HLCWidGen::new("msg".to_string(), 4, 0)
            .expect("HLCWidGen init failed")
            .next_hlc_wid();
        let timestamp_ms = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as u64;
        Self {
            id,
            from,
            to,
            timestamp_ms,
            payload,
        }
    }

    /// Shorthand: send a plain text message.
    pub fn text(from: Option<String>, to: Option<String>, content: impl Into<String>) -> Self {
        Self::new(
            from,
            to,
            MessageType::Text {
                content: content.into(),
            },
        )
    }

    /// Shorthand: send a command to a specific actor.
    pub fn command(to: String, cmd: ActorCommand) -> Self {
        Self::new(None, Some(to), MessageType::Command { command: cmd })
    }
}
