//! # wactorz-mqtt
//!
//! Async MQTT transport for AgentFlow.
//!
//! Provides:
//! - [`MqttClient`] — thin async wrapper around `rumqttc::AsyncClient`
//! - [`topics`] — well-known topic string constants and builders
//!
//! All AgentFlow MQTT topics follow the pattern:
//! `agents/{agent_id}/{event}` and `system/{event}`

pub mod client;
pub mod topics;

pub use client::{MqttClient, MqttConfig, MqttEvent};
