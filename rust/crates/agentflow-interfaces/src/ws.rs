//! WebSocket routes for the AgentFlow server.
//!
//! Two routes are mounted under the same axum `Router`:
//!
//! - `/ws`   — Python-compatible aggregated-state bridge.
//!             Compatible with `monitor.html` (and any client expecting
//!             `full_snapshot` / `patch` / `delete_agent` JSON messages).
//!
//! - `/mqtt` — Transparent WebSocket proxy to the Mosquitto broker's WS
//!             listener (configurable host/port, default `localhost:9001`).
//!             Compatible with `mqtt.js` / `frontend/dist/index.html`.
//!
//! Together these two routes ensure **any combination** of
//! `python|rust` backend × `monitor.html|frontend/dist/index.html` frontend
//! works without any client-side changes.
//!
//! ## `/ws` message protocol  (mirrors `monitor_server.py`)
//!
//! **Server → browser** on connect:
//! ```json
//! { "type": "full_snapshot", "state": { "agents": [...], "nodes": [...], ... } }
//! ```
//! **Server → browser** on MQTT event:
//! ```json
//! { "type": "patch", "event": { ... }, "state": { ... } }
//! ```
//! **Server → browser** after delete command:
//! ```json
//! { "type": "delete_agent", "agent_id": "...", "state": { ... } }
//! ```
//! **Browser → server** (commands):
//! ```json
//! { "type": "command", "command": "pause|stop|resume|delete", "agent_id": "..." }
//! ```

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::{
    Router,
    extract::{
        State,
        ws::{Message, WebSocket, WebSocketUpgrade},
    },
    http::HeaderMap,
    response::IntoResponse,
    routing::get,
};
use futures_util::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::sync::{Mutex, broadcast};

use agentflow_mqtt::MqttClient;

// ── Internal MQTT envelope (Rust MQTT loop → WS state aggregator) ─────────────

/// Raw MQTT message forwarded from the broker event loop.
/// Consumed by [`WsBridge::spawn_monitor_task`]; not sent to browser clients.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WsEnvelope {
    pub topic:   String,
    pub payload: Value,
}

// ── Monitor state ─────────────────────────────────────────────────────────────

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

/// Mirrors the in-memory state maintained by Python's `monitor_server.py`.
#[derive(Debug, Default)]
pub struct MonitorState {
    agents:        HashMap<String, Value>,
    nodes:         HashMap<String, Value>,
    alerts:        Vec<Value>,
    log_feed:      Vec<Value>,
    system_health: Value,
}

impl MonitorState {
    /// Serialisable snapshot sent to browser clients.
    pub fn snapshot(&self) -> Value {
        let agents: Vec<Value> = self.agents.values().cloned().collect();
        let nodes:  Vec<Value> = self.nodes.values().cloned().collect();
        let total_cost: f64 = self.agents.values()
            .filter_map(|a| a.get("cost_usd").and_then(|v| v.as_f64()))
            .sum();
        let alert_end    = self.alerts.len().min(10);
        let log_end      = self.log_feed.len().min(20);
        json!({
            "agents":          agents,
            "nodes":           nodes,
            "alerts":          &self.alerts[..alert_end],
            "log_feed":        &self.log_feed[..log_end],
            "system_health":   self.system_health,
            "total_cost_usd":  (total_cost * 1_000_000.0).round() / 1_000_000.0,
        })
    }

    fn update_agent(&mut self, agent_id: &str, key: &str, data: Value) {
        let short = &agent_id[..agent_id.len().min(8)];
        let entry = self.agents
            .entry(agent_id.to_string())
            .or_insert_with(|| json!({
                "agent_id":   agent_id,
                "name":       short,
                "first_seen": now_secs(),
            }));
        if let Some(obj) = entry.as_object_mut() {
            obj.insert(key.to_string(), data);
            obj.insert("last_update".to_string(), json!(now_secs()));
        }
    }

    fn add_log(&mut self, entry: Value) {
        self.log_feed.insert(0, entry);
        if self.log_feed.len() > 100 {
            self.log_feed.pop();
        }
    }

