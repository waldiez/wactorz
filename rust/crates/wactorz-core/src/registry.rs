//! Actor registry, system orchestrator, and Erlang/OTP-style supervisor.
//!
//! [`ActorRegistry`] is a thread-safe map of live actor mailboxes keyed by
//! WID string. [`ActorSystem`] wraps the registry and provides high-level
//! lifecycle operations. [`Supervisor`] adds automatic restart semantics.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tokio::sync::{mpsc, RwLock};

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
    factory:        ActorFactory,
    strategy:       SupervisorStrategy,
    max_restarts:   u32,
    restart_window: Duration,
    restart_delay:  Duration,
    /// ID of the currently running actor instance.
    actor_id:       Option<String>,
    /// Timestamps of recent restarts within the window.
    restart_times:  Vec<Instant>,
    /// Set to true after `Supervisor::stop()` to suppress the watch loop.
    stopped:        bool,
}

impl SpecEntry {
    /// Record a restart attempt. Returns `true` if within budget.
    fn record_restart(&mut self) -> bool {
        let now = Instant::now();
        self.restart_times.retain(|t| now.duration_since(*t) < self.restart_window);
        self.restart_times.push(now);
        (self.restart_times.len() as u32) <= self.max_restarts
    }

    fn exhausted(&self) -> bool {
        let now = Instant::now();
        let recent = self.restart_times.iter()
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
        Self { registry: ActorRegistry::new(), publisher }
    }

