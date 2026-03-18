//! # wactorz-core
//!
//! Core actor model primitives for AgentFlow.
//!
//! This crate provides the fundamental building blocks:
//! - [`Actor`] — the base trait every agent must implement
//! - [`Message`] / [`MessageType`] — typed inter-actor communication
//! - [`ActorState`] — lifecycle state machine
//! - [`ActorMetrics`] — runtime telemetry
//! - [`ActorRegistry`] / [`ActorSystem`] — actor lookup and orchestration

pub mod actor;
pub mod message;
pub mod metrics;
pub mod publish;
pub mod registry;

pub use actor::{Actor, ActorConfig, ActorState};
pub use message::{Message, MessageType};
pub use metrics::{ActorMetrics, MetricsSnapshot};
pub use publish::EventPublisher;
pub use registry::{
    ActorEntry, ActorFactory, ActorRegistry, ActorSystem, Supervisor, SupervisorStrategy,
};