    /// Parse one MQTT message and update internal state.
    ///
    /// Returns `Some((event, is_heartbeat))` when something should be
    /// broadcast, or `None` when the topic is not recognised.
    /// `is_heartbeat` suppresses the event from the browser's log feed
    /// (mirrors Python behaviour).
    pub fn parse_topic(&mut self, topic: &str, payload: Value) -> Option<(Value, bool)> {
        let parts: Vec<&str> = topic.split('/').collect();

        // ── system/# ────────────────────────────────────────────────────────
        if parts[0] == "system" && parts.len() >= 2 {
            match parts[1] {
                "health" => { self.system_health = payload.clone(); }
                "alerts" => {
                    self.alerts.insert(0, payload.clone());
                    if self.alerts.len() > 50 { self.alerts.pop(); }
                }
                _ => {}
            }
            return Some((json!({
                "type":    "system",
                "subtype": parts[1],
                "data":    payload,
            }), false));
        }

        // ── agents/{id}/{metric} ─────────────────────────────────────────────
        if parts[0] == "agents" && parts.len() >= 3 {
            let agent_id = parts[1];
            let metric   = parts[2];

            match metric {
                "status" => {
                    self.update_agent(agent_id, "status", payload.clone());
                    if let Some(obj) = payload.as_object() {
                        if let Some(entry) = self.agents.get_mut(agent_id) {
                            if let Some(e) = entry.as_object_mut() {
                                if let Some(n) = obj.get("name")  { e.insert("name".into(),  n.clone()); }
                                if let Some(s) = obj.get("state") { e.insert("state".into(), s.clone()); }
                            }
                        }
                    }
                    self.add_log(json!({
                        "type":      "status",
                        "agent_id":  agent_id,
                        "status":    payload,
                        "timestamp": now_secs(),
                    }));
                }
                "heartbeat" => {
                    self.update_agent(agent_id, "heartbeat", payload.clone());
                    if let Some(obj) = payload.as_object() {
                        let short = &agent_id[..agent_id.len().min(8)];
                        let name = obj.get("name").and_then(|v| v.as_str()).unwrap_or(short);
                        if let Some(entry) = self.agents.get_mut(agent_id) {
                            if let Some(e) = entry.as_object_mut() {
                                e.insert("name".into(), json!(name));
                                for k in &["cpu", "state"] {
                                    if let Some(v) = obj.get(*k) { e.insert(k.to_string(), v.clone()); }
                                }
                                if let Some(v) = obj.get("memory_mb") { e.insert("mem".into(), v.clone()); }
                                if let Some(v) = obj.get("task")      { e.insert("task".into(), v.clone()); }
                            }
                        }
                    }
                    // heartbeat → broadcast state update but suppress from log_feed
                    return Some((json!({
                        "type":     "agent",
                        "agent_id": agent_id,
                        "metric":   metric,
                        "data":     payload,
                    }), true));
                }
                "metrics" => {
                    self.update_agent(agent_id, "metrics", payload.clone());
                    if let Some(obj) = payload.as_object() {
                        if let Some(entry) = self.agents.get_mut(agent_id) {
                            if let Some(e) = entry.as_object_mut() {
                                for k in &["messages_processed", "cost_usd", "input_tokens", "output_tokens"] {
                                    if let Some(v) = obj.get(*k) { e.insert(k.to_string(), v.clone()); }
                                }
                            }
                        }
                    }
                }
                "logs" => {
                    let mut log = json!({
                        "type":      "log",
                        "agent_id":  agent_id,
                        "timestamp": now_secs(),
                    });
                    if let (Some(src), Some(dst)) = (payload.as_object(), log.as_object_mut()) {
                        for (k, v) in src { dst.entry(k.clone()).or_insert(v.clone()); }
                    }
                    self.add_log(log);
                }
                "spawned" => {
                    let mut log = json!({
                        "type":      "spawned",
                        "agent_id":  agent_id,
                        "timestamp": now_secs(),
                    });
                    if let (Some(src), Some(dst)) = (payload.as_object(), log.as_object_mut()) {
                        for (k, v) in src { dst.entry(k.clone()).or_insert(v.clone()); }
                    }
                    self.add_log(log);
                }
                "completed" => {
                    self.update_agent(agent_id, "last_completed", payload.clone());
                    self.add_log(json!({
                        "type":      "completed",
                        "agent_id":  agent_id,
                        "timestamp": now_secs(),
                    }));
                }
                "alert" => {
                    let short = &agent_id[..agent_id.len().min(8)];
                    let known_name = self.agents.get(agent_id)
                        .and_then(|a| a.get("name"))
                        .and_then(|v| v.as_str())
                        .unwrap_or(short)
                        .to_string();
                    let mut enriched = if let Some(obj) = payload.as_object() {
                        let mut e = obj.clone();
                        e.insert("agent_id".into(), json!(agent_id));
                        e.entry("name".to_string()).or_insert_with(|| json!(&known_name));
                        Value::Object(e)
                    } else {
                        json!({ "agent_id": agent_id })
                    };
                    let severity = enriched.get("severity").and_then(|v| v.as_str())
                        .unwrap_or("warning").to_string();
                    let name = enriched.get("name").and_then(|v| v.as_str())
                        .unwrap_or(&known_name).to_string();
                    self.alerts.insert(0, enriched);
                    if self.alerts.len() > 50 { self.alerts.pop(); }
                    self.add_log(json!({
                        "type":      "alert",
                        "agent_id":  agent_id,
                        "name":      name,
                        "message":   format!("{name} unresponsive ({severity})"),
                        "timestamp": now_secs(),
                    }));
                }
                _ => {}
            }
            return Some((json!({
                "type":     "agent",
                "agent_id": agent_id,
                "metric":   metric,
                "data":     payload,
            }), false));
        }

        // ── nodes/{name}/heartbeat ───────────────────────────────────────────
        if parts[0] == "nodes" && parts.len() >= 3 && parts[2] == "heartbeat" {
            let node_name = parts[1];
            if let Some(obj) = payload.as_object() {
                self.nodes.insert(node_name.to_string(), json!({
                    "node":      node_name,
                    "agents":    obj.get("agents").cloned().unwrap_or(json!([])),
                    "last_seen": now_secs(),
                    "online":    true,
                    "node_id":   obj.get("node_id").cloned().unwrap_or(json!("")),
                }));
            }
            return Some((json!({
                "type":      "node",
                "node_name": node_name,
                "data":      payload,
            }), false));
        }

        None
    }
}

