//! Lightweight publish channel: actors post (topic, payload) tuples; the
//! server task bridges them to the real MQTT broker.

use serde::Serialize;
use tokio::sync::mpsc;

/// Cloneable sender handle used by actors to publish MQTT-like events.
#[derive(Clone, Debug)]
pub struct EventPublisher {
    tx: mpsc::UnboundedSender<(String, Vec<u8>)>,
}

impl EventPublisher {
    /// Create a linked (publisher, receiver) pair.
    pub fn channel() -> (Self, mpsc::UnboundedReceiver<(String, Vec<u8>)>) {
        let (tx, rx) = mpsc::unbounded_channel();
        (Self { tx }, rx)
    }

    /// Publish a serialisable value to `topic`.
    pub fn publish<T: Serialize>(&self, topic: impl Into<String>, payload: &T) {
        match serde_json::to_vec(payload) {
            Ok(bytes) => {
                let _ = self.tx.send((topic.into(), bytes));
            }
            Err(e) => tracing::warn!("EventPublisher serialize error: {e}"),
        }
    }

    /// Publish raw bytes.
    pub fn publish_raw(&self, topic: impl Into<String>, payload: Vec<u8>) {
        let _ = self.tx.send((topic.into(), payload));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn channel_publish_json() {
        let (pub_, mut rx) = EventPublisher::channel();
        pub_.publish("agents/abc/status", &serde_json::json!({"state": "running"}));
        let (topic, bytes) = rx.recv().await.unwrap();
        assert_eq!(topic, "agents/abc/status");
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["state"], "running");
    }

    #[tokio::test]
    async fn channel_publish_raw() {
        let (pub_, mut rx) = EventPublisher::channel();
        pub_.publish_raw("system/health", b"ok".to_vec());
        let (topic, bytes) = rx.recv().await.unwrap();
        assert_eq!(topic, "system/health");
        assert_eq!(bytes, b"ok");
    }

    #[tokio::test]
    async fn publisher_is_clone() {
        let (pub_, mut rx) = EventPublisher::channel();
        let pub2 = pub_.clone();
        pub2.publish_raw("t", b"x".to_vec());
        let (t, _) = rx.recv().await.unwrap();
        assert_eq!(t, "t");
    }

    #[test]
    fn publish_unserializable_does_not_panic() {
        // A channel with no receiver — send will silently fail
        let (pub_, rx) = EventPublisher::channel();
        drop(rx);
        // Should not panic even though the receiver is gone
        pub_.publish_raw("t", b"x".to_vec());
    }
}
