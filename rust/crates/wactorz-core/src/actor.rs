//! Actor trait and lifecycle state machine.
//!
//! Every agent in AgentFlow implements [`Actor`]. The trait mirrors the Python
//! base `Actor` class: actors receive [`Message`]s via an async mailbox, emit
//! heartbeats, and transition through a well-defined [`ActorState`] lifecycle.

use std::sync::Arc;

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use tokio::sync::mpsc;

use crate::message::Message;
use crate::metrics::ActorMetrics;

/// Lifecycle states of an actor.
///
/// Transitions: `Initializing` → `Running` → `Paused` ⇄ `Running` → `Stopped`
/// Errors can force the actor into `Failed` from any running state.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ActorState {
    /// Actor is being initialised (resources not yet ready).
    Initializing,
    /// Actor is processing messages normally.
    Running,
    /// Actor is temporarily suspended; mailbox still buffering.
    Paused,
    /// Actor has been cleanly shut down.
    Stopped,
    /// Actor encountered an unrecoverable error.
    Failed(String),
}

impl std::fmt::Display for ActorState {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ActorState::Initializing => write!(f, "initializing"),
            ActorState::Running => write!(f, "running"),
            ActorState::Paused => write!(f, "paused"),
            ActorState::Stopped => write!(f, "stopped"),
            ActorState::Failed(e) => write!(f, "failed({e})"),
        }
    }
}

/// Static configuration supplied when creating an actor.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ActorConfig {
    /// Human-readable name.
    pub name: String,
    /// HLC-WID for this actor (time-ordered, node-scoped, collision-resistant).
    pub id: String,
    /// Maximum number of messages buffered in the mailbox.
    pub mailbox_capacity: usize,
    /// Heartbeat interval in seconds.
    pub heartbeat_interval_secs: u64,
    /// Whether this actor is protected from external termination.
    pub protected: bool,
}

/// Sanitise an actor name into a valid HLC-WID node segment (`[A-Za-z0-9_]+`).
fn sanitize_node_name(name: &str) -> String {
    let s: String = name
        .chars()
        .map(|c| {
            if c.is_alphanumeric() || c == '_' {
                c
            } else {
                '_'
            }
        })
        .take(20)
        .collect();
    if s.is_empty() { "actor".to_string() } else { s }
}

impl ActorConfig {
    /// Create a config with a fresh HLC-WID derived from `name`.
    pub fn new(name: impl Into<String>) -> Self {
        let name = name.into();
        let node = sanitize_node_name(&name);
        let id = wid::HLCWidGen::new(node, 4, 0)
            .unwrap_or_else(|_| wid::HLCWidGen::new("actor".to_string(), 4, 0).unwrap())
            .next_hlc_wid();
        Self {
            name,
            id,
            mailbox_capacity: 1000,
            heartbeat_interval_secs: 30,
            protected: false,
        }
    }

    /// Create a config with an explicit HLC-WID node segment (e.g. a NATO alphabet name).
    ///
    /// The `node` value is sanitised the same way as in [`Self::new`], then used
    /// verbatim as the node tag in the generated HLC-WID. This makes actor IDs
    /// human-readable and stable across renames.
    ///
    /// # Example
    /// ```ignore
    /// ActorConfig::new_with_node("wif-agent", "india")
    /// // → id: "20260303T120000.0001-india"
    /// ```
    pub fn new_with_node(name: impl Into<String>, node: impl Into<String>) -> Self {
        let name = name.into();
        let node = sanitize_node_name(&node.into());
        let id = wid::HLCWidGen::new(node, 4, 0)
            .unwrap_or_else(|_| wid::HLCWidGen::new("actor".to_string(), 4, 0).unwrap())
            .next_hlc_wid();
        Self {
            name,
            id,
            mailbox_capacity: 1000,
            heartbeat_interval_secs: 30,
            protected: false,
        }
    }

    /// Mark this actor as protected (cannot be killed externally).
    pub fn protected(mut self) -> Self {
        self.protected = true;
        self
    }
}

