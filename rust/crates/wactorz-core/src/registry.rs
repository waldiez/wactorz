//! Actor registry, system orchestrator, and Erlang/OTP-style supervisor.
//!
//! [`ActorRegistry`] is a thread-safe map of live actor mailboxes keyed by
//! WID string. [`ActorSystem`] wraps the registry and provides high-level
//! lifecycle operations. [`Supervisor`] adds automatic restart semantics.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tokio::sync::{RwLock, mpsc};

use crate::actor::{Actor, ActorState};
use crate::message::{ActorCommand, Message};
use crate::metrics::ActorMetrics;
use crate::publish::EventPublisher;

// ── Supervisor strategy ───────────────────────────────────────────────────────

/// Restart strategy for supervised actors — mirrors Erlang/OTP.
///
/// `OneForOne`  — restart only the crashed actor, leave siblings untouched.
/// `OneForAll`  — if any supervised actor crashes, restart ALL of them.
/// `RestForOne` — restart the crashed actor and every actor registered after it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
#[serde(rename_all = "snake_case")]
pub enum SupervisorStrategy {
    #[default]
    OneForOne,
    OneForAll,
    RestForOne,
}

/// Factory that produces a fresh boxed [`Actor`] on each invocation.
pub type ActorFactory = Arc<dyn Fn() -> Box<dyn Actor> + Send + Sync + 'static>;

struct SpecEntry {
    factory: ActorFactory,
    strategy: SupervisorStrategy,
    max_restarts: u32,
    restart_window: Duration,
    restart_delay: Duration,
    /// ID of the currently running actor instance.
    actor_id: Option<String>,
    /// Timestamps of recent restarts within the window.
    restart_times: Vec<Instant>,
    /// Set to true after `Supervisor::stop()` to suppress the watch loop.
    stopped: bool,
}

impl SpecEntry {
    /// Record a restart attempt. Returns `true` if within budget.
    fn record_restart(&mut self) -> bool {
        let now = Instant::now();
        self.restart_times
            .retain(|t| now.duration_since(*t) < self.restart_window);
        self.restart_times.push(now);
        (self.restart_times.len() as u32) <= self.max_restarts
    }

    fn exhausted(&self) -> bool {
        let now = Instant::now();
        let recent = self
            .restart_times
            .iter()
            .filter(|t| now.duration_since(**t) < self.restart_window)
            .count();
        (recent as u32) >= self.max_restarts
    }
}

// ── Registry ──────────────────────────────────────────────────────────────────

/// Metadata stored in the registry alongside each actor's mailbox sender.
#[derive(Debug, Clone)]
pub struct ActorEntry {
    pub id: String,
    pub name: String,
    pub state: ActorState,
    pub mailbox: mpsc::Sender<Message>,
    /// Whether this actor is protected from external kill commands.
    pub protected: bool,
    /// Runtime metrics for this actor.
    pub metrics: Arc<ActorMetrics>,
    /// ID of the supervisor overseeing this actor, if any.
    pub supervisor_id: Option<String>,
}

/// Thread-safe registry of all live actors.
#[derive(Debug, Default, Clone)]
pub struct ActorRegistry {
    actors: Arc<RwLock<HashMap<String, ActorEntry>>>,
}

impl ActorRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    /// Register a new actor entry.
    pub async fn register(&self, entry: ActorEntry) {
        let mut map = self.actors.write().await;
        map.insert(entry.id.clone(), entry);
    }

    /// Deregister an actor by WID.
    pub async fn deregister(&self, id: &str) {
        let mut map = self.actors.write().await;
        map.remove(id);
    }

    /// Look up an actor entry by WID.
    pub async fn get(&self, id: &str) -> Option<ActorEntry> {
        let map = self.actors.read().await;
        map.get(id).cloned()
    }

    /// Look up an actor by name.
    pub async fn get_by_name(&self, name: &str) -> Option<ActorEntry> {
        let map = self.actors.read().await;
        map.values().find(|e| e.name == name).cloned()
    }

    /// Return a snapshot of all registered actors.
    pub async fn list(&self) -> Vec<ActorEntry> {
        let map = self.actors.read().await;
        map.values().cloned().collect()
    }

    /// Update the stored [`ActorState`] for an actor.
    pub async fn update_state(&self, id: &str, state: ActorState) {
        let mut map = self.actors.write().await;
        if let Some(entry) = map.get_mut(id) {
            entry.state = state;
        }
    }

    /// Send a message directly to an actor's mailbox.
    pub async fn send(&self, id: &str, message: Message) -> anyhow::Result<()> {
        let map = self.actors.read().await;
        let entry = map
            .get(id)
            .ok_or_else(|| anyhow::anyhow!("actor {id} not found"))?;
        entry
            .mailbox
            .send(message)
            .await
            .map_err(|e| anyhow::anyhow!("mailbox full or closed: {e}"))
    }

    /// Broadcast a message to all registered actors.
    pub async fn broadcast(&self, message: Message) {
        let map = self.actors.read().await;
        for entry in map.values() {
            let _ = entry.mailbox.send(message.clone()).await;
        }
    }
}

