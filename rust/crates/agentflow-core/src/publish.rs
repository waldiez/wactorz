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
