//! # wactorz-interfaces
//!
//! Human and machine interfaces for AgentFlow.
//!
//! - [`cli`] — interactive REPL / command-line interface
//! - [`rest`] — axum HTTP REST API
//! - [`ws`] — WebSocket bridge: MQTT ↔ browser clients (for the Babylon.js dashboard)

pub mod cli;
pub mod rest;
pub mod ws;

pub use rest::RestServer;
pub use ws::WsBridge;
