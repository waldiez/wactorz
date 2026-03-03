//! Actor registry and system orchestrator.
//!
//! [`ActorRegistry`] is a thread-safe map of live actor mailboxes keyed by
//! WID string. [`ActorSystem`] wraps the registry and provides high-level
//! lifecycle operations: spawn, stop, broadcast, and graceful shutdown.

use std::collections::HashMap;
use std::sync::Arc;

use tokio::sync::{mpsc, RwLock};

use crate::actor::ActorState;
use crate::message::{ActorCommand, Message};
use crate::metrics::ActorMetrics;
use crate::publish::EventPublisher;

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
    ///
    /// Returns `Err` if the actor is not found or the mailbox is full.
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

    /// Create an ActorSystem with a specific EventPublisher.
    pub fn with_publisher(publisher: EventPublisher) -> Self {
        Self {
            registry: ActorRegistry::new(),
            publisher,
        }
    }

    /// Return a clone of the event publisher.
    pub fn publisher(&self) -> EventPublisher {
        self.publisher.clone()
    }

    /// Spawn a boxed actor, register it, and drive it on a Tokio task.
    ///
    /// The actor's `run()` method is called inside a `tokio::spawn` wrapper
    /// that deregisters it from the registry on completion.
    pub async fn spawn_actor(&self, actor: Box<dyn crate::actor::Actor>) -> anyhow::Result<String> {
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
