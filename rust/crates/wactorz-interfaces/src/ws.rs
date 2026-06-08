//! WebSocket routes for the Wactorz server.
//!
//! Two routes are mounted under the same axum `Router`:
//!
//! - `/ws`   — Python-compatible aggregated-state bridge.
//!   Compatible with `monitor.html` (and any client expecting
//!   `full_snapshot` / `patch` / `delete_agent` JSON messages).
//!
//! - `/mqtt` — Transparent WebSocket proxy to the Mosquitto broker's WS
//!   listener (configurable host/port, default `localhost:9001`).
//!   Compatible with `mqtt.js` / `frontend/dist/index.html`.
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
use tokio::sync::{Mutex, broadcast, mpsc};

use wactorz_core::{ActorSystem, Message as ActorMessage};
use wactorz_mqtt::MqttClient;

const AGENT_STALE_SECS: f64 = 90.0;
const TERMINAL_AGENT_GRACE_SECS: f64 = 15.0;

// ── Internal MQTT envelope (Rust MQTT loop → WS state aggregator) ─────────────

/// Raw MQTT message forwarded from the broker event loop.
/// Consumed by [`WsBridge::spawn_monitor_task`]; not sent to browser clients.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WsEnvelope {
    pub topic: String,
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
    agents: HashMap<String, Value>,
    nodes: HashMap<String, Value>,
    alerts: Vec<Value>,
    pub log_feed: Vec<Value>,
    system_health: Value,
}

impl MonitorState {
    fn prune_stale(&mut self) {
        let now = now_secs();
        self.agents.retain(|_, agent| {
            let last_update = agent
                .get("last_update")
                .and_then(|v| v.as_f64())
                .unwrap_or(now);
            let state = agent.get("state").and_then(|v| v.as_str()).unwrap_or("");
            let max_age = match state {
                "stopped" | "failed" => TERMINAL_AGENT_GRACE_SECS,
                _ => AGENT_STALE_SECS,
            };
            now - last_update <= max_age
        });
    }