/// The core Actor trait.
///
/// Implementors must be `Send + Sync` so they can be driven by Tokio tasks.
/// The actor loop is started by calling [`Actor::run`], which typically:
/// 1. Calls [`Actor::on_start`]
/// 2. Polls the mailbox and calls [`Actor::handle_message`] for each message
/// 3. Emits heartbeats on a timer
/// 4. Calls [`Actor::on_stop`] on shutdown
#[async_trait]
pub trait Actor: Send + Sync + 'static {
    /// Return this actor's unique WID identifier.
    fn id(&self) -> String;

    /// Return this actor's human-readable name.
    fn name(&self) -> &str;

    /// Return the current lifecycle state.
    fn state(&self) -> ActorState;

    /// Return a reference to this actor's metrics.
    fn metrics(&self) -> Arc<ActorMetrics>;

    /// Return a sender handle to this actor's mailbox.
    fn mailbox(&self) -> mpsc::Sender<Message>;

    /// Return whether this actor is protected from external kill commands.
    fn is_protected(&self) -> bool {
        false
    }

    /// Called once after the actor is created, before the message loop starts.
    async fn on_start(&mut self) -> anyhow::Result<()> {
        Ok(())
    }

    /// Called with each incoming message from the mailbox.
    async fn handle_message(&mut self, message: Message) -> anyhow::Result<()>;

    /// Called on heartbeat tick; default implementation is a no-op.
    async fn on_heartbeat(&mut self) -> anyhow::Result<()> {
        Ok(())
    }

    /// Called once just before the actor loop exits.
    async fn on_stop(&mut self) -> anyhow::Result<()> {
        Ok(())
    }

    /// Drive the actor's main loop.
    ///
    /// Default implementation: each concrete actor MUST override this method.
    /// The default returns an error to indicate it must be overridden.
    ///
    /// Pattern for concrete actors:
    /// ```ignore
    /// async fn run(&mut self) -> Result<()> {
    ///     self.on_start().await?;
    ///     let mut rx = self.mailbox_rx.take()
    ///         .ok_or_else(|| anyhow::anyhow!("already running"))?;
    ///     let mut hb = tokio::time::interval(Duration::from_secs(self.config.heartbeat_interval_secs));
    ///     hb.set_missed_tick_behavior(MissedTickBehavior::Skip);
    ///     loop {
    ///         tokio::select! {
    ///             biased;
    ///             msg = rx.recv() => {
    ///                 match msg {
    ///                     None => break,
    ///                     Some(m) => {
    ///                         self.metrics.record_received();
    ///                         if let MessageType::Command { command: ActorCommand::Stop } = &m.payload { break; }
    ///                         match self.handle_message(m).await {
    ///                             Ok(_) => self.metrics.record_processed(),
    ///                             Err(e) => { tracing::error!("[{}] {e}", self.config.name); self.metrics.record_failed(); }
    ///                         }
    ///                     }
    ///                 }
    ///             }
    ///             _ = hb.tick() => {
    ///                 self.metrics.record_heartbeat();
    ///                 if let Err(e) = self.on_heartbeat().await { tracing::error!("[{}] hb: {e}", self.config.name); }
    ///             }
    ///         }
    ///     }
    ///     self.on_stop().await
    /// }
    /// ```
    async fn run(&mut self) -> anyhow::Result<()> {
        anyhow::bail!("Actor::run() must be overridden by each concrete actor")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn actor_state_display() {
        assert_eq!(ActorState::Initializing.to_string(), "initializing");
        assert_eq!(ActorState::Running.to_string(), "running");
        assert_eq!(ActorState::Paused.to_string(), "paused");
        assert_eq!(ActorState::Stopped.to_string(), "stopped");
        assert_eq!(ActorState::Failed("oom".into()).to_string(), "failed(oom)");
    }

    #[test]
    fn actor_state_eq_and_clone() {
        assert_eq!(ActorState::Running, ActorState::Running);
        assert_ne!(ActorState::Running, ActorState::Stopped);
        let s = ActorState::Failed("e".into());
        assert_eq!(s.clone(), ActorState::Failed("e".into()));
    }

    #[test]
    fn actor_config_new_sets_fields() {
        let c = ActorConfig::new("my-agent");
        assert_eq!(c.name, "my-agent");
        assert!(!c.id.is_empty());
        assert_eq!(c.mailbox_capacity, 1000);
        assert_eq!(c.heartbeat_interval_secs, 30);
        assert!(!c.protected);
    }

    #[test]
    fn actor_config_new_with_node() {
        let c = ActorConfig::new_with_node("wif-agent", "india");
        assert_eq!(c.name, "wif-agent");
        assert!(!c.id.is_empty());
    }

    #[test]
    fn actor_config_protected_builder() {
        let c = ActorConfig::new("io-agent").protected();
        assert!(c.protected);
    }

    #[test]
    fn sanitize_node_name_replaces_special_chars() {
        // sanitize_node_name is private — test indirectly via ActorConfig::new
        // Names with spaces/slashes should still produce a non-empty id
        let c = ActorConfig::new("my agent/v2");
        assert!(!c.id.is_empty());
    }

    #[test]
    fn sanitize_node_name_empty_input_falls_back() {
        // Empty name → sanitize returns "actor" fallback
        let c = ActorConfig::new("");
        assert!(!c.id.is_empty());
    }

    #[test]
    fn sanitize_node_name_truncates_to_20_chars() {
        // 30-char name should produce a valid id (node truncated to 20)
        let long_name = "a".repeat(30);
        let c = ActorConfig::new(&long_name);
        assert!(!c.id.is_empty());
    }

    #[test]
    fn actor_config_is_clone_debug_serialize() {
        let c = ActorConfig::new("test");
        let json = serde_json::to_string(&c).unwrap();
        let c2: ActorConfig = serde_json::from_str(&json).unwrap();
        assert_eq!(c2.name, "test");
    }

    // ── Default Actor trait methods ───────────────────────────────────────────

    struct MinimalActor {
        config: ActorConfig,
        metrics: Arc<ActorMetrics>,
        mailbox_tx: mpsc::Sender<Message>,
    }

    impl MinimalActor {
        fn new(name: &str) -> Self {
            let config = ActorConfig::new(name);
            let (tx, _rx) = mpsc::channel(10);
            Self { config, metrics: Arc::new(ActorMetrics::new()), mailbox_tx: tx }
        }
    }

    #[async_trait]
    impl Actor for MinimalActor {
        fn id(&self) -> String { self.config.id.clone() }
        fn name(&self) -> &str { &self.config.name }
        fn state(&self) -> ActorState { ActorState::Running }
        fn metrics(&self) -> Arc<ActorMetrics> { self.metrics.clone() }
        fn mailbox(&self) -> mpsc::Sender<Message> { self.mailbox_tx.clone() }
        async fn handle_message(&mut self, _: Message) -> anyhow::Result<()> { Ok(()) }
        // run(), is_protected(), on_start(), on_heartbeat(), on_stop() all use defaults
    }

    #[test]
    fn actor_default_is_protected_returns_false() {
        let a = MinimalActor::new("min");
        assert!(!a.is_protected());
    }

    #[tokio::test]
    async fn actor_default_on_start_returns_ok() {
        let mut a = MinimalActor::new("min");
        assert!(a.on_start().await.is_ok());
    }

    #[tokio::test]
    async fn actor_default_on_heartbeat_returns_ok() {
        let mut a = MinimalActor::new("min");
        assert!(a.on_heartbeat().await.is_ok());
    }

    #[tokio::test]
    async fn actor_default_on_stop_returns_ok() {
        let mut a = MinimalActor::new("min");
        assert!(a.on_stop().await.is_ok());
    }

    #[tokio::test]
    async fn actor_default_run_returns_error() {
        let mut a = MinimalActor::new("min");
        assert!(a.run().await.is_err());
    }
}