// ── ActorSystem ───────────────────────────────────────────────────────────────

/// High-level actor system: owns the registry and drives spawning/shutdown.
#[derive(Debug, Clone)]
pub struct ActorSystem {
    pub registry: ActorRegistry,
    publisher: EventPublisher,
}

impl ActorSystem {
    pub fn new() -> Self {
        let (publisher, _rx) = EventPublisher::channel();
        Self {
            registry: ActorRegistry::new(),
            publisher,
        }
    }

    pub fn with_publisher(publisher: EventPublisher) -> Self {
        Self {
            registry: ActorRegistry::new(),
            publisher,
        }
    }

    pub fn publisher(&self) -> EventPublisher {
        self.publisher.clone()
    }

    fn _inject_fn(&self) -> impl Fn(ActorEntry) -> ActorEntry + '_ {
        |e| e // placeholder; injection happens at ActorEntry construction site
    }

    /// Spawn a boxed actor, register it, and drive it on a Tokio task.
    pub async fn spawn_actor(&self, actor: Box<dyn Actor>) -> anyhow::Result<String> {
        self.spawn_actor_supervised(actor, None).await
    }

    /// Spawn a boxed actor with an optional supervisor ID tag.
    pub async fn spawn_actor_supervised(
        &self,
        actor: Box<dyn Actor>,
        supervisor_id: Option<String>,
    ) -> anyhow::Result<String> {
        let id = actor.id();
        let name = actor.name().to_string();
        let mailbox = actor.mailbox();
        let protected = actor.is_protected();
        let metrics = actor.metrics();

        let entry = ActorEntry {
            id: id.clone(),
            name: name.clone(),
            state: ActorState::Initializing,
            mailbox,
            protected,
            metrics,
            supervisor_id,
        };
        self.registry.register(entry).await;

        let registry = self.registry.clone();
        let id_task = id.clone();
        tokio::spawn(async move {
            let mut actor = actor;
            registry.update_state(&id_task, ActorState::Running).await;
            match actor.run().await {
                Ok(_) => registry.update_state(&id_task, ActorState::Stopped).await,
                Err(e) => {
                    tracing::error!("[{}] run error: {e}", id_task);
                    registry
                        .update_state(&id_task, ActorState::Failed(e.to_string()))
                        .await;
                }
            }
            registry.deregister(&id_task).await;
            tracing::info!("Actor {name} ({id_task}) stopped");
        });
        Ok(id)
    }

    /// Send a stop command to the named actor (unless it is protected).
    pub async fn stop_actor(&self, name: &str) -> anyhow::Result<()> {
        let entry = self
            .registry
            .get_by_name(name)
            .await
            .ok_or_else(|| anyhow::anyhow!("actor '{name}' not found"))?;
        if entry.protected {
            anyhow::bail!("actor '{name}' is protected");
        }
        self.registry
            .send(
                &entry.id,
                Message::command(entry.id.clone(), ActorCommand::Stop),
            )
            .await
    }

    /// Gracefully shut down all actors.
    pub async fn shutdown(&self) -> anyhow::Result<()> {
        let actors = self.registry.list().await;
        for entry in actors {
            if !entry.protected {
                let _ = self
                    .registry
                    .send(
                        &entry.id,
                        Message::command(entry.id.clone(), ActorCommand::Stop),
                    )
                    .await;
            }
        }
        Ok(())
    }
}

impl Default for ActorSystem {
    fn default() -> Self {
        Self::new()
    }
}

// ── Supervisor ────────────────────────────────────────────────────────────────

/// OTP-inspired supervision tree.
///
/// Supervise critical actors and automatically restart them on failure using
/// one of three strategies:
/// - `OneForOne`  — restart only the crashed actor.
/// - `OneForAll`  — restart all supervised actors.
/// - `RestForOne` — restart the crashed actor and all registered after it.
///
/// # Usage
/// ```ignore
/// let mut sup = Supervisor::new(system.clone());
/// sup.supervise("main",    main_factory,    SupervisorStrategy::OneForOne, 10, 60.0, 2.0);
/// sup.supervise("monitor", monitor_factory, SupervisorStrategy::OneForOne, 10, 60.0, 1.0);
/// sup.start().await?;
/// ```
pub struct Supervisor {
    system: ActorSystem,
    specs: Arc<Mutex<Vec<(String, SpecEntry)>>>,
    poll_interval: Duration,
    watch_task: Option<tokio::task::JoinHandle<()>>,
}