// ── Shared bridge state ───────────────────────────────────────────────────────

#[derive(Clone)]
pub struct BridgeState {
    /// MQTT → WS broadcast (raw envelopes, consumed by monitor task).
    pub mqtt_tx:     broadcast::Sender<WsEnvelope>,
    /// Aggregated monitor state shared across all `/ws` connections.
    pub monitor:     Arc<Mutex<MonitorState>>,
    /// Broadcast channel: serialised JSON patches to all `/ws` clients.
    pub monitor_tx:  broadcast::Sender<String>,
    /// MQTT client for publishing commands received from the browser.
    pub mqtt_client: Arc<MqttClient>,
    /// Mosquitto WebSocket host (for `/mqtt` proxy).
    pub mqtt_host:   String,
    /// Mosquitto WebSocket port (for `/mqtt` proxy, default 9001).
    pub mqtt_ws_port: u16,
}

// ── WsBridge ──────────────────────────────────────────────────────────────────

pub struct WsBridge {
    state: BridgeState,
}

impl WsBridge {
    pub fn new(
        mqtt_tx:      broadcast::Sender<WsEnvelope>,
        mqtt_client:  Arc<MqttClient>,
        mqtt_host:    String,
        mqtt_ws_port: u16,
    ) -> Self {
        let (monitor_tx, _) = broadcast::channel::<String>(256);
        Self {
            state: BridgeState {
                mqtt_tx,
                monitor: Arc::new(Mutex::new(MonitorState::default())),
                monitor_tx,
                mqtt_client,
                mqtt_host,
                mqtt_ws_port,
            },
        }
    }

