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
    /// Number of supervisor-triggered restarts for this actor.
    pub restart_count: AtomicU64,
    /// Total LLM input tokens consumed by this actor.
    pub llm_input_tokens: AtomicU64,
    /// Total LLM output tokens produced by this actor.
    pub llm_output_tokens: AtomicU64,
    /// Total LLM cost in nano-USD (divide by 1_000_000_000 for USD).
    pub llm_cost_nano_usd: AtomicU64,
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

    pub fn record_restart(&self) {
        self.restart_count.fetch_add(1, Ordering::Relaxed);
    }

    /// Record LLM usage: token counts and cost (in nano-USD).
    pub fn record_llm_usage(&self, input_tokens: u64, output_tokens: u64, cost_nano_usd: u64) {
        self.llm_input_tokens
            .fetch_add(input_tokens, Ordering::Relaxed);
        self.llm_output_tokens
            .fetch_add(output_tokens, Ordering::Relaxed);
        self.llm_cost_nano_usd
            .fetch_add(cost_nano_usd, Ordering::Relaxed);
    }

    /// Snapshot current counters as a serializable struct.
    pub fn snapshot(&self) -> MetricsSnapshot {
        MetricsSnapshot {
            messages_received: self.messages_received.load(Ordering::Relaxed),
            messages_processed: self.messages_processed.load(Ordering::Relaxed),
            messages_failed: self.messages_failed.load(Ordering::Relaxed),
            heartbeats: self.heartbeats.load(Ordering::Relaxed),
            last_message_at: self.last_message_at.load(Ordering::Relaxed),
            restart_count: self.restart_count.load(Ordering::Relaxed),
            llm_input_tokens: self.llm_input_tokens.load(Ordering::Relaxed),
            llm_output_tokens: self.llm_output_tokens.load(Ordering::Relaxed),
            llm_cost_usd: self.llm_cost_nano_usd.load(Ordering::Relaxed) as f64 / 1_000_000_000.0,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn new_is_all_zeros() {
        let m = ActorMetrics::new();
        let s = m.snapshot();
        assert_eq!(s.messages_received, 0);
        assert_eq!(s.messages_processed, 0);
        assert_eq!(s.messages_failed, 0);
        assert_eq!(s.heartbeats, 0);
        assert_eq!(s.restart_count, 0);
        assert_eq!(s.llm_input_tokens, 0);
        assert_eq!(s.llm_output_tokens, 0);
        assert_eq!(s.llm_cost_usd, 0.0);
    }

    #[test]
    fn record_received_increments_and_sets_timestamp() {
        let m = ActorMetrics::new();
        m.record_received();
        m.record_received();
        let s = m.snapshot();
        assert_eq!(s.messages_received, 2);
        assert!(s.last_message_at > 0);
    }

    #[test]
    fn record_processed_increments() {
        let m = ActorMetrics::new();
        m.record_processed();
        assert_eq!(m.snapshot().messages_processed, 1);
    }

    #[test]
    fn record_failed_increments() {
        let m = ActorMetrics::new();
        m.record_failed();
        assert_eq!(m.snapshot().messages_failed, 1);
    }

    #[test]
    fn record_heartbeat_increments() {
        let m = ActorMetrics::new();
        m.record_heartbeat();
        m.record_heartbeat();
        assert_eq!(m.snapshot().heartbeats, 2);
    }

    #[test]
    fn record_restart_increments() {
        let m = ActorMetrics::new();
        m.record_restart();
        assert_eq!(m.snapshot().restart_count, 1);
    }

    #[test]
    fn record_llm_usage_accumulates() {
        let m = ActorMetrics::new();
        m.record_llm_usage(100, 50, 1_000_000);
        m.record_llm_usage(200, 100, 2_000_000);
        let s = m.snapshot();
        assert_eq!(s.llm_input_tokens, 300);
        assert_eq!(s.llm_output_tokens, 150);
        assert!((s.llm_cost_usd - 0.003).abs() < 1e-9);
    }

    #[test]
    fn snapshot_is_clone_and_debug() {
        let m = ActorMetrics::new();
        let s = m.snapshot();
        let _s2 = s.clone();
        let repr = format!("{s:?}");
        assert!(repr.contains("MetricsSnapshot"));
    }

    #[test]
    fn metrics_is_default() {
        let m = ActorMetrics::default();
        assert_eq!(m.snapshot().messages_received, 0);
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
    /// Number of supervisor restarts.
    pub restart_count: u64,
    /// Total LLM input tokens.
    pub llm_input_tokens: u64,
    /// Total LLM output tokens.
    pub llm_output_tokens: u64,
    /// Total LLM cost in USD.
    pub llm_cost_usd: f64,
}