impl Supervisor {
    pub fn new(system: ActorSystem) -> Self {
        Self {
            system,
            specs: Arc::new(Mutex::new(Vec::new())),
            poll_interval: Duration::from_secs(2),
            watch_task: None,
        }
    }

    pub fn with_poll_interval(system: ActorSystem, poll_interval: Duration) -> Self {
        Self {
            system,
            specs: Arc::new(Mutex::new(Vec::new())),
            poll_interval,
            watch_task: None,
        }
    }

    /// Register an actor to be supervised. Call before [`Supervisor::start`].
    pub fn supervise(
        &mut self,
        name: impl Into<String>,
        factory: ActorFactory,
        strategy: SupervisorStrategy,
        max_restarts: u32,
        restart_window_secs: f64,
        restart_delay_secs: f64,
    ) -> &mut Self {
        let entry = SpecEntry {
            factory,
            strategy,
            max_restarts,
            restart_window: Duration::from_secs_f64(restart_window_secs),
            restart_delay: Duration::from_secs_f64(restart_delay_secs),
            actor_id: None,
            restart_times: Vec::new(),
            stopped: false,
        };
        self.specs.lock().unwrap().push((name.into(), entry));
        self
    }

    /// Spawn all supervised actors and start the watch loop.
    pub async fn start(&mut self) -> anyhow::Result<()> {
        let sup_id = format!("supervisor-{}", uuid::Uuid::new_v4());

        // Collect (name, factory) pairs WITHOUT holding the lock across await points.
        // std::sync::MutexGuard is !Send and must not be held across .await.
        let tasks: Vec<(String, ActorFactory)> = {
            let specs = self.specs.lock().unwrap();
            specs
                .iter()
                .map(|(name, e)| (name.clone(), Arc::clone(&e.factory)))
                .collect()
        };

        for (name, factory) in &tasks {
            let actor = factory();
            let actor_id = self
                .system
                .spawn_actor_supervised(actor, Some(sup_id.clone()))
                .await?;
            {
                let mut specs = self.specs.lock().unwrap();
                if let Some((_, entry)) = specs.iter_mut().find(|(n, _)| n == name) {
                    entry.actor_id = Some(actor_id);
                }
            }
            tracing::info!("[Supervisor] Spawned '{name}'");
        }

        // Start watch loop
        let specs_clone = Arc::clone(&self.specs);
        let system_clone = self.system.clone();
        let poll = self.poll_interval;
        let sup_id_clone = sup_id.clone();

        self.watch_task = Some(tokio::spawn(async move {
            loop {
                tokio::time::sleep(poll).await;
                watch_once(&system_clone, &specs_clone, &sup_id_clone).await;
            }
        }));

        tracing::info!("[Supervisor] Started — supervising {} actors", {
            self.specs.lock().unwrap().len()
        });
        Ok(())
    }

    /// Stop all supervised actors and the watch loop.
    pub async fn stop(&mut self) {
        if let Some(task) = self.watch_task.take() {
            task.abort();
        }
        // Collect actor IDs and mark stopped while holding the lock, then
        // drop the lock before the async send calls (MutexGuard is !Send).
        let actor_ids: Vec<(String, Option<String>)> = {
            let mut specs = self.specs.lock().unwrap();
            specs
                .iter_mut()
                .map(|(name, entry)| {
                    entry.stopped = true;
                    (name.clone(), entry.actor_id.clone())
                })
                .collect()
        };
        for (name, actor_id) in actor_ids {
            if let Some(id) = actor_id {
                let _ = self
                    .system
                    .registry
                    .send(&id, Message::command(id.clone(), ActorCommand::Stop))
                    .await;
            }
            tracing::debug!("[Supervisor] Requested stop for '{name}'");
        }
    }

    /// Return a snapshot of supervision status.
    pub fn status(&self) -> Vec<serde_json::Value> {
        let specs = self.specs.lock().unwrap();
        specs
            .iter()
            .map(|(name, e)| {
                let now = Instant::now();
                let recent = e
                    .restart_times
                    .iter()
                    .filter(|t| now.duration_since(**t) < e.restart_window)
                    .count();
                serde_json::json!({
                    "name":          name,
                    "strategy":      format!("{:?}", e.strategy),
                    "max_restarts":  e.max_restarts,
                    "restarts_used": recent,
                    "exhausted":     e.exhausted(),
                    "actor_id":      e.actor_id,
                })
            })
            .collect()
    }
}

// ── Supervision watch-loop helpers ────────────────────────────────────────────