    /// Spawn a background task that:
    ///
    /// 1. Consumes raw MQTT envelopes from the broadcast channel.
    /// 2. Updates [`MonitorState`].
    /// 3. Broadcasts Python-compatible JSON patches to all `/ws` clients.
    pub fn spawn_monitor_task(&self) {
        let mut rx      = self.state.mqtt_tx.subscribe();
        let monitor     = Arc::clone(&self.state.monitor);
        let monitor_tx  = self.state.monitor_tx.clone();

        tokio::spawn(async move {
            while let Ok(envelope) = rx.recv().await {
                let msg = {
                    let mut st = monitor.lock().await;
                    match st.parse_topic(&envelope.topic, envelope.payload) {
                        None => continue,
                        Some((event, is_heartbeat)) => {
                            let snap      = st.snapshot();
                            let log_event = if is_heartbeat { Value::Null } else { event };
                            serde_json::to_string(&json!({
                                "type":  "patch",
                                "event": log_event,
                                "state": snap,
                            })).unwrap_or_default()
                        }
                    }
                };
                if !msg.is_empty() {
                    let _ = monitor_tx.send(msg);
                }
            }
        });
    }

    /// Build the axum `Router` with `/ws` and `/mqtt` routes.
    pub fn router(&self) -> Router {
        Router::new()
            .route("/ws",   get(ws_handler))
            .route("/mqtt", get(mqtt_proxy_handler))
            .with_state(self.state.clone())
    }
}

// ── /ws handler: Python-compatible aggregated state ───────────────────────────

async fn ws_handler(
    ws: WebSocketUpgrade,
    State(state): State<BridgeState>,
) -> impl IntoResponse {
    ws.on_upgrade(move |socket| handle_ws_socket(socket, state))
}

async fn handle_ws_socket(socket: WebSocket, state: BridgeState) {
    let mut monitor_rx = state.monitor_tx.subscribe();
    let (mut ws_send, mut ws_recv) = socket.split();

    // Send a full state snapshot immediately on connect (mirrors Python behaviour)
    let snap_json = {
        let st = state.monitor.lock().await;
        serde_json::to_string(&json!({
            "type":  "full_snapshot",
            "state": st.snapshot(),
        })).unwrap_or_default()
    };
    if ws_send.send(Message::Text(snap_json)).await.is_err() {
        return;
    }

    // Forward broadcast patches to this client
    let send_task = tokio::spawn(async move {
        while let Ok(json) = monitor_rx.recv().await {
            if ws_send.send(Message::Text(json)).await.is_err() {
                break;
            }
        }
    });

    // Handle inbound messages (commands from the browser)
    while let Some(Ok(msg)) = ws_recv.next().await {
        match msg {
            Message::Close(_) => break,
            Message::Text(text) => {
                handle_browser_command(&text, &state).await;
            }
            _ => {}
        }
    }
    send_task.abort();
}