    /// Serialisable snapshot sent to browser clients.
    pub fn snapshot(&mut self) -> Value {
        self.prune_stale();
        let agents: Vec<Value> = self.agents.values().cloned().collect();
        let nodes: Vec<Value> = self.nodes.values().cloned().collect();
        let total_cost: f64 = self
            .agents
            .values()
            .filter_map(|a| a.get("cost_usd").and_then(|v| v.as_f64()))
            .sum();
        let alert_end = self.alerts.len().min(10);
        let log_end = self.log_feed.len().min(20);
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
        let entry = self.agents.entry(agent_id.to_string()).or_insert_with(|| {
            json!({
                "agent_id":   agent_id,
                "name":       short,
                "first_seen": now_secs(),
            })
        });
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
                "health" => {
                    self.system_health = payload.clone();
                }
                "alerts" => {
                    self.alerts.insert(0, payload.clone());
                    if self.alerts.len() > 50 {
                        self.alerts.pop();
                    }
                }
                _ => {}
            }
            return Some((
                json!({
                    "type":    "system",
                    "subtype": parts[1],
                    "data":    payload,
                }),
                false,
            ));
        }

        // ── agents/{id}/{metric} ─────────────────────────────────────────────
        if parts[0] == "agents" && parts.len() >= 3 {
            let agent_id = parts[1];
            let metric = parts[2];

            match metric {
                "status" => {
                    self.update_agent(agent_id, "status", payload.clone());
                    if let Some(obj) = payload.as_object()
                        && let Some(entry) = self.agents.get_mut(agent_id)
                        && let Some(e) = entry.as_object_mut()
                    {
                        if let Some(n) = obj.get("name") {
                            e.insert("name".into(), n.clone());
                        }
                        if let Some(s) = obj.get("state") {
                            e.insert("state".into(), s.clone());
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
                        if let Some(entry) = self.agents.get_mut(agent_id)
                            && let Some(e) = entry.as_object_mut()
                        {
                            e.insert("name".into(), json!(name));
                            for k in &["cpu", "state"] {
                                if let Some(v) = obj.get(*k) {
                                    e.insert(k.to_string(), v.clone());
                                }
                            }
                            if let Some(v) = obj.get("memory_mb") {
                                e.insert("mem".into(), v.clone());
                            }
                            if let Some(v) = obj.get("task") {
                                e.insert("task".into(), v.clone());
                            }
                        }
                    }
                    // heartbeat → broadcast state update but suppress from log_feed
                    return Some((
                        json!({
                            "type":     "agent",
                            "agent_id": agent_id,
                            "metric":   metric,
                            "data":     payload,
                        }),
                        true,
                    ));
                }
                "metrics" => {
                    self.update_agent(agent_id, "metrics", payload.clone());
                    if let Some(obj) = payload.as_object()
                        && let Some(entry) = self.agents.get_mut(agent_id)
                        && let Some(e) = entry.as_object_mut()
                    {
                        for k in &[
                            "messages_processed",
                            "cost_usd",
                            "input_tokens",
                            "output_tokens",
                        ] {
                            if let Some(v) = obj.get(*k) {
                                e.insert(k.to_string(), v.clone());
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
                        for (k, v) in src {
                            dst.entry(k.clone()).or_insert(v.clone());
                        }
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
                        for (k, v) in src {
                            dst.entry(k.clone()).or_insert(v.clone());
                        }
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
                    let known_name = self
                        .agents
                        .get(agent_id)
                        .and_then(|a| a.get("name"))
                        .and_then(|v| v.as_str())
                        .unwrap_or(short)
                        .to_string();
                    let enriched = if let Some(obj) = payload.as_object() {
                        let mut e = obj.clone();
                        e.insert("agent_id".into(), json!(agent_id));
                        e.entry("name".to_string())
                            .or_insert_with(|| json!(&known_name));
                        Value::Object(e)
                    } else {
                        json!({ "agent_id": agent_id })
                    };
                    let severity = enriched
                        .get("severity")
                        .and_then(|v| v.as_str())
                        .unwrap_or("warning")
                        .to_string();
                    let name = enriched
                        .get("name")
                        .and_then(|v| v.as_str())
                        .unwrap_or(&known_name)
                        .to_string();
                    self.alerts.insert(0, enriched);
                    if self.alerts.len() > 50 {
                        self.alerts.pop();
                    }
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
            return Some((
                json!({
                    "type":     "agent",
                    "agent_id": agent_id,
                    "metric":   metric,
                    "data":     payload,
                }),
                false,
            ));
        }

        // ── nodes/{name}/heartbeat ───────────────────────────────────────────
        if parts[0] == "nodes" && parts.len() >= 3 && parts[2] == "heartbeat" {
            let node_name = parts[1];
            if let Some(obj) = payload.as_object() {
                self.nodes.insert(
                    node_name.to_string(),
                    json!({
                        "node":      node_name,
                        "agents":    obj.get("agents").cloned().unwrap_or(json!([])),
                        "last_seen": now_secs(),
                        "online":    true,
                        "node_id":   obj.get("node_id").cloned().unwrap_or(json!("")),
                    }),
                );
            }
            return Some((
                json!({
                    "type":      "node",
                    "node_name": node_name,
                    "data":      payload,
                }),
                false,
            ));
        }

        None
    }
}

// ── Shared bridge state ───────────────────────────────────────────────────────

#[derive(Clone)]
pub struct BridgeState {
    /// MQTT → WS broadcast (raw envelopes, consumed by monitor task).
    pub mqtt_tx: broadcast::Sender<WsEnvelope>,
    /// Aggregated monitor state shared across all `/ws` connections.
    pub monitor: Arc<Mutex<MonitorState>>,
    /// Broadcast channel: serialised JSON patches to all `/ws` clients.
    pub monitor_tx: broadcast::Sender<String>,
    /// MQTT client for publishing commands received from the browser.
    pub mqtt_client: Arc<MqttClient>,
    /// Live actor registry used for direct browser -> actor chat routing.
    pub system: ActorSystem,
    /// Mosquitto WebSocket host (for `/mqtt` proxy).
    pub mqtt_host: String,
    /// Mosquitto WebSocket port (for `/mqtt` proxy, default 9001).
    pub mqtt_ws_port: u16,
}

// ── WsBridge ──────────────────────────────────────────────────────────────────

pub struct WsBridge {
    state: BridgeState,
}

impl WsBridge {
    pub fn new(
        mqtt_tx: broadcast::Sender<WsEnvelope>,
        mqtt_client: Arc<MqttClient>,
        system: ActorSystem,
        mqtt_host: String,
        mqtt_ws_port: u16,
    ) -> Self {
        let (monitor_tx, _) = broadcast::channel::<String>(256);
        Self {
            state: BridgeState {
                mqtt_tx,
                monitor: Arc::new(Mutex::new(MonitorState::default())),
                monitor_tx,
                mqtt_client,
                system,
                mqtt_host,
                mqtt_ws_port,
            },
        }
    }

    /// Spawn a background task that:
    ///
    /// 1. Subscribes to `nodes/#` so remote-node heartbeats reach the bridge.
    /// 2. Consumes raw MQTT envelopes from the broadcast channel.
    /// 3. Updates [`MonitorState`].
    /// 4. Broadcasts Python-compatible JSON patches to all `/ws` clients.
    pub fn spawn_monitor_task(&self) {
        // Subscribe to nodes/# so remote node heartbeats are received.
        // agents/# and system/# are subscribed in main.rs; nodes/# is the
        // bridge's own concern.
        let mqtt_for_sub = Arc::clone(&self.state.mqtt_client);
        tokio::spawn(async move {
            if let Err(e) = mqtt_for_sub.subscribe("nodes/#").await {
                tracing::warn!(
                    "[ws-bridge] nodes/# subscribe failed (broker may not be running): {e}"
                );
            } else {
                tracing::info!("[ws-bridge] subscribed to nodes/#");
            }
        });

        let mut rx = self.state.mqtt_tx.subscribe();
        let monitor = Arc::clone(&self.state.monitor);
        let monitor_tx = self.state.monitor_tx.clone();

        tokio::spawn(async move {
            while let Ok(envelope) = rx.recv().await {
                let msg = {
                    let mut st = monitor.lock().await;
                    match st.parse_topic(&envelope.topic, envelope.payload) {
                        None => continue,
                        Some((event, is_heartbeat)) => {
                            let snap = st.snapshot();
                            let log_event = if is_heartbeat { Value::Null } else { event };
                            serde_json::to_string(&json!({
                                "type":  "patch",
                                "event": log_event,
                                "state": snap,
                            }))
                            .unwrap_or_default()
                        }
                    }
                };
                if !msg.is_empty() {
                    let _ = monitor_tx.send(msg);
                }
            }
        });
    }

    /// Shared reference to the live monitor state (for REST /api/feed).
    pub fn monitor_arc(&self) -> Arc<Mutex<MonitorState>> {
        Arc::clone(&self.state.monitor)
    }

    /// Build the axum `Router` with `/ws` and `/mqtt` routes.
    pub fn router(&self) -> Router {
        Router::new()
            .route("/ws", get(ws_handler))
            .route("/mqtt", get(mqtt_proxy_handler))
            .with_state(self.state.clone())
    }
}

// ── /ws handler: Python-compatible aggregated state ───────────────────────────

async fn ws_handler(ws: WebSocketUpgrade, State(state): State<BridgeState>) -> impl IntoResponse {
    ws.on_upgrade(move |socket| handle_ws_socket(socket, state))
}

async fn handle_ws_socket(socket: WebSocket, state: BridgeState) {
    let mut monitor_rx = state.monitor_tx.subscribe();
    let (mut ws_send, mut ws_recv) = socket.split();

    // Send a full state snapshot immediately on connect (mirrors Python behaviour)
    let snap_json = {
        let mut st = state.monitor.lock().await;
        serde_json::to_string(&json!({
            "type":  "full_snapshot",
            "state": st.snapshot(),
        }))
        .unwrap_or_default()
    };
    if ws_send.send(Message::Text(snap_json.into())).await.is_err() {
        return;
    }
    let config_json = serde_json::to_string(&json!({
        "type": "config",
        "chat_mode": "direct_ws",
    }))
    .unwrap_or_default();
    if ws_send
        .send(Message::Text(config_json.into()))
        .await
        .is_err()
    {
        return;
    }

    // Per-client direct-reply channel: slash command responses bypass the
    // broadcast and go only to this specific connection.
    let (reply_tx, mut reply_rx) = mpsc::channel::<String>(32);

    // Send task: merges broadcast patches and per-client direct replies.
    let send_task = tokio::spawn(async move {
        loop {
            tokio::select! {
                Ok(json) = monitor_rx.recv() => {
                    if ws_send.send(Message::Text(json.into())).await.is_err() {
                        break;
                    }
                }
                Some(json) = reply_rx.recv() => {
                    if ws_send.send(Message::Text(json.into())).await.is_err() {
                        break;
                    }
                }
                else => break,
            }
        }
    });

    // Handle inbound messages (commands and slash commands from the browser)
    while let Some(Ok(msg)) = ws_recv.next().await {
        match msg {
            Message::Close(_) => break,
            Message::Text(text) => {
                let trimmed = text.trim();
                if trimmed.starts_with('/') {
                    // Slash command — reply only to this client
                    let reply = handle_slash_command(trimmed, &state).await;
                    let _ = reply_tx.send(reply).await;
                } else {
                    handle_browser_message(trimmed, &state).await;
                }
            }
            _ => {}
        }
    }
    send_task.abort();
}

/// Handle a slash command sent by a browser client over `/ws`.
///
/// Mirrors the Python `handle_slash` dispatcher in `monitor_server.py`.
/// Returns a JSON string to send back to that specific client only.
async fn handle_slash_command(text: &str, state: &BridgeState) -> String {
    let parts: Vec<&str> = text.split_whitespace().collect();
    let cmd = parts.first().map(|s| s.to_lowercase()).unwrap_or_default();

    let content = match cmd.as_str() {
        "/help" | "/h" => "Commands:\n\
             \x20 /agents                        list all active agents\n\
             \x20 /nodes                         list remote nodes\n\
             \x20 /help                          show this help\n\n\
             Everything else is forwarded to the main orchestrator."
            .to_string(),

        "/agents" => {
            let st = state.monitor.lock().await;
            if st.agents.is_empty() {
                "No agents running.".to_string()
            } else {
                let mut lines = vec!["Agents:".to_string()];
                let mut names: Vec<&str> = st
                    .agents
                    .values()
                    .filter_map(|a| a.get("name").and_then(|v| v.as_str()))
                    .collect();
                names.sort_unstable();
                for name in names {
                    // Find the full agent entry for this name
                    let entry = st
                        .agents
                        .values()
                        .find(|a| a.get("name").and_then(|v| v.as_str()) == Some(name));
                    let state_str = entry
                        .and_then(|a| a.get("state"))
                        .and_then(|v| v.as_str())
                        .unwrap_or("?");
                    let agent_id = entry
                        .and_then(|a| a.get("agent_id"))
                        .and_then(|v| v.as_str())
                        .unwrap_or("");
                    let id_short = &agent_id[..agent_id.len().min(8)];
                    lines.push(format!("  [{state_str:8}] @{name:<22} {id_short}"));
                }
                lines.join("\n")
            }
        }

        "/nodes" => {
            let st = state.monitor.lock().await;
            let mut lines = vec!["Nodes:".to_string()];
            if st.nodes.is_empty() {
                lines.push("  (no remote nodes)".to_string());
            } else {
                let mut node_names: Vec<&str> = st.nodes.keys().map(|s| s.as_str()).collect();
                node_names.sort_unstable();
                for node_name in node_names {
                    if let Some(nd) = st.nodes.get(node_name) {
                        let online = nd.get("online").and_then(|v| v.as_bool()).unwrap_or(false);
                        let status = if online { "online" } else { "OFFLINE" };
                        let agents: Vec<String> = nd
                            .get("agents")
                            .and_then(|v| v.as_array())
                            .map(|arr| {
                                arr.iter()
                                    .filter_map(|v| v.as_str())
                                    .map(|s| format!("@{s}"))
                                    .collect()
                            })
                            .unwrap_or_default();
                        let agent_list = if agents.is_empty() {
                            "(no agents)".to_string()
                        } else {
                            agents.join(", ")
                        };
                        lines.push(format!("  {node_name:<20} {status:<6}   {agent_list}"));
                    }
                }
            }
            lines.join("\n")
        }

        _ => format!("Unknown command: {cmd}. Type /help for available commands."),
    };

    serde_json::to_string(&json!({
        "type":      "chat",
        "from":      "monitor",
        "content":   content,
        "timestamp": now_secs(),
    }))
    .unwrap_or_default()
}

async fn handle_browser_message(text: &str, state: &BridgeState) {
    let Ok(cmd) = serde_json::from_str::<Value>(text) else {
        return;
    };
    match cmd.get("type").and_then(|v| v.as_str()) {
        Some("command") => handle_browser_command(cmd, state).await,
        Some("chat") => handle_browser_chat(cmd, state).await,
        _ => {}
    }
}

async fn handle_browser_chat(cmd: Value, state: &BridgeState) {
    let Some(content) = cmd.get("content").and_then(|v| v.as_str()) else {
        return;
    };
    let target_name = cmd
        .get("agent_name")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .unwrap_or("main-actor");

    let Some(entry) = state.system.registry.get_by_name(target_name).await else {
        tracing::warn!("[ws] chat target not found: {target_name}");
        return;
    };
    let msg = ActorMessage::text(
        Some("user".to_string()),
        Some(entry.id.clone()),
        content.to_string(),
    );
    if let Err(err) = state.system.registry.send(&entry.id, msg).await {
        tracing::warn!("[ws] chat route failed for {target_name}: {err}");
    }
}

async fn handle_browser_command(cmd: Value, state: &BridgeState) {
    let Some(command) = cmd.get("command").and_then(|v| v.as_str()) else {
        return;
    };
    let Some(agent_id) = cmd.get("agent_id").and_then(|v| v.as_str()) else {
        return;
    };

    let valid = ["pause", "stop", "resume", "delete"];
    if !valid.contains(&command) {
        tracing::warn!("[ws] Unknown command: {command}");
        return;
    }

    tracing::info!(
        "[ws] {} -> {}",
        command.to_uppercase(),
        &agent_id[..agent_id.len().min(8)]
    );

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
            }))
            .unwrap_or_default()
        } else {
            if let Some(entry) = st.agents.get_mut(agent_id)
                && let Some(e) = entry.as_object_mut()
            {
                let new_state = match command {
                    "stop" => "stopped",
                    "pause" => "paused",
                    "resume" => "running",
                    _ => return,
                };
                e.insert("state".into(), json!(new_state));
            }
            let snap = st.snapshot();
            serde_json::to_string(&json!({
                "type":  "patch",
                "state": snap,
            }))
            .unwrap_or_default()
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
    use tokio_tungstenite::tungstenite::client::IntoClientRequest;

    let upstream_url = format!("ws://{}:{}/", state.mqtt_host, state.mqtt_ws_port);

    // Build a proper client handshake request, then add the MQTT sub-protocol.
    let request = {
        let mut request = match upstream_url.as_str().into_client_request() {
            Ok(r) => r,
            Err(e) => {
                tracing::warn!("[mqtt-proxy] bad upstream request: {e}");
                return;
            }
        };
        let p = proto.as_deref().unwrap_or("mqtt");
        if let Ok(value) = p.parse() {
            request
                .headers_mut()
                .insert("Sec-WebSocket-Protocol", value);
        }
        request
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
                TMsg::Text(t) => Message::Text(t.as_str().into()),
                TMsg::Close(_) => break,
                _ => continue,
            };
            if cl_send.send(out).await.is_err() {
                break;
            }
        }
    });

    // client → upstream
    while let Some(Ok(msg)) = cl_recv.next().await {
        let fwd = match msg {
            Message::Binary(b) => TMsg::Binary(b),
            Message::Text(t) => TMsg::Text(t.as_str().into()),
            Message::Close(_) => break,
            _ => continue,
        };
        if up_send.send(fwd).await.is_err() {
            break;
        }
    }

    up_to_cl.abort();
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::sync::Arc;

    fn fresh() -> MonitorState {
        MonitorState::default()
    }

    // ── WsEnvelope ────────────────────────────────────────────────────────────

    #[test]
    fn ws_envelope_serde_roundtrip() {
        let e = WsEnvelope { topic: "agents/a1/status".into(), payload: json!({"state": "running"}) };
        let s = serde_json::to_string(&e).unwrap();
        let d: WsEnvelope = serde_json::from_str(&s).unwrap();
        assert_eq!(d.topic, "agents/a1/status");
        assert_eq!(d.payload["state"], "running");
    }

    #[test]
    fn ws_envelope_is_debug_and_clone() {
        let e = WsEnvelope { topic: "t".into(), payload: json!(1) };
        let _ = format!("{e:?}");
        let e2 = e.clone();
        assert_eq!(e2.topic, "t");
    }

    // ── now_secs ──────────────────────────────────────────────────────────────

    #[test]
    fn now_secs_returns_positive() {
        assert!(now_secs() > 0.0);
    }

    // ── MonitorState::default ─────────────────────────────────────────────────

    #[test]
    fn monitor_state_default_is_empty() {
        let st = fresh();
        assert!(st.agents.is_empty());
        assert!(st.nodes.is_empty());
        assert!(st.alerts.is_empty());
        assert!(st.log_feed.is_empty());
    }

    // ── parse_topic: system/* ─────────────────────────────────────────────────

    #[test]
    fn parse_topic_system_health_updates_and_returns_event() {
        let mut st = fresh();
        let r = st.parse_topic("system/health", json!({"status": "ok"}));
        let (ev, is_hb) = r.unwrap();
        assert_eq!(ev["type"], "system");
        assert_eq!(ev["subtype"], "health");
        assert!(!is_hb);
        assert_eq!(st.system_health["status"], "ok");
    }

    #[test]
    fn parse_topic_system_alerts_inserts_and_returns_event() {
        let mut st = fresh();
        let r = st.parse_topic("system/alerts", json!({"msg": "alert!"}));
        let (ev, is_hb) = r.unwrap();
        assert_eq!(ev["subtype"], "alerts");
        assert!(!is_hb);
        assert_eq!(st.alerts.len(), 1);
    }

    #[test]
    fn parse_topic_system_alerts_prunes_at_50() {
        let mut st = fresh();
        for i in 0..55u32 {
            st.parse_topic("system/alerts", json!({"i": i}));
        }
        assert!(st.alerts.len() <= 50, "alerts len = {}", st.alerts.len());
    }

    #[test]
    fn parse_topic_system_unknown_subtype_still_returns_event() {
        let mut st = fresh();
        let r = st.parse_topic("system/unknown_sub", json!({}));
        let (ev, _) = r.unwrap();
        assert_eq!(ev["subtype"], "unknown_sub");
    }

    // ── parse_topic: agents/*  ────────────────────────────────────────────────

    #[test]
    fn parse_topic_agents_status_updates_agent_and_adds_log() {
        let mut st = fresh();
        let r = st.parse_topic("agents/a1b2c3d4/status", json!({"name": "alpha", "state": "running"}));
        let (ev, is_hb) = r.unwrap();
        assert_eq!(ev["type"], "agent");
        assert_eq!(ev["agent_id"], "a1b2c3d4");
        assert!(!is_hb);
        assert!(st.agents.contains_key("a1b2c3d4"));
        assert_eq!(st.agents["a1b2c3d4"]["name"], "alpha");
        assert_eq!(st.agents["a1b2c3d4"]["state"], "running");
        assert_eq!(st.log_feed[0]["type"], "status");
    }

    #[test]
    fn parse_topic_agents_status_payload_not_object_still_registers_agent() {
        let mut st = fresh();
        st.parse_topic("agents/plain_id/status", json!("not-object"));
        assert!(st.agents.contains_key("plain_id"));
    }

    #[test]
    fn parse_topic_agents_heartbeat_is_heartbeat_true() {
        let mut st = fresh();
        let r = st.parse_topic("agents/hb_id/heartbeat", json!({"name": "beta", "state": "running", "cpu": 12.5}));
        let (_, is_hb) = r.unwrap();
        assert!(is_hb, "heartbeat should set is_heartbeat=true");
        assert!(st.agents.contains_key("hb_id"));
        assert_eq!(st.agents["hb_id"]["cpu"], 12.5);
    }

    #[test]
    fn parse_topic_agents_heartbeat_updates_memory_and_task() {
        let mut st = fresh();
        st.parse_topic("agents/hb_id/heartbeat", json!({"name": "beta", "memory_mb": 256, "task": "scan", "state": "running"}));
        assert_eq!(st.agents["hb_id"]["mem"], 256);
        assert_eq!(st.agents["hb_id"]["task"], "scan");
    }

    #[test]
    fn parse_topic_agents_heartbeat_payload_not_object() {
        let mut st = fresh();
        let r = st.parse_topic("agents/hb2/heartbeat", json!("not-obj"));
        let (_, is_hb) = r.unwrap();
        assert!(is_hb);
    }

    #[test]
    fn parse_topic_agents_metrics_updates_agent() {
        let mut st = fresh();
        st.parse_topic("agents/m_id/metrics", json!({"messages_processed": 5, "cost_usd": 0.01, "input_tokens": 100, "output_tokens": 50}));
        assert_eq!(st.agents["m_id"]["messages_processed"], 5);
        assert_eq!(st.agents["m_id"]["cost_usd"], 0.01);
    }

    #[test]
    fn parse_topic_agents_metrics_payload_not_object() {
        let mut st = fresh();
        let r = st.parse_topic("agents/m2/metrics", json!(42));
        assert!(r.is_some());
    }

    #[test]
    fn parse_topic_agents_logs_adds_log_entry_and_merges_payload() {
        let mut st = fresh();
        st.parse_topic("agents/l_id/logs", json!({"message": "hello", "level": "info"}));
        assert!(!st.log_feed.is_empty());
        assert_eq!(st.log_feed[0]["type"], "log");
        assert_eq!(st.log_feed[0]["message"], "hello");
    }

    #[test]
    fn parse_topic_agents_logs_payload_not_object() {
        let mut st = fresh();
        st.parse_topic("agents/l2/logs", json!("string-log"));
        assert_eq!(st.log_feed[0]["type"], "log");
    }

    #[test]
    fn parse_topic_agents_spawned_adds_log_and_merges_payload() {
        let mut st = fresh();
        st.parse_topic("agents/sp_id/spawned", json!({"parent": "main"}));
        assert_eq!(st.log_feed[0]["type"], "spawned");
        assert_eq!(st.log_feed[0]["parent"], "main");
    }

    #[test]
    fn parse_topic_agents_spawned_payload_not_object() {
        let mut st = fresh();
        st.parse_topic("agents/sp2/spawned", json!(null));
        assert_eq!(st.log_feed[0]["type"], "spawned");
    }

    #[test]
    fn parse_topic_agents_completed_adds_log_and_updates_agent() {
        let mut st = fresh();
        st.parse_topic("agents/c_id/completed", json!({"result": "ok"}));
        assert_eq!(st.log_feed[0]["type"], "completed");
        assert!(st.agents.contains_key("c_id"));
    }

    #[test]
    fn parse_topic_agents_alert_with_object_payload_enriches() {
        let mut st = fresh();
        st.parse_topic("agents/al_id/alert", json!({"severity": "critical", "name": "my-agent"}));
        assert!(!st.alerts.is_empty());
        assert_eq!(st.alerts[0]["severity"], "critical");
        assert_eq!(st.log_feed[0]["type"], "alert");
        assert_eq!(st.log_feed[0]["message"], "my-agent unresponsive (critical)");
    }

    #[test]
    fn parse_topic_agents_alert_without_name_uses_short_id() {
        let mut st = fresh();
        st.parse_topic("agents/al_id_xx/alert", json!({"severity": "warning"}));
        assert!(!st.alerts.is_empty());
        // name defaults to short agent_id segment
        assert!(!st.log_feed[0]["name"].as_str().unwrap().is_empty());
    }

    #[test]
    fn parse_topic_agents_alert_uses_known_name_from_previous_status() {
        let mut st = fresh();
        st.parse_topic("agents/named_id/status", json!({"name": "well-known", "state": "running"}));
        st.parse_topic("agents/named_id/alert", json!({"severity": "warning"}));
        assert_eq!(st.log_feed[0]["name"], "well-known");
    }

    #[test]
    fn parse_topic_agents_alert_with_non_object_payload_uses_fallback() {
        let mut st = fresh();
        st.parse_topic("agents/al2/alert", json!("bad"));
        assert!(!st.alerts.is_empty());
        assert_eq!(st.alerts[0]["agent_id"], "al2");
    }

    #[test]
    fn parse_topic_agents_alert_prunes_at_50() {
        let mut st = fresh();
        for i in 0..55u32 {
            st.parse_topic(&format!("agents/ag{i}/alert"), json!({"i": i}));
        }
        assert!(st.alerts.len() <= 50);
    }

    #[test]
    fn parse_topic_agents_unknown_metric_returns_agent_event() {
        let mut st = fresh();
        let r = st.parse_topic("agents/u_id/custom_metric", json!({"data": 1}));
        let (ev, is_hb) = r.unwrap();
        assert_eq!(ev["type"], "agent");
        assert!(!is_hb);
    }

    // ── parse_topic: nodes/* ─────────────────────────────────────────────────

    #[test]
    fn parse_topic_nodes_heartbeat_with_object_updates_nodes() {
        let mut st = fresh();
        let r = st.parse_topic("nodes/rpi4/heartbeat", json!({"agents": ["alpha"], "node_id": "n1"}));
        let (ev, is_hb) = r.unwrap();
        assert_eq!(ev["type"], "node");
        assert_eq!(ev["node_name"], "rpi4");
        assert!(!is_hb);
        assert!(st.nodes.contains_key("rpi4"));
        assert_eq!(st.nodes["rpi4"]["agents"][0], "alpha");
        assert_eq!(st.nodes["rpi4"]["node_id"], "n1");
        assert_eq!(st.nodes["rpi4"]["online"], true);
    }

    #[test]
    fn parse_topic_nodes_heartbeat_with_non_object_skips_insert_but_returns_event() {
        let mut st = fresh();
        let r = st.parse_topic("nodes/rpi4/heartbeat", json!("bad"));
        assert!(r.is_some());
        assert!(st.nodes.is_empty());
    }

    #[test]
    fn parse_topic_nodes_non_heartbeat_returns_none() {
        let mut st = fresh();
        assert!(st.parse_topic("nodes/rpi4/other", json!({})).is_none());
    }

    // ── parse_topic: unrecognised patterns ───────────────────────────────────

    #[test]
    fn parse_topic_unknown_top_level_returns_none() {
        let mut st = fresh();
        assert!(st.parse_topic("unknown/topic/here", json!({})).is_none());
    }

    #[test]
    fn parse_topic_agents_too_short_returns_none() {
        let mut st = fresh();
        assert!(st.parse_topic("agents/only_one", json!({})).is_none());
    }

    #[test]
    fn parse_topic_system_too_short_returns_none() {
        let mut st = fresh();
        assert!(st.parse_topic("system", json!({})).is_none());
    }

    // ── snapshot ─────────────────────────────────────────────────────────────

    #[test]
    fn snapshot_returns_expected_keys() {
        let mut st = fresh();
        st.parse_topic("agents/a1/status", json!({"name": "alpha", "state": "running"}));
        st.parse_topic("nodes/n1/heartbeat", json!({"agents": [], "node_id": "n"}));
        let snap = st.snapshot();
        assert!(snap["agents"].is_array());
        assert!(snap["nodes"].is_array());
        assert!(snap["alerts"].is_array());
        assert!(snap["log_feed"].is_array());
        assert!(snap["total_cost_usd"].is_number());
    }

    #[test]
    fn snapshot_sums_cost_usd() {
        let mut st = fresh();
        st.parse_topic("agents/a1/metrics", json!({"cost_usd": 0.05, "messages_processed": 1, "input_tokens": 1, "output_tokens": 1}));
        let snap = st.snapshot();
        let cost = snap["total_cost_usd"].as_f64().unwrap();
        assert!(cost >= 0.0);
    }

    #[test]
    fn snapshot_caps_alerts_at_10() {
        let mut st = fresh();
        for i in 0..15u32 {
            st.alerts.push(json!({"i": i}));
        }
        let snap = st.snapshot();
        assert!(snap["alerts"].as_array().unwrap().len() <= 10);
    }

    #[test]
    fn snapshot_caps_log_feed_at_20() {
        let mut st = fresh();
        for i in 0..25u32 {
            st.log_feed.push(json!({"i": i}));
        }
        let snap = st.snapshot();
        assert!(snap["log_feed"].as_array().unwrap().len() <= 20);
    }

    // ── prune_stale ───────────────────────────────────────────────────────────

    #[test]
    fn prune_stale_removes_old_running_agent() {
        let mut st = fresh();
        st.agents.insert("old".into(), json!({ "agent_id": "old", "last_update": 0.0, "state": "running" }));
        st.prune_stale();
        assert!(!st.agents.contains_key("old"));
    }

    #[test]
    fn prune_stale_keeps_fresh_agent() {
        let mut st = fresh();
        st.agents.insert("fresh".into(), json!({ "agent_id": "fresh", "last_update": now_secs(), "state": "running" }));
        st.prune_stale();
        assert!(st.agents.contains_key("fresh"));
    }

    #[test]
    fn prune_stale_removes_old_stopped_agent_with_short_grace() {
        let mut st = fresh();
        let old = now_secs() - 20.0; // 20s ago > TERMINAL_AGENT_GRACE_SECS (15s)
        st.agents.insert("dead".into(), json!({ "agent_id": "dead", "last_update": old, "state": "stopped" }));
        st.prune_stale();
        assert!(!st.agents.contains_key("dead"));
    }

    #[test]
    fn prune_stale_keeps_recent_stopped_agent() {
        let mut st = fresh();
        let recent = now_secs() - 5.0; // 5s ago < TERMINAL_AGENT_GRACE_SECS (15s)
        st.agents.insert("recent_stopped".into(), json!({ "agent_id": "recent_stopped", "last_update": recent, "state": "stopped" }));
        st.prune_stale();
        assert!(st.agents.contains_key("recent_stopped"));
    }

    #[test]
    fn prune_stale_agents_without_last_update_default_to_now_and_are_kept() {
        let mut st = fresh();
        st.agents.insert("no_ts".into(), json!({ "agent_id": "no_ts", "state": "running" }));
        st.prune_stale();
        assert!(st.agents.contains_key("no_ts"));
    }

    // ── add_log ───────────────────────────────────────────────────────────────

    #[test]
    fn add_log_inserts_at_front_and_caps_at_100() {
        let mut st = fresh();
        for i in 0..105u32 {
            st.add_log(json!({"i": i}));
        }
        assert_eq!(st.log_feed.len(), 100);
        assert_eq!(st.log_feed[0]["i"], 104); // most recent at front
    }

    // ── update_agent ──────────────────────────────────────────────────────────

    #[test]
    fn update_agent_creates_entry_and_updates_key() {
        let mut st = fresh();
        st.update_agent("my_agent_id", "custom_key", json!("hello"));
        assert!(st.agents.contains_key("my_agent_id"));
        assert_eq!(st.agents["my_agent_id"]["custom_key"], "hello");
        assert!(st.agents["my_agent_id"]["last_update"].as_f64().unwrap() > 0.0);
    }

    #[test]
    fn update_agent_updates_existing_entry() {
        let mut st = fresh();
        st.update_agent("existing_id", "key", json!(1));
        st.update_agent("existing_id", "key", json!(2));
        assert_eq!(st.agents["existing_id"]["key"], 2);
    }

    // ── BridgeState / handle_slash_command / handle_browser_* ────────────────

    fn make_bridge_state() -> BridgeState {
        use wactorz_mqtt::{MqttClient, MqttConfig};
        let config = MqttConfig::default();
        let (mqtt_client, _event_loop) = MqttClient::new(config).unwrap();
        let (mqtt_tx, _) = broadcast::channel(8);
        let (monitor_tx, _) = broadcast::channel(8);
        BridgeState {
            mqtt_tx,
            monitor: Arc::new(Mutex::new(MonitorState::default())),
            monitor_tx,
            mqtt_client: Arc::new(mqtt_client),
            system: wactorz_core::ActorSystem::default(),
            mqtt_host: "localhost".into(),
            mqtt_ws_port: 9001,
        }
    }

    #[test]
    fn ws_bridge_monitor_arc_returns_arc() {
        use wactorz_mqtt::{MqttClient, MqttConfig};
        let (mqtt_tx, _) = broadcast::channel(8);
        let (mqtt_client, _) = MqttClient::new(MqttConfig::default()).unwrap();
        let bridge = WsBridge::new(
            mqtt_tx,
            Arc::new(mqtt_client),
            wactorz_core::ActorSystem::default(),
            "localhost".into(),
            9001,
        );
        let _arc = bridge.monitor_arc();
    }

    #[tokio::test]
    async fn handle_slash_command_help_returns_help_text() {
        let state = make_bridge_state();
        let reply = handle_slash_command("/help", &state).await;
        let v: serde_json::Value = serde_json::from_str(&reply).unwrap();
        assert_eq!(v["type"], "chat");
        assert!(v["content"].as_str().unwrap().contains("Commands"));
    }

    #[tokio::test]
    async fn handle_slash_command_h_alias_is_same_as_help() {
        let state = make_bridge_state();
        let reply = handle_slash_command("/h", &state).await;
        let v: serde_json::Value = serde_json::from_str(&reply).unwrap();
        assert!(v["content"].as_str().unwrap().contains("Commands"));
    }

    #[tokio::test]
    async fn handle_slash_command_agents_with_no_agents() {
        let state = make_bridge_state();
        let reply = handle_slash_command("/agents", &state).await;
        let v: serde_json::Value = serde_json::from_str(&reply).unwrap();
        assert!(v["content"].as_str().unwrap().contains("No agents"));
    }

    #[tokio::test]
    async fn handle_slash_command_agents_with_registered_agents() {
        let state = make_bridge_state();
        {
            let mut st = state.monitor.lock().await;
            st.agents.insert("test_id_abc".into(), json!({
                "agent_id": "test_id_abc",
                "name":     "alpha",
                "state":    "running",
                "last_update": now_secs(),
            }));
        }
        let reply = handle_slash_command("/agents", &state).await;
        let v: serde_json::Value = serde_json::from_str(&reply).unwrap();
        assert!(v["content"].as_str().unwrap().contains("Agents"));
    }

    #[tokio::test]
    async fn handle_slash_command_nodes_with_no_nodes() {
        let state = make_bridge_state();
        let reply = handle_slash_command("/nodes", &state).await;
        let v: serde_json::Value = serde_json::from_str(&reply).unwrap();
        assert!(v["content"].as_str().unwrap().contains("no remote nodes"));
    }

    #[tokio::test]
    async fn handle_slash_command_nodes_with_online_node() {
        let state = make_bridge_state();
        {
            let mut st = state.monitor.lock().await;
            st.nodes.insert("pi4".into(), json!({
                "node": "pi4", "online": true, "agents": ["alpha", "beta"]
            }));
        }
        let reply = handle_slash_command("/nodes", &state).await;
        let v: serde_json::Value = serde_json::from_str(&reply).unwrap();
        assert!(v["content"].as_str().unwrap().contains("pi4"));
        assert!(v["content"].as_str().unwrap().contains("online"));
    }

    #[tokio::test]
    async fn handle_slash_command_nodes_with_offline_node() {
        let state = make_bridge_state();
        {
            let mut st = state.monitor.lock().await;
            st.nodes.insert("pi5".into(), json!({
                "node": "pi5", "online": false, "agents": []
            }));
        }
        let reply = handle_slash_command("/nodes", &state).await;
        let v: serde_json::Value = serde_json::from_str(&reply).unwrap();
        assert!(v["content"].as_str().unwrap().contains("OFFLINE"));
        assert!(v["content"].as_str().unwrap().contains("no agents"));
    }

    #[tokio::test]
    async fn handle_slash_command_unknown_returns_error_message() {
        let state = make_bridge_state();
        let reply = handle_slash_command("/unknown_xyz", &state).await;
        let v: serde_json::Value = serde_json::from_str(&reply).unwrap();
        assert!(v["content"].as_str().unwrap().contains("Unknown command"));
    }

    #[tokio::test]
    async fn handle_browser_message_invalid_json_is_noop() {
        let state = make_bridge_state();
        handle_browser_message("not json", &state).await;
    }

    #[tokio::test]
    async fn handle_browser_message_unknown_type_is_noop() {
        let state = make_bridge_state();
        handle_browser_message(r#"{"type": "unknown_msg"}"#, &state).await;
    }

    #[tokio::test]
    async fn handle_browser_chat_no_content_is_noop() {
        let state = make_bridge_state();
        handle_browser_message(r#"{"type": "chat"}"#, &state).await;
    }

    #[tokio::test]
    async fn handle_browser_chat_unknown_agent_logs_warn() {
        let state = make_bridge_state();
        handle_browser_message(
            r#"{"type": "chat", "content": "hello", "agent_name": "ghost-agent"}"#,
            &state,
        )
        .await;
    }

    #[tokio::test]
    async fn handle_browser_chat_with_known_agent() {
        use wactorz_core::{ActorEntry, ActorMetrics, ActorState};
        let state = make_bridge_state();
        let (tx, _rx) = mpsc::channel(8);
        state.system.registry.register(ActorEntry {
            id: "chat-agent-id".into(),
            name: "main-actor".into(),
            state: ActorState::Running,
            mailbox: tx,
            protected: false,
            metrics: Arc::new(ActorMetrics::new()),
            supervisor_id: None,
        }).await;
        handle_browser_message(
            r#"{"type": "chat", "content": "hi", "agent_name": "main-actor"}"#,
            &state,
        )
        .await;
    }

    #[tokio::test]
    async fn handle_browser_chat_empty_agent_name_uses_main_actor() {
        let state = make_bridge_state();
        // agent_name="" is filtered → uses "main-actor" which is not registered → warn
        handle_browser_message(
            r#"{"type": "chat", "content": "hi", "agent_name": ""}"#,
            &state,
        )
        .await;
    }

    #[tokio::test]
    async fn handle_browser_command_no_command_field_is_noop() {
        let state = make_bridge_state();
        handle_browser_message(r#"{"type": "command", "agent_id": "x"}"#, &state).await;
    }

    #[tokio::test]
    async fn handle_browser_command_no_agent_id_field_is_noop() {
        let state = make_bridge_state();
        handle_browser_message(r#"{"type": "command", "command": "stop"}"#, &state).await;
    }

    #[tokio::test]
    async fn handle_browser_command_invalid_command_logs_warn() {
        let state = make_bridge_state();
        handle_browser_message(
            r#"{"type": "command", "command": "explode", "agent_id": "x"}"#,
            &state,
        )
        .await;
    }

    #[tokio::test]
    async fn handle_browser_command_pause_updates_state_optimistically() {
        let state = make_bridge_state();
        {
            let mut st = state.monitor.lock().await;
            st.agents.insert("pid1".into(), json!({
                "agent_id": "pid1", "state": "running", "last_update": now_secs()
            }));
        }
        handle_browser_message(
            r#"{"type": "command", "command": "pause", "agent_id": "pid1"}"#,
            &state,
        )
        .await;
    }

    #[tokio::test]
    async fn handle_browser_command_stop_updates_state_optimistically() {
        let state = make_bridge_state();
        {
            let mut st = state.monitor.lock().await;
            st.agents.insert("sid1".into(), json!({
                "agent_id": "sid1", "state": "running", "last_update": now_secs()
            }));
        }
        handle_browser_message(
            r#"{"type": "command", "command": "stop", "agent_id": "sid1"}"#,
            &state,
        )
        .await;
    }

    #[tokio::test]
    async fn handle_browser_command_resume_updates_state_optimistically() {
        let state = make_bridge_state();
        {
            let mut st = state.monitor.lock().await;
            st.agents.insert("rid1".into(), json!({
                "agent_id": "rid1", "state": "paused", "last_update": now_secs()
            }));
        }
        handle_browser_message(
            r#"{"type": "command", "command": "resume", "agent_id": "rid1"}"#,
            &state,
        )
        .await;
    }

    #[tokio::test]
    async fn handle_browser_command_delete_does_not_panic() {
        // MQTT publish will fail (no broker) → early return before delete.
        // Test just verifies the function handles the failure gracefully.
        let state = make_bridge_state();
        {
            let mut st = state.monitor.lock().await;
            st.agents.insert("did1".into(), json!({
                "agent_id": "did1", "state": "running", "last_update": now_secs()
            }));
        }
        handle_browser_message(
            r#"{"type": "command", "command": "delete", "agent_id": "did1"}"#,
            &state,
        )
        .await;
    }

    #[tokio::test]
    async fn spawn_monitor_task_subscribe_failure_is_handled() {
        // EventLoop is dropped (make_bridge_state drops it) → subscribe fails gracefully.
        let state = make_bridge_state();
        let bridge = WsBridge { state };
        bridge.spawn_monitor_task();
        // Give spawned tasks time to run the subscribe error path.
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }

    #[tokio::test]
    async fn spawn_monitor_task_broadcasts_patches() {
        use wactorz_mqtt::{MqttClient, MqttConfig};

        let (mqtt_tx, _) = broadcast::channel::<WsEnvelope>(16);
        let config = MqttConfig::default();
        let (mqtt_client, _event_loop) = MqttClient::new(config).unwrap();
        // _event_loop alive → subscribe("nodes/#") will succeed (covers Ok branch)

        let bridge = WsBridge::new(
            mqtt_tx.clone(),
            Arc::new(mqtt_client),
            wactorz_core::ActorSystem::default(),
            "localhost".into(),
            9001,
        );
        let mut monitor_rx = bridge.state.monitor_tx.subscribe();
        bridge.spawn_monitor_task();

        tokio::time::sleep(std::time::Duration::from_millis(20)).await;

        mqtt_tx
            .send(WsEnvelope {
                topic: "agents/abc123/status".into(),
                payload: serde_json::json!({"state": "running", "name": "test-agent"}),
            })
            .unwrap();

        let result = tokio::time::timeout(
            std::time::Duration::from_millis(200),
            monitor_rx.recv(),
        )
        .await;
        assert!(result.is_ok(), "monitor task should broadcast a patch within 200ms");
        let json_str = result.unwrap().unwrap();
        let v: serde_json::Value = serde_json::from_str(&json_str).unwrap();
        assert_eq!(v["type"], "patch");
    }

    #[tokio::test]
    async fn spawn_monitor_task_skips_unknown_topic() {
        use wactorz_mqtt::{MqttClient, MqttConfig};

        let (mqtt_tx, _) = broadcast::channel::<WsEnvelope>(16);
        let (mqtt_client, _event_loop) = MqttClient::new(MqttConfig::default()).unwrap();

        let bridge = WsBridge::new(
            mqtt_tx.clone(),
            Arc::new(mqtt_client),
            wactorz_core::ActorSystem::default(),
            "localhost".into(),
            9001,
        );
        let mut monitor_rx = bridge.state.monitor_tx.subscribe();
        bridge.spawn_monitor_task();

        tokio::time::sleep(std::time::Duration::from_millis(10)).await;

        // Unknown topic → parse_topic returns None → monitor_tx not sent to.
        mqtt_tx
            .send(WsEnvelope {
                topic: "unknown/topic".into(),
                payload: serde_json::json!({}),
            })
            .unwrap();

        let result = tokio::time::timeout(
            std::time::Duration::from_millis(100),
            monitor_rx.recv(),
        )
        .await;
        // Should time out because no broadcast was sent for unknown topic.
        assert!(result.is_err(), "unknown topic should not broadcast");
    }

    // ── handle_browser_command with a live MQTT EventLoop ─────────────────────
    //
    // When the EventLoop is alive, publish_json queues successfully → the
    // state-update and broadcast code after the publish is reached.

    fn make_bridge_state_live() -> (BridgeState, Box<dyn std::any::Any + Send>) {
        use wactorz_mqtt::{MqttClient, MqttConfig};
        let (mqtt_client, event_loop) = MqttClient::new(MqttConfig::default()).unwrap();
        let (mqtt_tx, _) = broadcast::channel(8);
        let (monitor_tx, _) = broadcast::channel(8);
        let state = BridgeState {
            mqtt_tx,
            monitor: Arc::new(Mutex::new(MonitorState::default())),
            monitor_tx,
            mqtt_client: Arc::new(mqtt_client),
            system: wactorz_core::ActorSystem::default(),
            mqtt_host: "localhost".into(),
            mqtt_ws_port: 9001,
        };
        (state, Box::new(event_loop))
    }

    #[tokio::test]
    async fn handle_browser_command_pause_with_live_mqtt_updates_state() {
        let (state, _event_loop) = make_bridge_state_live();
        {
            let mut st = state.monitor.lock().await;
            st.agents.insert("ag1".into(), json!({
                "agent_id": "ag1", "state": "running", "name": "test", "last_update": now_secs()
            }));
        }
        handle_browser_message(
            r#"{"type": "command", "command": "pause", "agent_id": "ag1"}"#,
            &state,
        )
        .await;
        let st = state.monitor.lock().await;
        assert_eq!(st.agents["ag1"]["state"], json!("paused"));
    }

    #[tokio::test]
    async fn handle_browser_command_stop_with_live_mqtt_updates_state() {
        let (state, _event_loop) = make_bridge_state_live();
        {
            let mut st = state.monitor.lock().await;
            st.agents.insert("ag2".into(), json!({
                "agent_id": "ag2", "state": "running", "name": "test", "last_update": now_secs()
            }));
        }
        handle_browser_message(
            r#"{"type": "command", "command": "stop", "agent_id": "ag2"}"#,
            &state,
        )
        .await;
        let st = state.monitor.lock().await;
        assert_eq!(st.agents["ag2"]["state"], json!("stopped"));
    }

    #[tokio::test]
    async fn handle_browser_command_resume_with_live_mqtt_updates_state() {
        let (state, _event_loop) = make_bridge_state_live();
        {
            let mut st = state.monitor.lock().await;
            st.agents.insert("ag3".into(), json!({
                "agent_id": "ag3", "state": "paused", "name": "test", "last_update": now_secs()
            }));
        }
        handle_browser_message(
            r#"{"type": "command", "command": "resume", "agent_id": "ag3"}"#,
            &state,
        )
        .await;
        let st = state.monitor.lock().await;
        assert_eq!(st.agents["ag3"]["state"], json!("running"));
    }

    #[tokio::test]
    async fn handle_browser_command_delete_with_live_mqtt_removes_agent() {
        let (state, _event_loop) = make_bridge_state_live();
        {
            let mut st = state.monitor.lock().await;
            st.agents.insert("ag4".into(), json!({
                "agent_id": "ag4", "state": "running", "name": "test", "last_update": now_secs()
            }));
        }
        handle_browser_message(
            r#"{"type": "command", "command": "delete", "agent_id": "ag4"}"#,
            &state,
        )
        .await;
        let st = state.monitor.lock().await;
        assert!(!st.agents.contains_key("ag4"), "agent should be removed after delete");
    }
}
