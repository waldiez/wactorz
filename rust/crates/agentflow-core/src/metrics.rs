//! Runtime telemetry for actors.
//!
//! [`ActorMetrics`] is a cheap `Arc`-wrapped, atomically updated counter set
//! that actors carry internally. The registry exposes these over MQTT/REST.

use std::sync::atomic::{AtomicU64, Ordering};

use serde::{Deserialize, Serialize};

/// Atomic runtime counters for an actor.
///
/// All fields use relaxed ordering because cross-thread ordering guarantees
/// are not required for telemetry — occasional skew is acceptable.
#[derive(Debug, Default)]
pub struct ActorMetrics {
    /// Total messages received since the actor started.
    pub messages_received: AtomicU64,
    /// Total messages successfully processed.
    pub messages_processed: AtomicU64,
    /// Total messages that raised an error during processing.
    pub messages_failed: AtomicU64,
    /// Number of heartbeat ticks emitted.
    pub heartbeats: AtomicU64,
    /// UNIX timestamp (seconds) of the last received message.
    pub last_message_at: AtomicU64,
}

impl ActorMetrics {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn record_received(&self) {
        self.messages_received.fetch_add(1, Ordering::Relaxed);
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        self.last_message_at.store(now, Ordering::Relaxed);
    }

    pub fn record_processed(&self) {
        self.messages_processed.fetch_add(1, Ordering::Relaxed);
    }

    pub fn record_failed(&self) {
        self.messages_failed.fetch_add(1, Ordering::Relaxed);
    }

    pub fn record_heartbeat(&self) {
        self.heartbeats.fetch_add(1, Ordering::Relaxed);
    }

    /// Snapshot current counters as a serializable struct.
    pub fn snapshot(&self) -> MetricsSnapshot {
        MetricsSnapshot {
            messages_received: self.messages_received.load(Ordering::Relaxed),
            messages_processed: self.messages_processed.load(Ordering::Relaxed),
            messages_failed: self.messages_failed.load(Ordering::Relaxed),
            heartbeats: self.heartbeats.load(Ordering::Relaxed),
            last_message_at: self.last_message_at.load(Ordering::Relaxed),
        }
    }
}

/// A point-in-time snapshot of [`ActorMetrics`] that is `Serialize`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MetricsSnapshot {
    pub messages_received: u64,
    pub messages_processed: u64,
    pub messages_failed: u64,
    pub heartbeats: u64,
    /// UNIX seconds of last message.
    pub last_message_at: u64,
}
