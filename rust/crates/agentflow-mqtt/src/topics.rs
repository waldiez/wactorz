//! Well-known MQTT topic constants and builder helpers.
//!
//! All AgentFlow topics follow one of two patterns:
//! - `agents/{agent_id}/{event}` — per-actor events
//! - `system/{event}` — system-wide broadcasts
//!
//! Use the builder functions to avoid string formatting errors in call sites.

/// Subscribe to all agent events (wildcard).
pub const AGENTS_ALL: &str = "agents/#";

/// System-wide health topic.
pub const SYSTEM_HEALTH: &str = "system/health";

/// System-wide shutdown topic.
pub const SYSTEM_SHUTDOWN: &str = "system/shutdown";

// ── Per-agent topic builders ──────────────────────────────────────────────────

/// `agents/{id}/heartbeat`
pub fn heartbeat(agent_id: &str) -> String {
    format!("agents/{agent_id}/heartbeat")
}

/// `agents/{id}/status`
pub fn status(agent_id: &str) -> String {
    format!("agents/{agent_id}/status")
}

/// `agents/{id}/logs`
pub fn logs(agent_id: &str) -> String {
    format!("agents/{agent_id}/logs")
}

/// `agents/{id}/alert`
pub fn alert(agent_id: &str) -> String {
    format!("agents/{agent_id}/alert")
}

/// `agents/{id}/commands` — topic on which the agent listens for commands.
pub fn commands(agent_id: &str) -> String {
    format!("agents/{agent_id}/commands")
}

/// `agents/{id}/result` — agent publishes task results here.
pub fn result(agent_id: &str) -> String {
    format!("agents/{agent_id}/result")
}

/// `agents/{id}/detections` — ML/monitoring agents publish detections here.
pub fn detections(agent_id: &str) -> String {
    format!("agents/{agent_id}/detections")
}

/// `agents/{id}/chat` — direct chat messages to/from an agent.
pub fn chat(agent_id: &str) -> String {
    format!("agents/{agent_id}/chat")
}

/// `agents/{id}/spawn` — agent announces its presence on startup.
pub fn spawn(agent_id: &str) -> String {
    format!("agents/{agent_id}/spawn")
}

/// `io/chat` — inbound messages from the UI gateway.
pub const IO_CHAT: &str = "io/chat";

// ── Parsing helpers ───────────────────────────────────────────────────────────

/// Extract `(agent_id, event)` from an `agents/{id}/{event}` topic.
///
/// Returns `None` if the topic does not match the expected pattern.
pub fn parse_agent_topic(topic: &str) -> Option<(&str, &str)> {
    let parts: Vec<&str> = topic.splitn(3, '/').collect();
    match parts.as_slice() {
        ["agents", id, event] => Some((id, event)),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn topic_builders_are_correct() {
        assert_eq!(heartbeat("abc"), "agents/abc/heartbeat");
        assert_eq!(commands("xyz"), "agents/xyz/commands");
    }

    #[test]
    fn parse_valid_agent_topic() {
        assert_eq!(
            parse_agent_topic("agents/abc-123/heartbeat"),
            Some(("abc-123", "heartbeat"))
        );
    }

    #[test]
    fn parse_invalid_topic_returns_none() {
        assert_eq!(parse_agent_topic("system/health"), None);
        assert_eq!(parse_agent_topic("agents/only-two"), None);
    }
}