    pub fn with_publisher(publisher: EventPublisher) -> Self {
        Self { registry: ActorRegistry::new(), publisher }
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
        let id       = actor.id();
        let name     = actor.name().to_string();
        let mailbox  = actor.mailbox();
        let protected = actor.is_protected();
        let metrics  = actor.metrics();

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

        let registry  = self.registry.clone();
        let id_task   = id.clone();
        tokio::spawn(async move {
            let mut actor = actor;
            registry.update_state(&id_task, ActorState::Running).await;
            match actor.run().await {
                Ok(_)  => registry.update_state(&id_task, ActorState::Stopped).await,
                Err(e) => {
                    tracing::error!("[{}] run error: {e}", id_task);
                    registry.update_state(&id_task, ActorState::Failed(e.to_string())).await;
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
            .send(&entry.id, Message::command(entry.id.clone(), ActorCommand::Stop))
            .await
    }

    /// Gracefully shut down all actors.
    pub async fn shutdown(&self) -> anyhow::Result<()> {
        let actors = self.registry.list().await;
        for entry in actors {
            if !entry.protected {
                let _ = self
                    .registry
                    .send(&entry.id, Message::command(entry.id.clone(), ActorCommand::Stop))
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
    system:       ActorSystem,
    specs:        Arc<Mutex<Vec<(String, SpecEntry)>>>,
    poll_interval: Duration,
    watch_task:   Option<tokio::task::JoinHandle<()>>,
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

    /// Register an actor to be supervised. Call before [`start`].
    pub fn supervise(
        &mut self,
        name:                impl Into<String>,
        factory:             ActorFactory,
        strategy:            SupervisorStrategy,
        max_restarts:        u32,
        restart_window_secs: f64,
        restart_delay_secs:  f64,
    ) -> &mut Self {
        let entry = SpecEntry {
            factory,
            strategy,
            max_restarts,
            restart_window: Duration::from_secs_f64(restart_window_secs),
            restart_delay:  Duration::from_secs_f64(restart_delay_secs),
            actor_id:       None,
            restart_times:  Vec::new(),
            stopped:        false,
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
            specs.iter().map(|(name, e)| (name.clone(), Arc::clone(&e.factory))).collect()
        };

        for (name, factory) in &tasks {
            let actor = factory();
            let actor_id = self.system
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
        let specs_clone  = Arc::clone(&self.specs);
        let system_clone = self.system.clone();
        let poll         = self.poll_interval;
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
        let mut specs = self.specs.lock().unwrap();
        for (name, entry) in specs.iter_mut() {
            entry.stopped = true;
            if let Some(id) = &entry.actor_id {
                let _ = self.system.registry
                    .send(id, Message::command(id.clone(), ActorCommand::Stop))
                    .await;
            }
            tracing::debug!("[Supervisor] Requested stop for '{name}'");
        }
    }

    /// Return a snapshot of supervision status.
    pub fn status(&self) -> Vec<serde_json::Value> {
        let specs = self.specs.lock().unwrap();
        specs.iter().map(|(name, e)| {
            let now = Instant::now();
            let recent = e.restart_times.iter()
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
        }).collect()
    }
}

// ── Supervision watch-loop helpers ────────────────────────────────────────────

async fn watch_once(
    system: &ActorSystem,
    specs:  &Mutex<Vec<(String, SpecEntry)>>,
    sup_id: &str,
) {
    // Collect names of failed/missing actors
    let failed: Vec<String> = {
        let specs_guard = specs.lock().unwrap();
        let mut out = Vec::new();
        for (name, entry) in specs_guard.iter() {
            if entry.stopped { continue; }
            let is_dead = match &entry.actor_id {
                None     => true,
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
            specs_guard.iter()
                .find(|(n, _)| n == name)
                .and_then(|(_, e)| e.actor_id.clone())
        };
        let dead = match actor_id_opt {
            None => true,
            Some(ref id) => match system.registry.get(id).await {
                None    => true, // deregistered → crashed
                Some(e) => matches!(e.state, ActorState::Failed(_)),
            }
        };
        // Skip intentionally stopped
        let stopped = specs.lock().unwrap().iter()
            .find(|(n, _)| n == name)
            .map(|(_, e)| e.stopped)
            .unwrap_or(true);
        if dead && !stopped {
            truly_failed.push(name.clone());
        }
    }

    if truly_failed.is_empty() { return; }

    for crashed_name in &truly_failed {
        let strategy = {
            let specs_guard = specs.lock().unwrap();
            specs_guard.iter()
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
                let all_names: Vec<String> = specs.lock().unwrap()
                    .iter().map(|(n, _)| n.clone()).collect();
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
                let all_names: Vec<String> = specs.lock().unwrap()
                    .iter().map(|(n, _)| n.clone()).collect();
                let idx = all_names.iter().position(|n| n == crashed_name).unwrap_or(0);
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

async fn stop_one(
    system: &ActorSystem,
    specs:  &Mutex<Vec<(String, SpecEntry)>>,
    name:   &str,
) {
    let actor_id = specs.lock().unwrap().iter()
        .find(|(n, _)| n == name)
        .and_then(|(_, e)| e.actor_id.clone());

    if let Some(id) = actor_id {
        // Only send Stop and wait if the actor is still in the registry.
        // An already-crashed actor has already deregistered itself; waiting
        // for it would waste 200 ms and push cascaded restarts beyond the
        // expected window.
        if system.registry.get(&id).await.is_some() {
            let _ = system.registry
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
    specs:  &Mutex<Vec<(String, SpecEntry)>>,
    name:   &str,
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
        (false, entry.restart_delay, budget_ok, Arc::clone(&entry.factory))
    };

    if !within_budget { return; }

    // Stop old actor first if still registered
    stop_one(system, specs, name).await;

    if delay > Duration::ZERO {
        tokio::time::sleep(delay).await;
    }

    let restart_count = {
        let specs_guard = specs.lock().unwrap();
        specs_guard.iter()
            .find(|(n, _)| n == name)
            .map(|(_, e)| e.restart_times.len() as u64)
            .unwrap_or(0)
    };

    let actor = factory();
    match system.spawn_actor_supervised(actor, Some(sup_id.to_string())).await {
        Ok(new_id) => {
            // Record restart count in actor metrics
            if let Some(entry) = system.registry.get(&new_id).await {
                entry.metrics.restart_count.store(restart_count, std::sync::atomic::Ordering::Relaxed);
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
