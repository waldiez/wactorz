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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn message_text_shorthand() {
        let m = Message::text(Some("a".into()), Some("b".into()), "hello");
        assert_eq!(m.from.as_deref(), Some("a"));
        assert_eq!(m.to.as_deref(), Some("b"));
        assert!(matches!(m.payload, MessageType::Text { ref content } if content == "hello"));
        assert!(!m.id.is_empty());
        assert!(m.timestamp_ms > 0);
    }

    #[test]
    fn message_command_shorthand() {
        let m = Message::command("actor-1".into(), ActorCommand::Stop);
        assert!(m.from.is_none());
        assert_eq!(m.to.as_deref(), Some("actor-1"));
        assert!(matches!(
            m.payload,
            MessageType::Command { command: ActorCommand::Stop }
        ));
    }

    #[test]
    fn message_new_broadcast() {
        let m = Message::new(None, None, MessageType::Heartbeat { sequence: 7 });
        assert!(m.from.is_none());
        assert!(m.to.is_none());
        assert!(matches!(m.payload, MessageType::Heartbeat { sequence: 7 }));
    }

    #[test]
    fn actor_command_eq() {
        assert_eq!(ActorCommand::Stop, ActorCommand::Stop);
        assert_ne!(ActorCommand::Stop, ActorCommand::Pause);
        assert_eq!(ActorCommand::Resume, ActorCommand::Resume);
        assert_eq!(ActorCommand::Status, ActorCommand::Status);
    }

    #[test]
    fn alert_severity_eq() {
        assert_eq!(AlertSeverity::Info, AlertSeverity::Info);
        assert_ne!(AlertSeverity::Warning, AlertSeverity::Error);
        assert_eq!(AlertSeverity::Critical, AlertSeverity::Critical);
    }

    #[test]
    fn message_type_variants_are_clone_debug() {
        let variants = vec![
            MessageType::Text { content: "hi".into() },
            MessageType::Heartbeat { sequence: 1 },
            MessageType::Command { command: ActorCommand::Stop },
            MessageType::Alert {
                severity: AlertSeverity::Warning,
                message: "warn".into(),
                context: serde_json::json!({}),
            },
            MessageType::Task {
                task_id: "t1".into(),
                description: "do it".into(),
                payload: serde_json::json!(null),
            },
            MessageType::TaskResult {
                task_id: "t1".into(),
                success: true,
                result: serde_json::json!(42),
            },
            MessageType::SpawnRequest {
                agent_type: "llm".into(),
                agent_name: "bot".into(),
                config: serde_json::json!({}),
            },
            MessageType::SpawnResult {
                agent_name: "bot".into(),
                agent_id: "id-1".into(),
                success: false,
                error: Some("oops".into()),
            },
        ];
        for v in variants {
            let _c = v.clone();
            let _d = format!("{v:?}");
        }
    }

    #[test]
    fn message_type_serde_roundtrip() {
        let original = MessageType::Text { content: "roundtrip".into() };
        let json = serde_json::to_string(&original).unwrap();
        let decoded: MessageType = serde_json::from_str(&json).unwrap();
        assert!(matches!(decoded, MessageType::Text { ref content } if content == "roundtrip"));
    }
}