async fn handle_browser_command(text: &str, state: &BridgeState) {
    let Ok(cmd) = serde_json::from_str::<Value>(text) else { return };
    if cmd.get("type").and_then(|v| v.as_str()) != Some("command") { return; }

    let Some(command)  = cmd.get("command").and_then(|v| v.as_str())  else { return };
    let Some(agent_id) = cmd.get("agent_id").and_then(|v| v.as_str()) else { return };

    let valid = ["pause", "stop", "resume", "delete"];
    if !valid.contains(&command) {
        tracing::warn!("[ws] Unknown command: {command}");
        return;
    }

    tracing::info!("[ws] {} -> {}", command.to_uppercase(), &agent_id[..agent_id.len().min(8)]);

    // Publish command to MQTT
    let mqtt_payload = json!({
        "command":   command,
        "sender":    "monitor-dashboard",
        "timestamp": now_secs(),
    });
    let topic = format!("agents/{agent_id}/commands");
    if let Err(e) = state.mqtt_client.publish_json(&topic, &mqtt_payload).await {
        tracing::error!("[ws] MQTT publish failed: {e}");
        return;
    }

    // Optimistic state update + broadcast
    let msg = {
        let mut st = state.monitor.lock().await;
        if command == "delete" {
            st.agents.remove(agent_id);
            let snap = st.snapshot();
            serde_json::to_string(&json!({
                "type":     "delete_agent",
                "agent_id": agent_id,
                "state":    snap,
            })).unwrap_or_default()
        } else {
            if let Some(entry) = st.agents.get_mut(agent_id) {
                if let Some(e) = entry.as_object_mut() {
                    let new_state = match command {
                        "stop"   => "stopped",
                        "pause"  => "paused",
                        "resume" => "running",
                        _        => return,
                    };
                    e.insert("state".into(), json!(new_state));
                }
            }
            let snap = st.snapshot();
            serde_json::to_string(&json!({
                "type":  "patch",
                "state": snap,
            })).unwrap_or_default()
        }
    };

    if !msg.is_empty() {
        let _ = state.monitor_tx.send(msg);
    }
}

// ── /mqtt handler: transparent proxy to Mosquitto WS ─────────────────────────
//
// The browser's mqtt.js speaks the MQTT binary protocol over WebSocket.
// We forward every frame verbatim to/from Mosquitto's WS listener (port 9001
// by default, or whatever --mqtt-ws-port is set to).
//
// Supports the "mqtt" subprotocol header so mqtt.js is satisfied.

async fn mqtt_proxy_handler(
    ws: WebSocketUpgrade,
    headers: HeaderMap,
    State(state): State<BridgeState>,
) -> impl IntoResponse {
    // Echo back whichever MQTT sub-protocol the client announced
    let proto = headers
        .get("sec-websocket-protocol")
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string());

    let ws = ws.protocols(["mqtt", "mqttv3.1"]);
    ws.on_upgrade(move |socket| proxy_to_mosquitto(socket, state, proto))
}

async fn proxy_to_mosquitto(socket: WebSocket, state: BridgeState, proto: Option<String>) {
    use tokio_tungstenite::connect_async;
    use tokio_tungstenite::tungstenite::Message as TMsg;
    use tokio_tungstenite::tungstenite::http::Request;

    let upstream_url = format!("ws://{}:{}/", state.mqtt_host, state.mqtt_ws_port);

    // Build upstream WS request, forwarding the MQTT sub-protocol header
    let request = {
        let mut builder = Request::builder().uri(&upstream_url);
        let p = proto.as_deref().unwrap_or("mqtt");
        builder = builder.header("Sec-WebSocket-Protocol", p);
        match builder.body(()) {
            Ok(r)  => r,
            Err(e) => {
                tracing::warn!("[mqtt-proxy] bad request: {e}");
                return;
            }
        }
    };

    let upstream = match connect_async(request).await {
        Ok((stream, _)) => stream,
        Err(e) => {
            tracing::warn!(
                "[mqtt-proxy] upstream connect failed ({}): {e}",
                upstream_url
            );
            return;
        }
    };

    let (mut up_send, mut up_recv) = upstream.split();
    let (mut cl_send, mut cl_recv) = socket.split();

    // upstream → client
    let up_to_cl = tokio::spawn(async move {
        while let Some(Ok(msg)) = up_recv.next().await {
            let out = match msg {
                TMsg::Binary(b) => Message::Binary(b),
                TMsg::Text(t)   => Message::Text(t),
                TMsg::Close(_)  => break,
                _               => continue,
            };
            if cl_send.send(out).await.is_err() { break; }
        }
    });

    // client → upstream
    while let Some(Ok(msg)) = cl_recv.next().await {
        let fwd = match msg {
            Message::Binary(b) => TMsg::Binary(b),
            Message::Text(t)   => TMsg::Text(t),
            Message::Close(_)  => break,
            _                  => continue,
        };
        if up_send.send(fwd).await.is_err() { break; }
    }

    up_to_cl.abort();
}
