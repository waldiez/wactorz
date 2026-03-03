//! WebSocket ↔ MQTT bridge.
//!
//! [`WsBridge`] accepts WebSocket connections from browser clients (the
//! Babylon.js dashboard) and proxies MQTT messages bidirectionally:
//!
//! - MQTT → WS: all `agents/#` and `system/health` messages are forwarded
//!   to every connected browser client as JSON.
//! - WS → MQTT: messages from the browser are published back to MQTT on
//!   the topic specified in the envelope (e.g. `agents/{id}/commands`).
//!
//! The bridge runs as a standalone axum route (`/ws`) alongside the REST API.
//!
//! ## Message envelope (browser ↔ bridge)
//!
//! ```json
//! { "topic": "agents/abc/commands", "payload": { ... } }
//! ```

use axum::{
    extract::{
        ws::{WebSocket, WebSocketUpgrade},
        State,
    },
    response::IntoResponse,
    routing::get,
    Router,
};
use futures_util::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use tokio::sync::broadcast;

/// A message envelope relayed between browser and bridge.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WsEnvelope {
    pub topic: String,
    pub payload: serde_json::Value,
}

/// Shared state for the WS bridge.
#[derive(Clone)]
pub struct BridgeState {
    /// Broadcast channel: MQTT → all connected WS clients.
    pub mqtt_tx: broadcast::Sender<WsEnvelope>,
}

/// The WebSocket / MQTT bridge server.
pub struct WsBridge {
    state: BridgeState,
}

impl WsBridge {
    /// Create a new bridge.
    ///
    /// `mqtt_tx` is the broadcast sender; the MQTT event loop should publish
    /// to it whenever a message arrives on `agents/#` or `system/health`.
    pub fn new(mqtt_tx: broadcast::Sender<WsEnvelope>) -> Self {
        Self {
            state: BridgeState { mqtt_tx },
        }
    }

    /// Return an axum `Router` with the `/ws` upgrade route mounted.
    pub fn router(&self) -> Router {
        Router::new()
            .route("/ws", get(ws_handler))
            .with_state(self.state.clone())
    }
}

/// Axum handler: upgrades the HTTP connection to a WebSocket.
async fn ws_handler(ws: WebSocketUpgrade, State(state): State<BridgeState>) -> impl IntoResponse {
    ws.on_upgrade(move |socket| handle_socket(socket, state))
}

/// Drive a single WebSocket connection.
///
/// Spawns two tasks:
/// 1. MQTT fan-out: receives from `mqtt_rx` and sends to the browser.
/// 2. Browser inbound: reads WS frames and publishes back to MQTT.
async fn handle_socket(socket: WebSocket, state: BridgeState) {
    let mut mqtt_rx = state.mqtt_tx.subscribe();
    let (mut ws_send, mut ws_recv) = socket.split();

    let send_task = tokio::spawn(async move {
        while let Ok(envelope) = mqtt_rx.recv().await {
            if let Ok(json) = serde_json::to_string(&envelope) {
                if ws_send
                    .send(axum::extract::ws::Message::Text(json.into()))
                    .await
                    .is_err()
                {
                    break;
                }
            }
        }
    });

    while let Some(Ok(msg)) = ws_recv.next().await {
        match msg {
            axum::extract::ws::Message::Close(_) => break,
            axum::extract::ws::Message::Text(text) => {
                tracing::debug!("WS inbound: {}", text);
                // TODO: parse WsEnvelope and forward to MQTT
            }
            _ => {}
        }
    }
    send_task.abort();
}