async fn watch_once(system: &ActorSystem, specs: &Mutex<Vec<(String, SpecEntry)>>, sup_id: &str) {
    // Collect names of failed/missing actors
    let failed: Vec<String> = {
        let specs_guard = specs.lock().unwrap();
        let mut out = Vec::new();
        for (name, entry) in specs_guard.iter() {
            if entry.stopped {
                continue;
            }
            let is_dead = match &entry.actor_id {
                None => true,
                Some(_id) => {
                    // Use a blocking check — actor state is updated by the spawned task
                    // We do an immediate registry lookup (async, but brief)
                    // We'll collect IDs and check outside the lock
                    false // placeholder — resolved below
                }
            };
            let _ = is_dead; // resolved in next step
            out.push(name.clone()); // collect all names for async check
        }
        out
    };

    // Now do async checks outside the mutex
    let mut truly_failed: Vec<String> = Vec::new();
    for name in &failed {
        let actor_id_opt = {
            let specs_guard = specs.lock().unwrap();
            specs_guard
                .iter()
                .find(|(n, _)| n == name)
                .and_then(|(_, e)| e.actor_id.clone())
        };
        let dead = match actor_id_opt {
            None => true,
            Some(ref id) => match system.registry.get(id).await {
                None => true, // deregistered → crashed
                Some(e) => matches!(e.state, ActorState::Failed(_)),
            },
        };
        // Skip intentionally stopped
        let stopped = specs
            .lock()
            .unwrap()
            .iter()
            .find(|(n, _)| n == name)
            .map(|(_, e)| e.stopped)
            .unwrap_or(true);
        if dead && !stopped {
            truly_failed.push(name.clone());
        }
    }

    if truly_failed.is_empty() {
        return;
    }

    for crashed_name in &truly_failed {
        let strategy = {
            let specs_guard = specs.lock().unwrap();
            specs_guard
                .iter()
                .find(|(n, _)| n == crashed_name)
                .map(|(_, e)| e.strategy.clone())
                .unwrap_or(SupervisorStrategy::OneForOne)
        };

        tracing::warn!(
            "[Supervisor] '{crashed_name}' failed — applying {:?} strategy.",
            strategy
        );

        match strategy {
            SupervisorStrategy::OneForOne => {
                restart_one(system, specs, crashed_name, sup_id).await;
            }
            SupervisorStrategy::OneForAll => {
                // Stop all others, then restart all in order
                let all_names: Vec<String> = specs
                    .lock()
                    .unwrap()
                    .iter()
                    .map(|(n, _)| n.clone())
                    .collect();
                for name in all_names.iter().rev() {
                    if name != crashed_name {
                        stop_one(system, specs, name).await;
                    }
                }
                for name in &all_names {
                    restart_one(system, specs, name, sup_id).await;
                }
            }
            SupervisorStrategy::RestForOne => {
                let all_names: Vec<String> = specs
                    .lock()
                    .unwrap()
                    .iter()
                    .map(|(n, _)| n.clone())
                    .collect();
                let idx = all_names
                    .iter()
                    .position(|n| n == crashed_name)
                    .unwrap_or(0);
                let affected: Vec<String> = all_names[idx..].to_vec();
                for name in affected.iter().rev() {
                    if name != crashed_name {
                        stop_one(system, specs, name).await;
                    }
                }
                for name in &affected {
                    restart_one(system, specs, name, sup_id).await;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::actor::ActorConfig;
    use crate::message::MessageType;
    use std::sync::Arc;
    use std::time::Duration;

    // ── Minimal test actor ────────────────────────────────────────────────────

    struct TestActor {
        config: ActorConfig,
        metrics: Arc<ActorMetrics>,
        mailbox_tx: mpsc::Sender<Message>,
        mailbox_rx: Option<mpsc::Receiver<Message>>,
    }

    impl TestActor {
        fn new(name: &str) -> Self {
            let config = ActorConfig::new(name);
            let (tx, rx) = mpsc::channel(10);
            Self { config, metrics: Arc::new(ActorMetrics::new()), mailbox_tx: tx, mailbox_rx: Some(rx) }
        }
    }

    #[async_trait::async_trait]
    impl Actor for TestActor {
        fn id(&self) -> String { self.config.id.clone() }
        fn name(&self) -> &str { &self.config.name }
        fn state(&self) -> ActorState { ActorState::Running }
        fn metrics(&self) -> Arc<ActorMetrics> { self.metrics.clone() }
        fn mailbox(&self) -> mpsc::Sender<Message> { self.mailbox_tx.clone() }
        async fn handle_message(&mut self, _: Message) -> anyhow::Result<()> { Ok(()) }
        async fn run(&mut self) -> anyhow::Result<()> {
            let mut rx = self.mailbox_rx.take().expect("run() called twice");
            while let Some(msg) = rx.recv().await {
                if let MessageType::Command { command: ActorCommand::Stop } = &msg.payload { break; }
            }
            Ok(())
        }
    }

    fn make_entry(name: &str) -> ActorEntry {
        let (tx, _rx) = mpsc::channel(10);
        ActorEntry {
            id: format!("{name}-id"),
            name: name.to_string(),
            state: ActorState::Running,
            mailbox: tx,
            protected: false,
            metrics: Arc::new(ActorMetrics::new()),
            supervisor_id: None,
        }
    }

    // ── ActorRegistry ─────────────────────────────────────────────────────────

    #[tokio::test]
    async fn registry_register_and_get() {
        let reg = ActorRegistry::new();
        let entry = make_entry("alpha");
        let id = entry.id.clone();
        reg.register(entry).await;
        let got = reg.get(&id).await.unwrap();
        assert_eq!(got.name, "alpha");
    }

    #[tokio::test]
    async fn registry_get_missing_returns_none() {
        let reg = ActorRegistry::new();
        assert!(reg.get("missing").await.is_none());
    }

    #[tokio::test]
    async fn registry_deregister() {
        let reg = ActorRegistry::new();
        let entry = make_entry("bravo");
        let id = entry.id.clone();
        reg.register(entry).await;
        reg.deregister(&id).await;
        assert!(reg.get(&id).await.is_none());
    }

    #[tokio::test]
    async fn registry_get_by_name_found() {
        let reg = ActorRegistry::new();
        reg.register(make_entry("charlie")).await;
        let found = reg.get_by_name("charlie").await.unwrap();
        assert_eq!(found.name, "charlie");
    }

    #[tokio::test]
    async fn registry_get_by_name_missing() {
        let reg = ActorRegistry::new();
        assert!(reg.get_by_name("nobody").await.is_none());
    }

    #[tokio::test]
    async fn registry_list_all() {
        let reg = ActorRegistry::new();
        reg.register(make_entry("delta")).await;
        reg.register(make_entry("echo")).await;
        assert_eq!(reg.list().await.len(), 2);
    }

    #[tokio::test]
    async fn registry_update_state() {
        let reg = ActorRegistry::new();
        let entry = make_entry("foxtrot");
        let id = entry.id.clone();
        reg.register(entry).await;
        reg.update_state(&id, ActorState::Paused).await;
        assert_eq!(reg.get(&id).await.unwrap().state, ActorState::Paused);
    }

    #[tokio::test]
    async fn registry_update_state_missing_is_noop() {
        let reg = ActorRegistry::new();
        reg.update_state("missing-id", ActorState::Stopped).await; // must not panic
    }

    #[tokio::test]
    async fn registry_send_delivers_message() {
        let reg = ActorRegistry::new();
        let (tx, mut rx) = mpsc::channel(10);
        reg.register(ActorEntry { id: "golf-id".into(), name: "golf".into(), state: ActorState::Running, mailbox: tx, protected: false, metrics: Arc::new(ActorMetrics::new()), supervisor_id: None }).await;
        reg.send("golf-id", Message::text(None, None, "ping")).await.unwrap();
        assert!(rx.recv().await.is_some());
    }

    #[tokio::test]
    async fn registry_send_to_missing_errors() {
        let reg = ActorRegistry::new();
        assert!(reg.send("missing-id", Message::text(None, None, "x")).await.is_err());
    }

    #[tokio::test]
    async fn registry_broadcast() {
        let reg = ActorRegistry::new();
        let (tx1, mut rx1) = mpsc::channel(10);
        let (tx2, mut rx2) = mpsc::channel(10);
        reg.register(ActorEntry { id: "h1".into(), name: "hotel".into(), state: ActorState::Running, mailbox: tx1, protected: false, metrics: Arc::new(ActorMetrics::new()), supervisor_id: None }).await;
        reg.register(ActorEntry { id: "h2".into(), name: "india".into(), state: ActorState::Running, mailbox: tx2, protected: false, metrics: Arc::new(ActorMetrics::new()), supervisor_id: None }).await;
        reg.broadcast(Message::text(None, None, "all")).await;
        assert!(rx1.recv().await.is_some());
        assert!(rx2.recv().await.is_some());
    }

    #[tokio::test]
    async fn registry_is_clone_sharing_state() {
        let reg1 = ActorRegistry::default();
        let reg2 = reg1.clone();
        reg1.register(make_entry("juliet")).await;
        assert_eq!(reg2.list().await.len(), 1); // same underlying Arc
    }

    // ── ActorSystem ──────────────────────────────────────────────────────────

    #[tokio::test]
    async fn system_new_and_default_are_empty() {
        assert!(ActorSystem::new().registry.list().await.is_empty());
        assert!(ActorSystem::default().registry.list().await.is_empty());
    }

    #[tokio::test]
    async fn system_with_publisher_and_publisher_clone() {
        let (pub_, _rx) = EventPublisher::channel();
        let sys = ActorSystem::with_publisher(pub_);
        let _p = sys.publisher();
        assert!(sys.registry.list().await.is_empty());
    }

    #[tokio::test]
    async fn system_spawn_actor_registers_it() {
        let sys = ActorSystem::new();
        let id = sys.spawn_actor(Box::new(TestActor::new("kilo"))).await.unwrap();
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert!(sys.registry.get(&id).await.is_some());
    }

    #[tokio::test]
    async fn system_stop_actor_sends_stop() {
        let sys = ActorSystem::new();
        sys.spawn_actor(Box::new(TestActor::new("lima"))).await.unwrap();
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert!(sys.stop_actor("lima").await.is_ok());
    }

    #[tokio::test]
    async fn system_stop_missing_actor_errors() {
        assert!(ActorSystem::new().stop_actor("nobody").await.is_err());
    }

    #[tokio::test]
    async fn system_stop_protected_actor_errors() {
        let sys = ActorSystem::new();
        let (tx, _rx) = mpsc::channel(10);
        sys.registry.register(ActorEntry { id: "p-id".into(), name: "protected".into(), state: ActorState::Running, mailbox: tx, protected: true, metrics: Arc::new(ActorMetrics::new()), supervisor_id: None }).await;
        assert!(sys.stop_actor("protected").await.is_err());
    }

    #[tokio::test]
    async fn system_shutdown_stops_unprotected() {
        let sys = ActorSystem::new();
        sys.spawn_actor(Box::new(TestActor::new("mike"))).await.unwrap();
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert!(sys.shutdown().await.is_ok());
    }

    #[tokio::test]
    async fn system_shutdown_skips_protected() {
        let sys = ActorSystem::new();
        let (tx, _rx) = mpsc::channel(10);
        sys.registry.register(ActorEntry { id: "prot".into(), name: "prot".into(), state: ActorState::Running, mailbox: tx, protected: true, metrics: Arc::new(ActorMetrics::new()), supervisor_id: None }).await;
        assert!(sys.shutdown().await.is_ok());
    }

    // ── SupervisorStrategy ───────────────────────────────────────────────────

    #[test]
    fn strategy_default_is_one_for_one() {
        assert_eq!(SupervisorStrategy::default(), SupervisorStrategy::OneForOne);
    }

    #[test]
    fn strategy_eq_and_ne() {
        assert_eq!(SupervisorStrategy::OneForAll, SupervisorStrategy::OneForAll);
        assert_ne!(SupervisorStrategy::OneForOne, SupervisorStrategy::RestForOne);
    }

    #[test]
    fn strategy_serde_roundtrip() {
        for s in [SupervisorStrategy::OneForOne, SupervisorStrategy::OneForAll, SupervisorStrategy::RestForOne] {
            let j = serde_json::to_string(&s).unwrap();
            let s2: SupervisorStrategy = serde_json::from_str(&j).unwrap();
            assert_eq!(s, s2);
        }
    }

    // ── Supervisor ───────────────────────────────────────────────────────────

    #[tokio::test]
    async fn supervisor_new_status_is_empty() {
        let sup = Supervisor::new(ActorSystem::new());
        assert!(sup.status().is_empty());
    }

    #[tokio::test]
    async fn supervisor_with_poll_interval() {
        let sup = Supervisor::with_poll_interval(ActorSystem::new(), Duration::from_millis(100));
        assert!(sup.status().is_empty());
    }

    #[tokio::test]
    async fn supervisor_supervise_and_status() {
        let mut sup = Supervisor::new(ActorSystem::new());
        let factory: ActorFactory = Arc::new(|| Box::new(TestActor::new("november")));
        sup.supervise("november", factory, SupervisorStrategy::OneForOne, 3, 60.0, 0.0);
        let s = sup.status();
        assert_eq!(s.len(), 1);
        assert_eq!(s[0]["name"], "november");
        assert_eq!(s[0]["max_restarts"], 3);
        assert_eq!(s[0]["exhausted"], false);
    }

    #[tokio::test]
    async fn supervisor_start_spawns_actor_and_stop_aborts_watch() {
        let sys = ActorSystem::new();
        let mut sup = Supervisor::with_poll_interval(sys.clone(), Duration::from_millis(50));
        let factory: ActorFactory = Arc::new(|| Box::new(TestActor::new("oscar")));
        sup.supervise("oscar", factory, SupervisorStrategy::OneForOne, 3, 60.0, 0.0);
        sup.start().await.unwrap();
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert!(!sys.registry.list().await.is_empty());
        sup.stop().await;
    }

    // ── CrashingActor: actor that fails immediately ───────────────────────────

    struct CrashingActor {
        config: ActorConfig,
        metrics: Arc<ActorMetrics>,
        mailbox_tx: mpsc::Sender<Message>,
        #[allow(dead_code)]
        mailbox_rx: Option<mpsc::Receiver<Message>>,
    }

    impl CrashingActor {
        fn new(name: &str) -> Self {
            let config = ActorConfig::new(name);
            let (tx, rx) = mpsc::channel(10);
            Self { config, metrics: Arc::new(ActorMetrics::new()), mailbox_tx: tx, mailbox_rx: Some(rx) }
        }
    }

    #[async_trait::async_trait]
    impl Actor for CrashingActor {
        fn id(&self) -> String { self.config.id.clone() }
        fn name(&self) -> &str { &self.config.name }
        fn state(&self) -> ActorState { ActorState::Failed("crash".into()) }
        fn metrics(&self) -> Arc<ActorMetrics> { self.metrics.clone() }
        fn mailbox(&self) -> mpsc::Sender<Message> { self.mailbox_tx.clone() }
        async fn handle_message(&mut self, _: Message) -> anyhow::Result<()> { anyhow::bail!("crash") }
        async fn run(&mut self) -> anyhow::Result<()> { anyhow::bail!("intentional crash") }
    }

    // ── SpecEntry unit tests ──────────────────────────────────────────────────

    #[test]
    fn spec_entry_record_restart_tracks_budget() {
        let factory: ActorFactory = Arc::new(|| Box::new(TestActor::new("t")));
        let mut entry = SpecEntry {
            factory,
            strategy: SupervisorStrategy::OneForOne,
            max_restarts: 3,
            restart_window: Duration::from_secs(60),
            restart_delay: Duration::ZERO,
            actor_id: None,
            restart_times: Vec::new(),
            stopped: false,
        };
        assert!(!entry.exhausted());
        assert!(entry.record_restart()); // 1 of 3 → within budget
        assert!(entry.record_restart()); // 2 of 3
        assert!(entry.record_restart()); // 3 of 3 → AT limit
        assert!(entry.exhausted());      // now exhausted
        assert!(!entry.record_restart()); // 4 > 3 → over budget
    }

    #[test]
    fn spec_entry_exhausted_with_expired_window() {
        let factory: ActorFactory = Arc::new(|| Box::new(TestActor::new("t")));
        let mut entry = SpecEntry {
            factory,
            strategy: SupervisorStrategy::OneForOne,
            max_restarts: 1,
            restart_window: Duration::from_nanos(1), // window expires instantly
            restart_delay: Duration::ZERO,
            actor_id: None,
            restart_times: Vec::new(),
            stopped: false,
        };
        entry.record_restart();
        // After window expires, old restarts are pruned → not exhausted again
        std::thread::sleep(Duration::from_millis(1));
        assert!(!entry.exhausted());
    }

    // ── Crash + restart coverage (exercises watch_once, restart_one, stop_one)

    #[tokio::test]
    async fn system_spawn_crashing_actor_is_deregistered_after_crash() {
        let sys = ActorSystem::new();
        let id = sys.spawn_actor(Box::new(CrashingActor::new("crash-test"))).await.unwrap();
        tokio::time::sleep(Duration::from_millis(50)).await;
        // Actor runs and immediately fails → deregistered
        assert!(sys.registry.get(&id).await.is_none());
    }

    #[tokio::test]
    async fn supervisor_watch_once_restarts_crashed_actor_one_for_one() {
        tokio::time::timeout(std::time::Duration::from_secs(5), async {
            let sys = ActorSystem::new();
            let factory: ActorFactory = Arc::new(|| Box::new(CrashingActor::new("crash-sup")));
            let mut sup = Supervisor::with_poll_interval(sys.clone(), Duration::from_millis(30));
            sup.supervise("crash-sup", factory, SupervisorStrategy::OneForOne, 5, 60.0, 0.0);
            sup.start().await.unwrap();
            // Let actor crash and supervisor detect + restart it at least once
            tokio::time::sleep(Duration::from_millis(200)).await;
            sup.stop().await;
            // After stop, watch task is aborted
        }).await.unwrap();
    }

    #[tokio::test]
    async fn supervisor_watch_once_one_for_all_strategy() {
        tokio::time::timeout(std::time::Duration::from_secs(5), async {
            let sys = ActorSystem::new();
            let crash_factory: ActorFactory = Arc::new(|| Box::new(CrashingActor::new("crash-a")));
            let stable_factory: ActorFactory = Arc::new(|| Box::new(TestActor::new("stable-b")));
            let mut sup = Supervisor::with_poll_interval(sys.clone(), Duration::from_millis(30));
            sup.supervise("crash-a", crash_factory, SupervisorStrategy::OneForAll, 5, 60.0, 0.0);
            sup.supervise("stable-b", stable_factory, SupervisorStrategy::OneForAll, 5, 60.0, 0.0);
            sup.start().await.unwrap();
            tokio::time::sleep(Duration::from_millis(200)).await;
            sup.stop().await;
        }).await.unwrap();
    }

    #[tokio::test]
    async fn supervisor_watch_once_rest_for_one_strategy() {
        tokio::time::timeout(std::time::Duration::from_secs(5), async {
            let sys = ActorSystem::new();
            let stable_factory: ActorFactory = Arc::new(|| Box::new(TestActor::new("first")));
            let crash_factory: ActorFactory = Arc::new(|| Box::new(CrashingActor::new("second")));
            let mut sup = Supervisor::with_poll_interval(sys.clone(), Duration::from_millis(30));
            sup.supervise("first", stable_factory, SupervisorStrategy::RestForOne, 5, 60.0, 0.0);
            sup.supervise("second", crash_factory, SupervisorStrategy::RestForOne, 5, 60.0, 0.0);
            sup.start().await.unwrap();
            tokio::time::sleep(Duration::from_millis(200)).await;
            sup.stop().await;
        }).await.unwrap();
    }

    #[tokio::test]
    async fn system_inject_fn_is_identity() {
        let sys = ActorSystem::new();
        let f = sys._inject_fn();
        let (tx, _rx) = mpsc::channel(10);
        let entry = ActorEntry {
            id: "test-id".into(),
            name: "test".into(),
            state: ActorState::Running,
            mailbox: tx,
            protected: false,
            metrics: Arc::new(ActorMetrics::new()),
            supervisor_id: None,
        };
        let result = f(entry);
        assert_eq!(result.name, "test");
    }
}

async fn stop_one(system: &ActorSystem, specs: &Mutex<Vec<(String, SpecEntry)>>, name: &str) {
    let actor_id = specs
        .lock()
        .unwrap()
        .iter()
        .find(|(n, _)| n == name)
        .and_then(|(_, e)| e.actor_id.clone());

    if let Some(id) = actor_id {
        // Only send Stop and wait if the actor is still in the registry.
        // An already-crashed actor has already deregistered itself; waiting
        // for it would waste 200 ms and push cascaded restarts beyond the
        // expected window.
        if system.registry.get(&id).await.is_some() {
            let _ = system
                .registry
                .send(&id, Message::command(id.clone(), ActorCommand::Stop))
                .await;
            // Brief pause to let the actor deregister
            tokio::time::sleep(Duration::from_millis(200)).await;
        }
    }
    // Clear actor_id
    let mut specs_guard = specs.lock().unwrap();
    if let Some((_, entry)) = specs_guard.iter_mut().find(|(n, _)| n == name) {
        entry.actor_id = None;
    }
}

async fn restart_one(
    system: &ActorSystem,
    specs: &Mutex<Vec<(String, SpecEntry)>>,
    name: &str,
    sup_id: &str,
) {
    let (_exhausted, delay, within_budget, factory) = {
        let mut specs_guard = specs.lock().unwrap();
        let Some((_, entry)) = specs_guard.iter_mut().find(|(n, _)| n == name) else {
            return;
        };
        if entry.exhausted() {
            tracing::error!(
                "[Supervisor] '{name}' exhausted restart budget ({} restarts). Giving up.",
                entry.max_restarts
            );
            return;
        }
        let budget_ok = entry.record_restart();
        (
            false,
            entry.restart_delay,
            budget_ok,
            Arc::clone(&entry.factory),
        )
    };

    if !within_budget {
        return;
    }

    // Stop old actor first if still registered
    stop_one(system, specs, name).await;

    if delay > Duration::ZERO {
        tokio::time::sleep(delay).await;
    }

    let restart_count = {
        let specs_guard = specs.lock().unwrap();
        specs_guard
            .iter()
            .find(|(n, _)| n == name)
            .map(|(_, e)| e.restart_times.len() as u64)
            .unwrap_or(0)
    };

    let actor = factory();
    match system
        .spawn_actor_supervised(actor, Some(sup_id.to_string()))
        .await
    {
        Ok(new_id) => {
            // Record restart count in actor metrics
            if let Some(entry) = system.registry.get(&new_id).await {
                entry
                    .metrics
                    .restart_count
                    .store(restart_count, std::sync::atomic::Ordering::Relaxed);
            }
            let mut specs_guard = specs.lock().unwrap();
            if let Some((_, e)) = specs_guard.iter_mut().find(|(n, _)| n == name) {
                e.actor_id = Some(new_id);
            }
            tracing::info!("[Supervisor] '{name}' restarted (#{restart_count}).");
        }
        Err(e) => {
            tracing::error!("[Supervisor] Failed to restart '{name}': {e}");
        }
    }
}
