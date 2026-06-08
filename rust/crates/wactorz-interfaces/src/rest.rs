//! axum HTTP REST API.
//!
//! Exposes a thin REST layer over the actor system.
//!
//! ## Endpoints
//!
//! | Method | Path | Description |
//! |--------|------|-------------|
//! | GET | `/health` | Server liveness check |
//! | GET | `/actors` | List all actors + states |
//! | GET | `/actors/{id}` | Single actor info |
//! | POST | `/actors/{id}/message` | Send a message to an actor |
//! | DELETE | `/actors/{id}` | Stop an actor (if not protected) |
//! | GET | `/actors/{id}/metrics` | Actor runtime metrics |
//! | POST | `/chat` | Send a message to MainActor and stream response |

use anyhow::Result;
use axum::{
    Json, Router,
    body::Bytes,
    extract::{Path, Query, State},
    http::{HeaderMap, StatusCode, header},
    response::{IntoResponse, Response},
    routing::{delete, get, post},
};
use serde::Deserialize;
use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::Arc;
use tokio::sync::Mutex;
use tower_http::cors::CorsLayer;
use tower_http::services::{ServeDir, ServeFile};
use tower_http::trace::TraceLayer;

use crate::ws::MonitorState;
use wactorz_core::ActorSystem;
use wactorz_core::message::{ActorCommand, Message};

/// JSON body for POST /api/reset
#[derive(Debug, Deserialize)]
struct ResetRequest {
    scope: String,
    agent: Option<String>,
}

/// JSON body for POST /api/cost/limit
#[derive(Debug, Deserialize)]
struct CostLimitRequest {
    limit_usd: f64,
    #[serde(default = "default_period")]
    period: String,
}

fn default_period() -> String {
    "monthly".to_string()
}

/// Runtime config exposed via /api/config (mirrors Python's config_handler).
#[derive(Clone, Debug, Default)]
pub struct RuntimeConfig {
    pub ha_url: String,
    pub ha_token: String,
    pub fuseki_url: String,
    pub fuseki_dataset: String,
    pub fuseki_user: String,
    pub fuseki_password: String,
    pub weather_default_location: String,
    pub mqtt_host: String,
    pub mqtt_port: u16,
    pub mqtt_ws_port: u16,
    pub llm_provider: String,
    pub llm_model: String,
    /// Root data directory — actor DBs live at <data_dir>/actors/<name>.db
    /// and the global chat log at <data_dir>/wactorz.db.
    pub data_dir: String,
}

/// Shared application state injected into axum handlers.
#[derive(Clone)]
pub struct AppState {
    pub system: ActorSystem,
    pub config: RuntimeConfig,
    pub http: reqwest::Client,
    /// Live monitor state shared with WsBridge — used for GET /api/feed.
    pub monitor: Option<Arc<Mutex<MonitorState>>>,
}

/// JSON body for POST /actors/{id}/message
#[derive(Debug, Deserialize)]
pub struct SendMessageRequest {
    pub content: String,
    #[serde(rename = "type", default)]
    pub message_type: String,
}

/// JSON body for POST /chat
#[derive(Debug, Deserialize)]
pub struct ChatRequest {
    pub message: String,
    pub agent_name: Option<String>,
}

/// The axum HTTP server.
pub struct RestServer {
    state: AppState,
    addr: SocketAddr,
    /// Path to the built frontend assets directory (e.g. "static/app").
    static_dir: String,
    /// Optional WsBridge router merged onto the same port (Python-compatible single-port setup).
    ws_router: Option<axum::Router>,
}

impl RestServer {
    pub fn new(
        system: ActorSystem,
        addr: SocketAddr,
        config: RuntimeConfig,
        static_dir: String,
    ) -> Self {
        Self {
            state: AppState {
                system,
                config,
                http: reqwest::Client::new(),
                monitor: None,
            },
            addr,
            static_dir,
            ws_router: None,
        }
    }

    /// Merge a WsBridge router so /ws and /mqtt are served on the same port.
    pub fn with_ws(mut self, ws_router: axum::Router) -> Self {
        self.ws_router = Some(ws_router);
        self
    }

    /// Share the WsBridge's live MonitorState so /api/feed can read it.
    pub fn with_monitor(mut self, monitor: Arc<Mutex<MonitorState>>) -> Self {
        self.state.monitor = Some(monitor);
        self
    }

    /// Build the axum `Router`.
    pub fn router(&self) -> Router {
        let index_html = format!("{}/index.html", self.static_dir);
        let serve_dir = ServeDir::new(&self.static_dir).fallback(ServeFile::new(&index_html));

        let mut r = Router::new()
            .route("/health", get(health_handler))
            // Native paths
            .route("/actors", get(list_actors_handler))
            .route("/actors/{id}", get(get_actor_handler))
            .route("/actors/{id}/message", post(send_message_handler))
            .route("/actors/{id}", delete(stop_actor_handler))
            .route("/actors/{id}/pause", post(pause_actor_handler))
            .route("/actors/{id}/resume", post(resume_actor_handler))
            .route("/actors/{id}/metrics", get(get_metrics_handler))
            .route("/chat", post(chat_handler))
            // /api/* aliases — match paths the Python backend and frontend expect
            .route("/api/config", get(config_handler))
            .route("/api/actors", get(list_actors_handler))
            .route("/api/actors/{id}", get(get_actor_handler))
            .route("/api/actors/{id}/message", post(send_message_handler))
            .route("/api/actors/{id}", delete(stop_actor_handler))
            .route("/api/actors/{id}/pause", post(pause_actor_handler))
            .route("/api/actors/{id}/resume", post(resume_actor_handler))
            .route("/api/actors/{id}/metrics", get(get_metrics_handler))
            .route("/api/fuseki/{dataset}/sparql", post(fuseki_sparql_handler))
            .route("/api/fuseki/{dataset}/update", post(fuseki_update_handler))
            // Python-compatible aliases
            .route("/api/feed", get(feed_handler))
            .route("/feed", get(feed_handler))
            .route("/config", get(config_handler))
            // Actor conversation history (kv_store → conversation_history)
            .route("/api/actors/{id}/history", get(actor_history_handler))
            .route("/actors/{id}/history", get(actor_history_handler))
            // Global chat log
            .route("/api/chats", get(chat_log_handler))
            .route("/chats", get(chat_log_handler))
            // Reset
            .route("/api/reset", post(reset_handler))
            // Cost tracking
            .route("/api/cost", get(cost_handler))
            .route("/cost", get(cost_handler))
            .route("/api/cost/limit", post(cost_limit_handler))
            .route("/cost/limit", post(cost_limit_handler))
            .route("/api/cost/reset", post(cost_reset_handler))
            .route("/cost/reset", post(cost_reset_handler))
            // TTS
            .route("/api/tts/voices", get(tts_voices_handler))
            .route("/api/tts", get(tts_handler))
            // HA bridge sync
            .route("/api/ha/sync", post(ha_sync_handler))
            .with_state(self.state.clone());

        // Merge /ws and /mqtt onto the same port so the frontend can reach
        // them via window.location.host (Python-compatible single-port layout).
        if let Some(ws) = &self.ws_router {
            r = r.merge(ws.clone());
        }

        r.fallback_service(serve_dir)
            .layer(CorsLayer::permissive())
            .layer(TraceLayer::new_for_http())
    }

    /// Start listening and serving.
    pub async fn serve(self) -> Result<()> {
        let router = self.router();
        let listener = tokio::net::TcpListener::bind(self.addr).await?;
        tracing::info!("REST API listening on {}", self.addr);
        axum::serve(listener, router).await?;
        Ok(())
    }
}

// ── Handlers ─────────────────────────────────────────────────────────────────

async fn health_handler() -> impl IntoResponse {
    Json(serde_json::json!({ "status": "ok" }))
}

async fn list_actors_handler(State(state): State<AppState>) -> impl IntoResponse {
    let actors = state.system.registry.list().await;
    let body: Vec<_> = actors
        .iter()
        .map(|e| {
            serde_json::json!({
                "id": e.id,
                "name": e.name,
                "state": format!("{}", e.state),
                "protected": e.protected,
            })
        })
        .collect();
    Json(body)
}

async fn get_actor_handler(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> impl IntoResponse {
    match state.system.registry.get(&id).await {
        Some(entry) => Json(serde_json::json!({
            "id": entry.id,
            "name": entry.name,
            "state": format!("{}", entry.state),
            "protected": entry.protected,
        }))
        .into_response(),
        None => (StatusCode::NOT_FOUND, "actor not found").into_response(),
    }
}

async fn send_message_handler(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Json(body): Json<SendMessageRequest>,
) -> axum::response::Response {
    let msg = Message::text(None, Some(id.clone()), body.content);
    match state.system.registry.send(&id, msg).await {
        Ok(_) => (StatusCode::OK, Json(serde_json::json!({"status": "sent"}))).into_response(),
        Err(e) => (StatusCode::NOT_FOUND, e.to_string()).into_response(),
    }
}

async fn stop_actor_handler(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> axum::response::Response {
    let entry = match state.system.registry.get(&id).await {
        Some(e) => e,
        None => return (StatusCode::NOT_FOUND, "actor not found").into_response(),
    };
    if entry.protected {
        return (StatusCode::FORBIDDEN, "actor is protected").into_response();
    }
    let msg = Message::command(id.clone(), ActorCommand::Stop);
    match state.system.registry.send(&id, msg).await {
        Ok(_) => (StatusCode::OK, "stopping").into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

async fn get_metrics_handler(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> axum::response::Response {
    match state.system.registry.get(&id).await {
        Some(e) => Json(e.metrics.snapshot()).into_response(),
        None => (StatusCode::NOT_FOUND, "actor not found").into_response(),
    }
}

async fn pause_actor_handler(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> axum::response::Response {
    let entry = match state.system.registry.get(&id).await {
        Some(e) => e,
        None => return (StatusCode::NOT_FOUND, "actor not found").into_response(),
    };
    if entry.protected {
        return (StatusCode::FORBIDDEN, "actor is protected").into_response();
    }
    let msg = Message::command(id.clone(), ActorCommand::Pause);
    match state.system.registry.send(&id, msg).await {
        Ok(_) => (
            StatusCode::OK,
            Json(serde_json::json!({"status": "pausing"})),
        )
            .into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

async fn resume_actor_handler(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> axum::response::Response {
    let entry = match state.system.registry.get(&id).await {
        Some(e) => e,
        None => return (StatusCode::NOT_FOUND, "actor not found").into_response(),
    };
    if entry.protected {
        return (StatusCode::FORBIDDEN, "actor is protected").into_response();
    }
    let msg = Message::command(id.clone(), ActorCommand::Resume);
    match state.system.registry.send(&id, msg).await {
        Ok(_) => (
            StatusCode::OK,
            Json(serde_json::json!({"status": "resuming"})),
        )
            .into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

async fn config_handler(State(state): State<AppState>) -> impl IntoResponse {
    let c = &state.config;
    Json(serde_json::json!({
        "ha": { "url": c.ha_url, "token": c.ha_token },
        "fuseki": { "url": c.fuseki_url, "dataset": c.fuseki_dataset },
        "mqtt": {
            "host": c.mqtt_host,
            "port": c.mqtt_port,
            "url": format!("ws://{}:{}", c.mqtt_host, c.mqtt_ws_port),
        },
        "llm": { "provider": c.llm_provider, "model": c.llm_model },
        "weather": { "defaultLocation": c.weather_default_location },
    }))
}

async fn fuseki_sparql_handler(
    State(state): State<AppState>,
    Path(dataset): Path<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    fuseki_proxy_request(state, dataset, "sparql", headers, body).await
}

async fn fuseki_update_handler(
    State(state): State<AppState>,
    Path(dataset): Path<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    fuseki_proxy_request(state, dataset, "update", headers, body).await
}

async fn fuseki_proxy_request(
    state: AppState,
    dataset: String,
    operation: &'static str,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let base = state.config.fuseki_url.trim().trim_end_matches('/');
    if base.is_empty() {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(serde_json::json!({
                "error": "Fuseki is not configured on the Rust server"
            })),
        )
            .into_response();
    }

    let target = format!("{base}/{dataset}/{operation}");
    let mut request = state.http.post(&target);
    tracing::info!(
        "Fuseki proxy {} dataset={} target={} auth={}",
        operation,
        dataset,
        target,
        if headers.get(header::AUTHORIZATION).is_some() || !state.config.fuseki_user.is_empty() {
            "yes"
        } else {
            "no"
        }
    );

    if let Some(value) = headers.get(header::AUTHORIZATION) {
        request = request.header(header::AUTHORIZATION, value);
    } else if !state.config.fuseki_user.is_empty() {
        request = request.basic_auth(
            &state.config.fuseki_user,
            Some(&state.config.fuseki_password),
        );
    }
    if let Some(value) = headers.get(header::ACCEPT) {
        request = request.header(header::ACCEPT, value);
    }
    if let Some(value) = headers.get(header::CONTENT_TYPE) {
        request = request.header(header::CONTENT_TYPE, value);
    }

    let upstream = match request.body(body.to_vec()).send().await {
        Ok(resp) => resp,
        Err(err) => {
            tracing::warn!("Fuseki proxy error for {target}: {err}");
            return (
                StatusCode::BAD_GATEWAY,
                Json(serde_json::json!({
                    "error": format!("Fuseki proxy request failed: {err}")
                })),
            )
                .into_response();
        }
    };

    let status =
        StatusCode::from_u16(upstream.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
    tracing::info!(
        "Fuseki proxy {} target={} status={}",
        operation,
        target,
        status
    );
    let mut response_headers = HeaderMap::new();
    if let Some(value) = upstream.headers().get(header::CONTENT_TYPE) {
        response_headers.insert(header::CONTENT_TYPE, value.clone());
    }

    match upstream.bytes().await {
        Ok(bytes) => (status, response_headers, bytes).into_response(),
        Err(err) => {
            tracing::warn!("Fuseki proxy body read error for {target}: {err}");
            (
                StatusCode::BAD_GATEWAY,
                Json(serde_json::json!({
                    "error": format!("Fuseki proxy response read failed: {err}")
                })),
            )
                .into_response()
        }
    }
}

async fn feed_handler(State(state): State<AppState>) -> impl IntoResponse {
    match &state.monitor {
        None => Json(serde_json::json!([])).into_response(),
        Some(arc) => {
            let monitor = arc.lock().await;
            let mut feed: Vec<serde_json::Value> = monitor
                .log_feed
                .iter()
                .take(50)
                .enumerate()
                .map(|(i, item)| {
                    let mut obj = item.clone();
                    if let Some(map) = obj.as_object_mut() {
                        map.entry("_seq").or_insert_with(|| serde_json::json!(i));
                    }
                    obj
                })
                .collect();
            // Chronological order (oldest first), matching Python's feed_handler
            feed.reverse();
            Json(feed).into_response()
        }
    }
}

async fn actor_history_handler(
    State(state): State<AppState>,
    Path(id): Path<String>,
) -> axum::response::Response {
    let entry = match state.system.registry.get(&id).await {
        Some(e) => e,
        None => return (StatusCode::NOT_FOUND, "actor not found").into_response(),
    };
    let actor_name = entry.name.clone();
    let data_dir = state.config.data_dir.clone();

    let result = tokio::task::spawn_blocking(move || {
        let db = wactorz_core::ActorPersistence::open(
            std::path::Path::new(&data_dir),
            &actor_name,
        )?;
        let history = db
            .get("conversation_history")
            .unwrap_or_else(|| serde_json::json!([]));
        let filtered: Vec<serde_json::Value> = history
            .as_array()
            .map(|arr| {
                arr.iter()
                    .filter(|msg| {
                        msg.get("role")
                            .and_then(|r| r.as_str())
                            .map(|r| r == "user" || r == "assistant")
                            .unwrap_or(false)
                    })
                    .cloned()
                    .collect()
            })
            .unwrap_or_default();
        Ok::<_, anyhow::Error>(filtered)
    })
    .await;

    match result {
        Ok(Ok(msgs)) => Json(msgs).into_response(),
        Ok(Err(e)) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

async fn chat_log_handler(
    State(state): State<AppState>,
    Query(params): Query<HashMap<String, String>>,
) -> axum::response::Response {
    let agent_filter = params.get("agent").cloned();
    let role_filter = params.get("role").cloned();
    let since: Option<f64> = params.get("since").and_then(|s| s.parse().ok());
    let limit: i64 = params.get("limit").and_then(|s| s.parse().ok()).unwrap_or(100);
    let data_dir = state.config.data_dir.clone();

    let result = tokio::task::spawn_blocking(move || {
        let db_path = std::path::Path::new(&data_dir).join("wactorz.db");
        let conn = rusqlite::Connection::open(&db_path)?;
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS chat_log (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      REAL,
                agent_name TEXT,
                role    TEXT,
                content TEXT,
                session_id TEXT
            );",
        )?;

        let mut sql = String::from(
            "SELECT id, ts, agent_name, role, content, session_id FROM chat_log",
        );
        let mut clauses: Vec<String> = Vec::new();
        let mut bind_vals: Vec<rusqlite::types::Value> = Vec::new();

        if let Some(ref a) = agent_filter {
            clauses.push("agent_name = ?".into());
            bind_vals.push(rusqlite::types::Value::Text(a.clone()));
        }
        if let Some(ref r) = role_filter {
            clauses.push("role = ?".into());
            bind_vals.push(rusqlite::types::Value::Text(r.clone()));
        }
        if let Some(s) = since {
            clauses.push("ts >= ?".into());
            bind_vals.push(rusqlite::types::Value::Real(s));
        }
        if !clauses.is_empty() {
            sql.push_str(" WHERE ");
            sql.push_str(&clauses.join(" AND "));
        }
        sql.push_str(" ORDER BY ts DESC LIMIT ?");
        bind_vals.push(rusqlite::types::Value::Integer(limit));

        let mut stmt = conn.prepare(&sql)?;
        let rows = stmt.query_map(
            rusqlite::params_from_iter(bind_vals.iter()),
            |row| {
                let id: i64 = row.get(0)?;
                let ts: f64 = row.get(1).unwrap_or(0.0);
                let agent_name: String = row.get(2).unwrap_or_default();
                let role: String = row.get(3).unwrap_or_default();
                let content: String = row.get(4).unwrap_or_default();
                let session_id: String = row.get(5).unwrap_or_default();
                Ok((id, ts, agent_name, role, content, session_id))
            },
        )?;

        let mut messages: Vec<serde_json::Value> = rows
            .filter_map(|r| r.ok())
            .map(|(id, ts, agent_name, role, content, session_id)| {
                serde_json::json!({
                    "id": id,
                    "ts": ts,
                    "agent_name": agent_name,
                    "role": role,
                    "content": content,
                    "session_id": session_id,
                })
            })
            .collect();
        messages.reverse(); // chronological order — oldest first, matching Python
        Ok::<_, anyhow::Error>(messages)
    })
    .await;

    match result {
        Ok(Ok(msgs)) => Json(msgs).into_response(),
        Ok(Err(e)) => {
            tracing::warn!("chat_log query error: {e}");
            (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response()
        }
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

async fn chat_handler(
    State(state): State<AppState>,
    Json(body): Json<ChatRequest>,
) -> axum::response::Response {
    let target_name = body.agent_name.as_deref().unwrap_or("main-actor");
    match state.system.registry.get_by_name(target_name).await {
        None => (
            StatusCode::NOT_FOUND,
            format!("agent '{target_name}' not found"),
        )
            .into_response(),
        Some(entry) => {
            let msg = Message::text(None, Some(entry.id.clone()), body.message);
            match state.system.registry.send(&entry.id, msg).await {
                Ok(_) => Json(serde_json::json!({"status": "sent", "agent": target_name}))
                    .into_response(),
                Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
            }
        }
    }
}

// ── Reset ────────────────────────────────────────────────────────────────────

async fn reset_handler(
    State(state): State<AppState>,
    Json(body): Json<ResetRequest>,
) -> axum::response::Response {
    const VALID: &[&str] = &["chat", "state", "metrics", "spawns", "logs", "all"];
    if !VALID.contains(&body.scope.as_str()) {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({ "error": format!("scope must be one of {VALID:?}") })),
        )
            .into_response();
    }
    let data_dir = std::path::PathBuf::from(&state.config.data_dir);
    let scope = body.scope.clone();
    let agent = body.agent.clone();
    let result =
        tokio::task::spawn_blocking(move || do_reset(&data_dir, &scope, agent.as_deref())).await;
    match result {
        Ok(Ok(())) => Json(
            serde_json::json!({ "status": "ok", "scope": body.scope, "agent": body.agent }),
        )
        .into_response(),
        Ok(Err(e)) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

fn do_reset(data_dir: &std::path::Path, scope: &str, agent: Option<&str>) -> anyhow::Result<()> {
    match scope {
        "chat" => do_reset_chat(data_dir, agent),
        "state" => do_reset_state(data_dir, agent),
        "metrics" => do_reset_metrics(data_dir, agent),
        "spawns" => do_reset_spawns(data_dir, agent),
        "logs" => do_reset_logs(data_dir),
        "all" => {
            do_reset_chat(data_dir, agent)?;
            do_reset_state(data_dir, agent)?;
            do_reset_metrics(data_dir, agent)?;
            do_reset_spawns(data_dir, agent)?;
            do_reset_logs(data_dir)
        }
        _ => Ok(()),
    }
}

fn do_reset_chat(data_dir: &std::path::Path, agent: Option<&str>) -> anyhow::Result<()> {
    let db_path = data_dir.join("wactorz.db");
    if db_path.exists() {
        let conn = rusqlite::Connection::open(&db_path)?;
        let _ = conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS chat_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, agent_name TEXT, role TEXT, content TEXT, session_id TEXT
            );",
        );
        if let Some(name) = agent {
            let _ = conn.execute(
                "DELETE FROM chat_log WHERE agent_name = ?",
                rusqlite::params![name],
            );
        } else {
            let _ = conn.execute("DELETE FROM chat_log", []);
        }
    }
    for_actor_dbs(data_dir, agent, |conn| {
        let _ = conn.execute(
            "DELETE FROM kv_store WHERE key IN ('conversation_history', 'history_summary')",
            [],
        );
    });
    Ok(())
}

fn do_reset_state(data_dir: &std::path::Path, agent: Option<&str>) -> anyhow::Result<()> {
    for_actor_dbs(data_dir, agent, |conn| {
        let _ = conn.execute("DELETE FROM kv_store", []);
    });
    Ok(())
}

fn do_reset_metrics(data_dir: &std::path::Path, agent: Option<&str>) -> anyhow::Result<()> {
    for_actor_dbs(data_dir, agent, |conn| {
        let _ = conn.execute(
            "DELETE FROM kv_store WHERE key IN ('_final_cost', '_messages_processed')",
            [],
        );
    });
    Ok(())
}

fn do_reset_spawns(data_dir: &std::path::Path, agent: Option<&str>) -> anyhow::Result<()> {
    let db_path = data_dir.join("wactorz.db");
    if db_path.exists() && let Ok(conn) = rusqlite::Connection::open(&db_path) {
        if let Some(name) = agent {
            let _ = conn.execute(
                "DELETE FROM spawn_registry WHERE name = ? OR spawned_by = ?",
                rusqlite::params![name, name],
            );
        } else {
            let _ = conn.execute("DELETE FROM spawn_registry", []);
        }
    }
    Ok(())
}

fn do_reset_logs(data_dir: &std::path::Path) -> anyhow::Result<()> {
    for name in &["wactorz.log", "monitor.log"] {
        let p = data_dir.join(name);
        if p.exists() {
            std::fs::write(&p, b"")?;
        }
    }
    Ok(())
}

/// Run `f` against the SQLite kv_store of every matching actor DB under `<data_dir>/actors/`.
fn for_actor_dbs<F: FnMut(&rusqlite::Connection)>(
    data_dir: &std::path::Path,
    agent: Option<&str>,
    mut f: F,
) {
    let actors_dir = data_dir.join("actors");
    if !actors_dir.exists() {
        return;
    }
    let Ok(entries) = std::fs::read_dir(&actors_dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) != Some("db") {
            continue;
        }
        if let Some(name) = agent {
            let safe: String = name
                .chars()
                .map(|c| if c.is_alphanumeric() || c == '-' || c == '_' { c } else { '_' })
                .collect();
            let stem = path.file_stem().and_then(|s| s.to_str()).unwrap_or("");
            if stem != safe {
                continue;
            }
        }
        if let Ok(conn) = rusqlite::Connection::open(&path) {
            f(&conn);
        }
    }
}

// ── Cost ─────────────────────────────────────────────────────────────────────

async fn cost_handler(State(state): State<AppState>) -> axum::response::Response {
    let data_dir = std::path::PathBuf::from(&state.config.data_dir);
    let result =
        tokio::task::spawn_blocking(move || aggregate_cost(&data_dir)).await;
    match result {
        Ok(info) => Json(info).into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

fn aggregate_cost(data_dir: &std::path::Path) -> serde_json::Value {
    let actors_dir = data_dir.join("actors");
    let mut total_cost = 0.0f64;
    let mut total_input = 0u64;
    let mut total_output = 0u64;
    let mut agents: Vec<serde_json::Value> = Vec::new();

    if actors_dir.exists() && let Ok(entries) = std::fs::read_dir(&actors_dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().and_then(|e| e.to_str()) != Some("db") {
                continue;
            }
            let Ok(conn) = rusqlite::Connection::open(&path) else {
                continue;
            };
            let Ok(raw) = conn.query_row(
                "SELECT value FROM kv_store WHERE key = '_final_cost'",
                [],
                |row| row.get::<_, String>(0),
            ) else {
                continue;
            };
            let Ok(v) = serde_json::from_str::<serde_json::Value>(&raw) else {
                continue;
            };
            let cost = v.get("cost_usd").and_then(|x| x.as_f64()).unwrap_or(0.0);
            let input = v.get("input_tokens").and_then(|x| x.as_u64()).unwrap_or(0);
            let output = v.get("output_tokens").and_then(|x| x.as_u64()).unwrap_or(0);
            let name = v.get("name").and_then(|x| x.as_str()).unwrap_or("").to_string();
            total_cost += cost;
            total_input += input;
            total_output += output;
            agents.push(serde_json::json!({
                "name": name,
                "cost_usd": cost,
                "input_tokens": input,
                "output_tokens": output,
            }));
        }
    }

    let (limit_usd, period) = read_cost_limit(data_dir);
    serde_json::json!({
        "total_cost_usd": (total_cost * 1_000_000.0).round() / 1_000_000.0,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "limit_usd": limit_usd,
        "period": period,
        "agents": agents,
    })
}

fn read_cost_limit(data_dir: &std::path::Path) -> (f64, String) {
    let db_path = data_dir.join("wactorz.db");
    if !db_path.exists() {
        return (0.0, "monthly".to_string());
    }
    let Ok(conn) = rusqlite::Connection::open(&db_path) else {
        return (0.0, "monthly".to_string());
    };
    let _ = conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS kv_store (key TEXT PRIMARY KEY, value TEXT NOT NULL);",
    );
    let Ok(raw) = conn.query_row(
        "SELECT value FROM kv_store WHERE key = 'cost_limit'",
        [],
        |row| row.get::<_, String>(0),
    ) else {
        return (0.0, "monthly".to_string());
    };
    let Ok(v) = serde_json::from_str::<serde_json::Value>(&raw) else {
        return (0.0, "monthly".to_string());
    };
    let limit = v.get("limit_usd").and_then(|x| x.as_f64()).unwrap_or(0.0);
    let period = v
        .get("period")
        .and_then(|x| x.as_str())
        .unwrap_or("monthly")
        .to_string();
    (limit, period)
}

async fn cost_limit_handler(
    State(state): State<AppState>,
    Json(body): Json<CostLimitRequest>,
) -> axum::response::Response {
    if !["daily", "weekly", "monthly"].contains(&body.period.as_str()) {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({ "error": "period must be daily, weekly, or monthly" })),
        )
            .into_response();
    }
    let data_dir = std::path::PathBuf::from(&state.config.data_dir);
    let limit_usd = body.limit_usd;
    let period = body.period.clone();
    let result = tokio::task::spawn_blocking(move || {
        let db_path = data_dir.join("wactorz.db");
        let conn = rusqlite::Connection::open(&db_path)?;
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS kv_store (key TEXT PRIMARY KEY, value TEXT NOT NULL);",
        )?;
        let val = serde_json::to_string(&serde_json::json!({ "limit_usd": limit_usd, "period": period }))?;
        conn.execute(
            "INSERT INTO kv_store (key, value) VALUES ('cost_limit', ?1)
             ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            rusqlite::params![val],
        )?;
        Ok::<_, anyhow::Error>(())
    })
    .await;
    match result {
        Ok(Ok(())) => Json(
            serde_json::json!({ "ok": true, "limit_usd": body.limit_usd, "period": body.period }),
        )
        .into_response(),
        Ok(Err(e)) => (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({ "error": e.to_string() })),
        )
            .into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

async fn cost_reset_handler(State(state): State<AppState>) -> axum::response::Response {
    let data_dir = std::path::PathBuf::from(&state.config.data_dir);
    let result = tokio::task::spawn_blocking(move || {
        for_actor_dbs(&data_dir, None, |conn| {
            let _ = conn.execute("DELETE FROM kv_store WHERE key = '_final_cost'", []);
        });
        Ok::<_, anyhow::Error>(())
    })
    .await;
    match result {
        Ok(Ok(())) => {
            Json(serde_json::json!({ "ok": true, "total_cost_usd": 0.0 })).into_response()
        }
        Ok(Err(e)) => (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({ "error": e.to_string() })),
        )
            .into_response(),
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
    }
}

// ── TTS ──────────────────────────────────────────────────────────────────────

async fn tts_voices_handler() -> axum::response::Response {
    let output = tokio::process::Command::new("edge-tts")
        .args(["--list-voices"])
        .output()
        .await;
    match output {
        Ok(out) if out.status.success() => {
            let text = String::from_utf8_lossy(&out.stdout);
            // Parse "Name: ...\nGender: ...\n\n" blocks
            let mut voices: Vec<serde_json::Value> = Vec::new();
            let mut name = String::new();
            let mut gender = String::new();
            for line in text.lines() {
                if let Some(v) = line.strip_prefix("Name: ") {
                    name = v.trim().to_string();
                } else if let Some(v) = line.strip_prefix("Gender: ") {
                    gender = v.trim().to_string();
                } else if line.trim().is_empty() && !name.is_empty() {
                    let locale: String = name.splitn(3, '-').take(2).collect::<Vec<_>>().join("-");
                    voices.push(serde_json::json!({ "name": name, "locale": locale, "gender": gender }));
                    name.clear();
                    gender.clear();
                }
            }
            if !name.is_empty() {
                let locale: String = name.splitn(3, '-').take(2).collect::<Vec<_>>().join("-");
                voices.push(serde_json::json!({ "name": name, "locale": locale, "gender": gender }));
            }
            Json(voices).into_response()
        }
        _ => Json(serde_json::json!([])).into_response(),
    }
}

async fn tts_handler(Query(params): Query<HashMap<String, String>>) -> axum::response::Response {
    let text = params.get("text").map(|s| s.trim().to_string()).unwrap_or_default();
    if text.is_empty() {
        return (StatusCode::BAD_REQUEST, "text param required").into_response();
    }

    // Strip code blocks and cap at 300 chars — same as Python
    let clean: String = {
        let mut s = text.clone();
        while let (Some(start), Some(end)) = (s.find("```"), s.find("```").and_then(|i| s[i+3..].find("```").map(|j| i + 3 + j + 3))) {
            s = format!("{}code block{}", &s[..start], &s[end..]);
        }
        s.chars().take(300).collect()
    };

    let default_voice = std::env::var("TTS_VOICE").unwrap_or_else(|_| "en-US-JennyNeural".to_string());
    let voice = params.get("voice").cloned().unwrap_or(default_voice);

    let tmp = std::env::temp_dir().join(format!("wactorz_tts_{}.mp3", std::process::id()));
    let result = tokio::process::Command::new("edge-tts")
        .args(["--text", &clean, "--voice", &voice, "--write-media", tmp.to_str().unwrap_or("/tmp/tts.mp3")])
        .output()
        .await;

    match result {
        Ok(out) if out.status.success() => {
            match tokio::fs::read(&tmp).await {
                Ok(audio) => {
                    let _ = tokio::fs::remove_file(&tmp).await;
                    (
                        StatusCode::OK,
                        [(header::CONTENT_TYPE, "audio/mpeg"),
                         (header::CACHE_CONTROL, "no-store")],
                        audio,
                    )
                        .into_response()
                }
                Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
            }
        }
        _ => (
            StatusCode::SERVICE_UNAVAILABLE,
            "edge-tts not installed — pip install 'wactorz[tts]' or brew install edge-tts",
        )
            .into_response(),
    }
}

// ── HA bridge sync ────────────────────────────────────────────────────────────

async fn ha_sync_handler(State(state): State<AppState>) -> axum::response::Response {
    if state.config.ha_token.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(serde_json::json!({ "error": "HA_TOKEN not configured" })),
        )
            .into_response();
    }
    match state.system.registry.get_by_name("ha-state-bridge").await {
        None => (
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({ "error": "ha-state-bridge not running" })),
        )
            .into_response(),
        Some(entry) => {
            // Send Stop — the supervisor will restart the agent automatically
            let msg = Message::command(entry.id.clone(), ActorCommand::Stop);
            match state.system.registry.send(&entry.id, msg).await {
                Ok(_) => Json(serde_json::json!({ "status": "restarted" })).into_response(),
                Err(e) => (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response(),
            }
        }
    }
}

// ── Tests ─────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn setup_global_db(dir: &TempDir) -> rusqlite::Connection {
        let conn = rusqlite::Connection::open(dir.path().join("wactorz.db")).unwrap();
        conn.execute_batch(
            "CREATE TABLE chat_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, agent_name TEXT, role TEXT, content TEXT, session_id TEXT
             );
             CREATE TABLE spawn_registry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT, spawned_by TEXT
             );
             CREATE TABLE kv_store (key TEXT PRIMARY KEY, value TEXT NOT NULL);",
        )
        .unwrap();
        conn
    }

    fn setup_actor_db(dir: &TempDir, name: &str) -> rusqlite::Connection {
        let actors = dir.path().join("actors");
        std::fs::create_dir_all(&actors).unwrap();
        let conn = rusqlite::Connection::open(actors.join(format!("{name}.db"))).unwrap();
        conn.execute_batch(
            "CREATE TABLE kv_store (key TEXT PRIMARY KEY, value TEXT NOT NULL);",
        )
        .unwrap();
        conn
    }

    // ── reset_chat ────────────────────────────────────────────────────────────

    #[test]
    fn reset_chat_clears_all_chat_log_rows() {
        let dir = TempDir::new().unwrap();
        let conn = setup_global_db(&dir);
        conn.execute(
            "INSERT INTO chat_log (ts, agent_name, role, content, session_id) VALUES (1.0, 'a', 'user', 'hi', 's1')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO chat_log (ts, agent_name, role, content, session_id) VALUES (2.0, 'b', 'assistant', 'hello', 's1')",
            [],
        )
        .unwrap();
        drop(conn);

        do_reset_chat(dir.path(), None).unwrap();

        let conn = rusqlite::Connection::open(dir.path().join("wactorz.db")).unwrap();
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM chat_log", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 0);
    }

    #[test]
    fn reset_chat_with_agent_filter_only_removes_that_agent() {
        let dir = TempDir::new().unwrap();
        let conn = setup_global_db(&dir);
        conn.execute("INSERT INTO chat_log (ts, agent_name, role, content, session_id) VALUES (1.0, 'keep', 'user', 'x', 's')", []).unwrap();
        conn.execute("INSERT INTO chat_log (ts, agent_name, role, content, session_id) VALUES (2.0, 'remove', 'user', 'y', 's')", []).unwrap();
        drop(conn);

        do_reset_chat(dir.path(), Some("remove")).unwrap();

        let conn = rusqlite::Connection::open(dir.path().join("wactorz.db")).unwrap();
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM chat_log", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 1, "only 'remove' rows should be deleted");
    }

    #[test]
    fn reset_chat_clears_history_kv_in_actor_db() {
        let dir = TempDir::new().unwrap();
        setup_global_db(&dir);
        let actor = setup_actor_db(&dir, "my-agent");
        actor.execute("INSERT INTO kv_store VALUES ('conversation_history', '[]')", []).unwrap();
        actor.execute("INSERT INTO kv_store VALUES ('history_summary', '\"\"')", []).unwrap();
        actor.execute("INSERT INTO kv_store VALUES ('other_key', '\"keep\"')", []).unwrap();
        drop(actor);

        do_reset_chat(dir.path(), None).unwrap();

        let actor = rusqlite::Connection::open(dir.path().join("actors/my-agent.db")).unwrap();
        let remaining: Vec<String> = {
            let mut stmt = actor.prepare("SELECT key FROM kv_store").unwrap();
            stmt.query_map([], |r| r.get(0))
                .unwrap()
                .flatten()
                .collect()
        };
        assert!(!remaining.contains(&"conversation_history".to_string()));
        assert!(!remaining.contains(&"history_summary".to_string()));
        assert!(remaining.contains(&"other_key".to_string()));
    }

    // ── reset_metrics ─────────────────────────────────────────────────────────

    #[test]
    fn reset_metrics_removes_cost_and_message_count_keys() {
        let dir = TempDir::new().unwrap();
        setup_global_db(&dir);
        let actor = setup_actor_db(&dir, "llm");
        actor.execute("INSERT INTO kv_store VALUES ('_final_cost', '{\"cost_usd\":0.5}')", []).unwrap();
        actor.execute("INSERT INTO kv_store VALUES ('_messages_processed', '100')", []).unwrap();
        actor.execute("INSERT INTO kv_store VALUES ('other', '\"keep\"')", []).unwrap();
        drop(actor);

        do_reset_metrics(dir.path(), None).unwrap();

        let actor = rusqlite::Connection::open(dir.path().join("actors/llm.db")).unwrap();
        let remaining: Vec<String> = {
            let mut stmt = actor.prepare("SELECT key FROM kv_store").unwrap();
            stmt.query_map([], |r| r.get(0))
                .unwrap()
                .flatten()
                .collect()
        };
        assert!(!remaining.contains(&"_final_cost".to_string()));
        assert!(!remaining.contains(&"_messages_processed".to_string()));
        assert!(remaining.contains(&"other".to_string()));
    }

    // ── reset_logs ────────────────────────────────────────────────────────────

    #[test]
    fn reset_logs_truncates_existing_log_files() {
        let dir = TempDir::new().unwrap();
        std::fs::write(dir.path().join("wactorz.log"), b"old log content").unwrap();
        std::fs::write(dir.path().join("monitor.log"), b"monitor output").unwrap();

        do_reset_logs(dir.path()).unwrap();

        assert_eq!(std::fs::read(dir.path().join("wactorz.log")).unwrap(), b"");
        assert_eq!(std::fs::read(dir.path().join("monitor.log")).unwrap(), b"");
    }

    #[test]
    fn reset_logs_does_not_error_when_log_files_absent() {
        let dir = TempDir::new().unwrap();
        assert!(do_reset_logs(dir.path()).is_ok());
    }

    // ── aggregate_cost ────────────────────────────────────────────────────────

    #[test]
    fn aggregate_cost_sums_across_all_actor_dbs() {
        let dir = TempDir::new().unwrap();
        for (name, cost, inp, out) in [("a", 0.01, 100u64, 50u64), ("b", 0.02, 200, 100)] {
            let actor = setup_actor_db(&dir, name);
            let val = serde_json::to_string(&serde_json::json!({
                "name": name, "cost_usd": cost,
                "input_tokens": inp, "output_tokens": out,
            }))
            .unwrap();
            actor.execute("INSERT INTO kv_store VALUES ('_final_cost', ?)", rusqlite::params![val]).unwrap();
        }

        let info = aggregate_cost(dir.path());
        let total = info["total_cost_usd"].as_f64().unwrap();
        assert!((total - 0.03).abs() < 1e-6, "total={total}");
        assert_eq!(info["total_input_tokens"].as_u64().unwrap(), 300);
        assert_eq!(info["total_output_tokens"].as_u64().unwrap(), 150);
        assert_eq!(info["agents"].as_array().unwrap().len(), 2);
    }

    #[test]
    fn aggregate_cost_returns_zero_when_no_actor_dbs() {
        let dir = TempDir::new().unwrap();
        let info = aggregate_cost(dir.path());
        assert_eq!(info["total_cost_usd"].as_f64().unwrap(), 0.0);
        assert_eq!(info["agents"].as_array().unwrap().len(), 0);
    }

    // ── read_cost_limit ───────────────────────────────────────────────────────

    #[test]
    fn read_cost_limit_returns_defaults_when_no_db() {
        let dir = TempDir::new().unwrap();
        let (limit, period) = read_cost_limit(dir.path());
        assert_eq!(limit, 0.0);
        assert_eq!(period, "monthly");
    }

    #[test]
    fn read_cost_limit_reads_stored_limit() {
        let dir = TempDir::new().unwrap();
        let conn = setup_global_db(&dir);
        let val = serde_json::to_string(&serde_json::json!({"limit_usd": 5.0, "period": "weekly"})).unwrap();
        conn.execute("INSERT INTO kv_store VALUES ('cost_limit', ?)", rusqlite::params![val]).unwrap();
        drop(conn);

        let (limit, period) = read_cost_limit(dir.path());
        assert!((limit - 5.0).abs() < 1e-9);
        assert_eq!(period, "weekly");
    }

    // ── do_reset (integration) ────────────────────────────────────────────────

    #[test]
    fn do_reset_all_scope_clears_everything() {
        let dir = TempDir::new().unwrap();
        let conn = setup_global_db(&dir);
        conn.execute("INSERT INTO chat_log (ts, agent_name, role, content, session_id) VALUES (1.0, 'x', 'user', 'hi', 's')", []).unwrap();
        drop(conn);
        let actor = setup_actor_db(&dir, "agent");
        actor.execute("INSERT INTO kv_store VALUES ('_final_cost', '{\"cost_usd\":1.0}')", []).unwrap();
        actor.execute("INSERT INTO kv_store VALUES ('conversation_history', '[]')", []).unwrap();
        drop(actor);
        std::fs::write(dir.path().join("wactorz.log"), b"stuff").unwrap();

        do_reset(dir.path(), "all", None).unwrap();

        let conn = rusqlite::Connection::open(dir.path().join("wactorz.db")).unwrap();
        let chat_count: i64 = conn.query_row("SELECT COUNT(*) FROM chat_log", [], |r| r.get(0)).unwrap();
        assert_eq!(chat_count, 0);
        assert_eq!(std::fs::read(dir.path().join("wactorz.log")).unwrap(), b"");
    }

    #[test]
    fn do_reset_rejects_unknown_scope_silently() {
        let dir = TempDir::new().unwrap();
        // Unknown scope is already rejected by the handler before calling do_reset,
        // but do_reset itself should not error — it's a no-op.
        assert!(do_reset(dir.path(), "nonexistent", None).is_ok());
    }

    // ── do_reset_state ────────────────────────────────────────────────────────

    #[test]
    fn do_reset_state_clears_kv_store() {
        let dir = TempDir::new().unwrap();
        let actor = setup_actor_db(&dir, "state-agent");
        actor.execute("INSERT INTO kv_store VALUES ('some_key', '\"val\"')", []).unwrap();
        drop(actor);

        do_reset_state(dir.path(), None).unwrap();

        let actor = rusqlite::Connection::open(dir.path().join("actors/state-agent.db")).unwrap();
        let count: i64 = actor.query_row("SELECT COUNT(*) FROM kv_store", [], |r| r.get(0)).unwrap();
        assert_eq!(count, 0);
    }

    // ── do_reset_spawns ───────────────────────────────────────────────────────

    #[test]
    fn do_reset_spawns_clears_spawn_registry() {
        let dir = TempDir::new().unwrap();
        let conn = setup_global_db(&dir);
        conn.execute("INSERT INTO spawn_registry (name, spawned_by) VALUES ('child', 'main')", []).unwrap();
        drop(conn);

        do_reset_spawns(dir.path(), None).unwrap();

        let conn = rusqlite::Connection::open(dir.path().join("wactorz.db")).unwrap();
        let count: i64 = conn.query_row("SELECT COUNT(*) FROM spawn_registry", [], |r| r.get(0)).unwrap();
        assert_eq!(count, 0);
    }

    #[test]
    fn do_reset_spawns_with_agent_filter() {
        let dir = TempDir::new().unwrap();
        let conn = setup_global_db(&dir);
        conn.execute("INSERT INTO spawn_registry (name, spawned_by) VALUES ('keep', 'main')", []).unwrap();
        conn.execute("INSERT INTO spawn_registry (name, spawned_by) VALUES ('remove', 'main')", []).unwrap();
        drop(conn);

        do_reset_spawns(dir.path(), Some("remove")).unwrap();

        let conn = rusqlite::Connection::open(dir.path().join("wactorz.db")).unwrap();
        let count: i64 = conn.query_row("SELECT COUNT(*) FROM spawn_registry", [], |r| r.get(0)).unwrap();
        assert_eq!(count, 1);
    }

    #[test]
    fn do_reset_spawns_no_db_is_ok() {
        let dir = TempDir::new().unwrap();
        assert!(do_reset_spawns(dir.path(), None).is_ok());
    }

    // ── for_actor_dbs with agent filter ──────────────────────────────────────

    #[test]
    fn for_actor_dbs_agent_filter_targets_only_named_db() {
        let dir = TempDir::new().unwrap();
        setup_actor_db(&dir, "target");
        setup_actor_db(&dir, "other");
        let mut visited = Vec::new();
        for_actor_dbs(dir.path(), Some("target"), |_conn| {
            visited.push("visited");
        });
        assert_eq!(visited.len(), 1);
    }

    // ── default_period ────────────────────────────────────────────────────────

    #[test]
    fn default_period_is_monthly() {
        assert_eq!(default_period(), "monthly");
    }

    // ── RuntimeConfig ─────────────────────────────────────────────────────────

    #[test]
    fn runtime_config_default_is_valid() {
        let c = RuntimeConfig::default();
        assert!(c.ha_url.is_empty());
        assert_eq!(c.mqtt_port, 0);
    }

    // ── RestServer construction ────────────────────────────────────────────────

    #[test]
    fn rest_server_new_and_router_build() {
        let dir = TempDir::new().unwrap();
        let sys = wactorz_core::ActorSystem::default();
        let config = RuntimeConfig {
            data_dir: dir.path().to_str().unwrap().to_string(),
            ..Default::default()
        };
        let server = RestServer::new(sys, "127.0.0.1:0".parse().unwrap(), config, dir.path().to_str().unwrap().to_string());
        let _router = server.router();
    }

    #[test]
    fn rest_server_with_monitor() {
        use crate::ws::MonitorState;
        use std::sync::Arc;
        use tokio::sync::Mutex;

        let dir = TempDir::new().unwrap();
        let sys = wactorz_core::ActorSystem::default();
        let config = RuntimeConfig {
            data_dir: dir.path().to_str().unwrap().to_string(),
            ..Default::default()
        };
        let monitor = Arc::new(Mutex::new(MonitorState::default()));
        let server = RestServer::new(sys, "127.0.0.1:0".parse().unwrap(), config, dir.path().to_str().unwrap().to_string())
            .with_monitor(monitor);
        let _router = server.router();
    }

    // ── Axum handler tests via tower::ServiceExt ──────────────────────────────

    fn make_state(dir: &TempDir) -> AppState {
        AppState {
            system: wactorz_core::ActorSystem::default(),
            config: RuntimeConfig {
                data_dir: dir.path().to_str().unwrap().to_string(),
                ..Default::default()
            },
            http: reqwest::Client::new(),
            monitor: None,
        }
    }

    fn build_router(dir: &TempDir) -> axum::Router {
        let sys = wactorz_core::ActorSystem::default();
        let config = RuntimeConfig {
            data_dir: dir.path().to_str().unwrap().to_string(),
            ..Default::default()
        };
        let server = RestServer::new(
            sys,
            "127.0.0.1:0".parse().unwrap(),
            config,
            dir.path().to_str().unwrap().to_string(),
        );
        server.router()
    }

    async fn body_bytes(resp: axum::response::Response) -> Vec<u8> {
        let bytes = axum::body::to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        bytes.to_vec()
    }

    #[tokio::test]
    async fn handler_health_returns_ok() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(Request::builder().uri("/health").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn handler_list_actors_empty_system() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(Request::builder().uri("/actors").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let bytes = body_bytes(resp).await;
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert!(v.as_array().unwrap().is_empty());
    }

    #[tokio::test]
    async fn handler_get_actor_not_found() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(Request::builder().uri("/actors/no-such-id").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn handler_send_message_actor_not_found() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/actors/no-such/message")
                    .header("Content-Type", "application/json")
                    .body(Body::from(r#"{"content":"hello"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn handler_stop_actor_not_found() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(
                Request::builder()
                    .method("DELETE")
                    .uri("/actors/no-such")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn handler_pause_actor_not_found() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/actors/no-such/pause")
                    .header("Content-Type", "application/json")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn handler_resume_actor_not_found() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/actors/no-such/resume")
                    .header("Content-Type", "application/json")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn handler_get_metrics_not_found() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(Request::builder().uri("/actors/no-such/metrics").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn handler_config_returns_json() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(Request::builder().uri("/api/config").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let bytes = body_bytes(resp).await;
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert!(v.get("ha").is_some());
        assert!(v.get("mqtt").is_some());
    }

    #[tokio::test]
    async fn handler_feed_without_monitor_returns_empty_array() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(Request::builder().uri("/api/feed").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let bytes = body_bytes(resp).await;
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert!(v.as_array().unwrap().is_empty());
    }

    #[tokio::test]
    async fn handler_feed_with_monitor_returns_log_feed() {
        use tower::ServiceExt;
        use axum::http::{Request, StatusCode};
        use axum::body::Body;
        use crate::ws::MonitorState;
        use std::sync::Arc;
        use tokio::sync::Mutex;

        let dir = TempDir::new().unwrap();
        let monitor = Arc::new(Mutex::new(MonitorState::default()));
        {
            let mut st = monitor.lock().await;
            st.log_feed.push(serde_json::json!({"type": "test", "msg": "hello"}));
        }

        let sys = wactorz_core::ActorSystem::default();
        let config = RuntimeConfig {
            data_dir: dir.path().to_str().unwrap().to_string(),
            ..Default::default()
        };
        let server = RestServer::new(
            sys,
            "127.0.0.1:0".parse().unwrap(),
            config,
            dir.path().to_str().unwrap().to_string(),
        )
        .with_monitor(monitor);

        let resp = server
            .router()
            .oneshot(Request::builder().uri("/api/feed").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let bytes = body_bytes(resp).await;
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert!(!v.as_array().unwrap().is_empty());
    }

    #[tokio::test]
    async fn handler_cost_with_no_actors_returns_zero() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(Request::builder().uri("/api/cost").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let bytes = body_bytes(resp).await;
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["total_cost_usd"].as_f64().unwrap(), 0.0);
    }

    #[tokio::test]
    async fn handler_cost_limit_invalid_period_returns_bad_request() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/cost/limit")
                    .header("Content-Type", "application/json")
                    .body(Body::from(r#"{"limit_usd": 5.0, "period": "yearly"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn handler_cost_limit_valid_returns_ok() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        setup_global_db(&dir);
        let resp = build_router(&dir)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/cost/limit")
                    .header("Content-Type", "application/json")
                    .body(Body::from(r#"{"limit_usd": 10.0, "period": "monthly"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn handler_cost_limit_no_period_uses_default_monthly() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        setup_global_db(&dir);
        let resp = build_router(&dir)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/cost/limit")
                    .header("Content-Type", "application/json")
                    .body(Body::from(r#"{"limit_usd": 5.0}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let bytes = body_bytes(resp).await;
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v["period"], "monthly");
    }

    #[tokio::test]
    async fn handler_cost_reset_returns_ok() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/cost/reset")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn handler_reset_invalid_scope_returns_bad_request() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/reset")
                    .header("Content-Type", "application/json")
                    .body(Body::from(r#"{"scope": "invalid"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn handler_reset_valid_scope_chat_returns_ok() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        setup_global_db(&dir);
        let resp = build_router(&dir)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/reset")
                    .header("Content-Type", "application/json")
                    .body(Body::from(r#"{"scope": "chat"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn handler_reset_valid_scope_all_returns_ok() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        setup_global_db(&dir);
        let resp = build_router(&dir)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/reset")
                    .header("Content-Type", "application/json")
                    .body(Body::from(r#"{"scope": "all"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn handler_chat_agent_not_found() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/chat")
                    .header("Content-Type", "application/json")
                    .body(Body::from(r#"{"message": "hello", "agent_name": "nonexistent"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn handler_chat_default_main_actor_not_found() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/chat")
                    .header("Content-Type", "application/json")
                    .body(Body::from(r#"{"message": "hello"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn handler_actor_history_not_found() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(Request::builder().uri("/actors/no-such/history").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn handler_chat_log_empty_db_returns_empty() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        setup_global_db(&dir);
        let resp = build_router(&dir)
            .oneshot(Request::builder().uri("/api/chats").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let bytes = body_bytes(resp).await;
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert!(v.as_array().unwrap().is_empty());
    }

    #[tokio::test]
    async fn handler_chat_log_with_filters_returns_ok() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let conn = setup_global_db(&dir);
        conn.execute(
            "INSERT INTO chat_log (ts, agent_name, role, content, session_id) VALUES (1.0, 'alpha', 'user', 'hi', 's1')",
            [],
        ).unwrap();
        drop(conn);

        let resp = build_router(&dir)
            .oneshot(
                Request::builder()
                    .uri("/api/chats?agent=alpha&role=user&since=0&limit=10")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let bytes = body_bytes(resp).await;
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v.as_array().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn handler_fuseki_sparql_empty_url_returns_503() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/fuseki/mydb/sparql")
                    .header("Content-Type", "application/sparql-query")
                    .body(Body::from("SELECT * WHERE { ?s ?p ?o }"))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn handler_tts_empty_text_returns_bad_request() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(Request::builder().uri("/api/tts?text=").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn handler_tts_no_text_param_returns_bad_request() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(Request::builder().uri("/api/tts").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn handler_tts_with_text_and_no_edge_tts_returns_503() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(Request::builder().uri("/api/tts?text=hello+world").body(Body::empty()).unwrap())
            .await
            .unwrap();
        // edge-tts is not installed in test env → 503
        assert!(resp.status() == StatusCode::SERVICE_UNAVAILABLE || resp.status() == StatusCode::OK);
    }

    #[tokio::test]
    async fn handler_tts_voices_returns_array() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(Request::builder().uri("/api/tts/voices").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let bytes = body_bytes(resp).await;
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert!(v.is_array()); // empty [] when edge-tts not installed
    }

    #[tokio::test]
    async fn handler_ha_sync_empty_token_returns_bad_request() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/ha/sync")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn handler_ha_sync_no_ha_bridge_returns_not_found() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        // Provide a non-empty token so we get past the empty-token guard
        let sys = wactorz_core::ActorSystem::default();
        let config = RuntimeConfig {
            ha_token: "test-token".to_string(),
            data_dir: dir.path().to_str().unwrap().to_string(),
            ..Default::default()
        };
        let server = RestServer::new(sys, "127.0.0.1:0".parse().unwrap(), config, dir.path().to_str().unwrap().to_string());
        let resp = server
            .router()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/ha/sync")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    }

    // ── Actor interaction tests (with registered actors) ──────────────────────

    fn make_test_actor_entry(name: &str, protected: bool) -> (wactorz_core::ActorEntry, tokio::sync::mpsc::Receiver<wactorz_core::message::Message>) {
        use wactorz_core::ActorMetrics;
        use wactorz_core::ActorState;
        use std::sync::Arc;

        let (tx, rx) = tokio::sync::mpsc::channel(10);
        let entry = wactorz_core::ActorEntry {
            id: format!("{name}-test-id"),
            name: name.to_string(),
            state: ActorState::Running,
            mailbox: tx,
            protected,
            metrics: Arc::new(ActorMetrics::new()),
            supervisor_id: None,
        };
        (entry, rx)
    }

    #[tokio::test]
    async fn handler_get_actor_found() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let sys = wactorz_core::ActorSystem::default();
        let (entry, _rx) = make_test_actor_entry("test-actor", false);
        let id = entry.id.clone();
        sys.registry.register(entry).await;

        let config = RuntimeConfig { data_dir: dir.path().to_str().unwrap().to_string(), ..Default::default() };
        let server = RestServer::new(sys, "127.0.0.1:0".parse().unwrap(), config, dir.path().to_str().unwrap().to_string());
        let resp = server.router()
            .oneshot(Request::builder().uri(format!("/actors/{id}")).body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn handler_get_metrics_found() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let sys = wactorz_core::ActorSystem::default();
        let (entry, _rx) = make_test_actor_entry("metrics-actor", false);
        let id = entry.id.clone();
        sys.registry.register(entry).await;

        let config = RuntimeConfig { data_dir: dir.path().to_str().unwrap().to_string(), ..Default::default() };
        let server = RestServer::new(sys, "127.0.0.1:0".parse().unwrap(), config, dir.path().to_str().unwrap().to_string());
        let resp = server.router()
            .oneshot(Request::builder().uri(format!("/actors/{id}/metrics")).body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn handler_stop_actor_found_and_not_protected() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let sys = wactorz_core::ActorSystem::default();
        let (entry, _rx) = make_test_actor_entry("stop-actor", false);
        let id = entry.id.clone();
        sys.registry.register(entry).await;

        let config = RuntimeConfig { data_dir: dir.path().to_str().unwrap().to_string(), ..Default::default() };
        let server = RestServer::new(sys, "127.0.0.1:0".parse().unwrap(), config, dir.path().to_str().unwrap().to_string());
        let resp = server.router()
            .oneshot(Request::builder().method("DELETE").uri(format!("/actors/{id}")).body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn handler_stop_actor_protected_returns_forbidden() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let sys = wactorz_core::ActorSystem::default();
        let (entry, _rx) = make_test_actor_entry("prot-actor", true);
        let id = entry.id.clone();
        sys.registry.register(entry).await;

        let config = RuntimeConfig { data_dir: dir.path().to_str().unwrap().to_string(), ..Default::default() };
        let server = RestServer::new(sys, "127.0.0.1:0".parse().unwrap(), config, dir.path().to_str().unwrap().to_string());
        let resp = server.router()
            .oneshot(Request::builder().method("DELETE").uri(format!("/actors/{id}")).body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    }

    #[tokio::test]
    async fn handler_pause_actor_found_and_not_protected() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let sys = wactorz_core::ActorSystem::default();
        let (entry, _rx) = make_test_actor_entry("pause-actor", false);
        let id = entry.id.clone();
        sys.registry.register(entry).await;

        let config = RuntimeConfig { data_dir: dir.path().to_str().unwrap().to_string(), ..Default::default() };
        let server = RestServer::new(sys, "127.0.0.1:0".parse().unwrap(), config, dir.path().to_str().unwrap().to_string());
        let resp = server.router()
            .oneshot(Request::builder().method("POST").uri(format!("/actors/{id}/pause")).body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn handler_pause_actor_protected_returns_forbidden() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let sys = wactorz_core::ActorSystem::default();
        let (entry, _rx) = make_test_actor_entry("prot-pause", true);
        let id = entry.id.clone();
        sys.registry.register(entry).await;

        let config = RuntimeConfig { data_dir: dir.path().to_str().unwrap().to_string(), ..Default::default() };
        let server = RestServer::new(sys, "127.0.0.1:0".parse().unwrap(), config, dir.path().to_str().unwrap().to_string());
        let resp = server.router()
            .oneshot(Request::builder().method("POST").uri(format!("/actors/{id}/pause")).body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    }

    #[tokio::test]
    async fn handler_resume_actor_found_and_not_protected() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let sys = wactorz_core::ActorSystem::default();
        let (entry, _rx) = make_test_actor_entry("resume-actor", false);
        let id = entry.id.clone();
        sys.registry.register(entry).await;

        let config = RuntimeConfig { data_dir: dir.path().to_str().unwrap().to_string(), ..Default::default() };
        let server = RestServer::new(sys, "127.0.0.1:0".parse().unwrap(), config, dir.path().to_str().unwrap().to_string());
        let resp = server.router()
            .oneshot(Request::builder().method("POST").uri(format!("/actors/{id}/resume")).body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn handler_resume_actor_protected_returns_forbidden() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let sys = wactorz_core::ActorSystem::default();
        let (entry, _rx) = make_test_actor_entry("prot-resume", true);
        let id = entry.id.clone();
        sys.registry.register(entry).await;

        let config = RuntimeConfig { data_dir: dir.path().to_str().unwrap().to_string(), ..Default::default() };
        let server = RestServer::new(sys, "127.0.0.1:0".parse().unwrap(), config, dir.path().to_str().unwrap().to_string());
        let resp = server.router()
            .oneshot(Request::builder().method("POST").uri(format!("/actors/{id}/resume")).body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    }

    #[tokio::test]
    async fn handler_send_message_found_actor() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let sys = wactorz_core::ActorSystem::default();
        let (entry, _rx) = make_test_actor_entry("msg-actor", false);
        let id = entry.id.clone();
        sys.registry.register(entry).await;

        let config = RuntimeConfig { data_dir: dir.path().to_str().unwrap().to_string(), ..Default::default() };
        let server = RestServer::new(sys, "127.0.0.1:0".parse().unwrap(), config, dir.path().to_str().unwrap().to_string());
        let resp = server.router()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri(format!("/actors/{id}/message"))
                    .header("Content-Type", "application/json")
                    .body(Body::from(r#"{"content":"test message"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn handler_chat_found_actor() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let sys = wactorz_core::ActorSystem::default();
        let (entry, _rx) = make_test_actor_entry("main-actor", false);
        sys.registry.register(entry).await;

        let config = RuntimeConfig { data_dir: dir.path().to_str().unwrap().to_string(), ..Default::default() };
        let server = RestServer::new(sys, "127.0.0.1:0".parse().unwrap(), config, dir.path().to_str().unwrap().to_string());
        let resp = server.router()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/chat")
                    .header("Content-Type", "application/json")
                    .body(Body::from(r#"{"message": "hello"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn handler_actor_history_found_with_db() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let sys = wactorz_core::ActorSystem::default();
        let (entry, _rx) = make_test_actor_entry("history-actor", false);
        let id = entry.id.clone();
        sys.registry.register(entry).await;

        // Create actor DB with conversation_history
        let actor_db = setup_actor_db(&dir, "history-actor");
        let history = serde_json::json!([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]);
        actor_db.execute(
            "INSERT INTO kv_store VALUES ('conversation_history', ?)",
            rusqlite::params![serde_json::to_string(&history).unwrap()],
        ).unwrap();
        drop(actor_db);

        let config = RuntimeConfig { data_dir: dir.path().to_str().unwrap().to_string(), ..Default::default() };
        let server = RestServer::new(sys, "127.0.0.1:0".parse().unwrap(), config, dir.path().to_str().unwrap().to_string());
        let resp = server.router()
            .oneshot(Request::builder().uri(format!("/actors/{id}/history")).body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let bytes = body_bytes(resp).await;
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v.as_array().unwrap().len(), 2);
    }

    #[test]
    fn rest_server_with_ws_merges_router() {
        let dir = TempDir::new().unwrap();
        let sys = wactorz_core::ActorSystem::default();
        let config = RuntimeConfig {
            data_dir: dir.path().to_str().unwrap().to_string(),
            ..Default::default()
        };
        let ws_router = axum::Router::new();
        let server = RestServer::new(sys, "127.0.0.1:0".parse().unwrap(), config, dir.path().to_str().unwrap().to_string())
            .with_ws(ws_router);
        let _router = server.router();
    }

    #[tokio::test]
    async fn handler_ha_sync_with_token_and_ha_bridge_found() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let sys = wactorz_core::ActorSystem::default();
        let (entry, _rx) = make_test_actor_entry("ha-state-bridge", false);
        sys.registry.register(entry).await;

        let config = RuntimeConfig {
            ha_token: "test-token".to_string(),
            data_dir: dir.path().to_str().unwrap().to_string(),
            ..Default::default()
        };
        let server = RestServer::new(sys, "127.0.0.1:0".parse().unwrap(), config, dir.path().to_str().unwrap().to_string());
        let resp = server
            .router()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/ha/sync")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn handler_reset_state_scope_returns_ok() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        setup_global_db(&dir);
        let resp = build_router(&dir)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/reset")
                    .header("Content-Type", "application/json")
                    .body(Body::from(r#"{"scope": "state"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn handler_reset_metrics_scope_returns_ok() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        setup_global_db(&dir);
        let resp = build_router(&dir)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/reset")
                    .header("Content-Type", "application/json")
                    .body(Body::from(r#"{"scope": "metrics"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn handler_reset_spawns_scope_returns_ok() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        setup_global_db(&dir);
        let resp = build_router(&dir)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/reset")
                    .header("Content-Type", "application/json")
                    .body(Body::from(r#"{"scope": "spawns"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn handler_reset_logs_scope_returns_ok() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/reset")
                    .header("Content-Type", "application/json")
                    .body(Body::from(r#"{"scope": "logs"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn handler_reset_with_agent_filter_returns_ok() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        setup_global_db(&dir);
        setup_actor_db(&dir, "my-agent");
        let resp = build_router(&dir)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/reset")
                    .header("Content-Type", "application/json")
                    .body(Body::from(r#"{"scope": "chat", "agent": "my-agent"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn handler_list_actors_api_alias_works() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(Request::builder().uri("/api/actors").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn handler_cost_limit_daily_period_returns_ok() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        setup_global_db(&dir);
        let resp = build_router(&dir)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/cost/limit")
                    .header("Content-Type", "application/json")
                    .body(Body::from(r#"{"limit_usd": 1.0, "period": "daily"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn handler_cost_limit_weekly_period_returns_ok() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        setup_global_db(&dir);
        let resp = build_router(&dir)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/cost/limit")
                    .header("Content-Type", "application/json")
                    .body(Body::from(r#"{"limit_usd": 2.0, "period": "weekly"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn handler_fuseki_update_empty_url_returns_503() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let resp = build_router(&dir)
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/fuseki/mydb/update")
                    .header("Content-Type", "application/sparql-update")
                    .body(Body::from("DELETE WHERE { ?s ?p ?o }"))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[test]
    fn aggregate_cost_skips_non_db_and_entries_without_cost() {
        let dir = TempDir::new().unwrap();
        let actors_dir = dir.path().join("actors");
        std::fs::create_dir_all(&actors_dir).unwrap();

        // Non-db file → skipped (covers continue at non-db extension)
        std::fs::write(actors_dir.join("readme.txt"), b"ignore me").unwrap();

        // DB with no kv_store table → query_row fails → skipped (covers missing key continue)
        let empty_path = actors_dir.join("empty.db");
        let _ = rusqlite::Connection::open(&empty_path).unwrap();

        // DB with non-JSON value → serde_json parse fails → skipped (covers bad-json continue)
        let bad_conn = setup_actor_db(&dir, "badjson");
        bad_conn
            .execute("INSERT INTO kv_store VALUES ('_final_cost', 'INVALID')", [])
            .unwrap();
        drop(bad_conn);

        let info = aggregate_cost(dir.path());
        assert_eq!(info["total_cost_usd"].as_f64().unwrap(), 0.0);
        assert_eq!(info["agents"].as_array().unwrap().len(), 0);
    }

    #[tokio::test]
    async fn handler_cost_with_actor_db_returns_nonzero_total() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let conn = setup_actor_db(&dir, "cost-agent");
        let val = serde_json::to_string(&serde_json::json!({
            "name": "cost-agent",
            "cost_usd": 0.5,
            "input_tokens": 1000,
            "output_tokens": 500,
        }))
        .unwrap();
        conn.execute(
            "INSERT INTO kv_store VALUES ('_final_cost', ?)",
            rusqlite::params![val],
        )
        .unwrap();
        drop(conn);
        // Non-db file to exercise the extension-mismatch continue
        std::fs::write(dir.path().join("actors").join("notes.txt"), b"").unwrap();

        let resp = build_router(&dir)
            .oneshot(
                Request::builder()
                    .uri("/api/cost")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let bytes = body_bytes(resp).await;
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert!(v["total_cost_usd"].as_f64().unwrap() > 0.0);
        assert_eq!(v["agents"].as_array().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn make_state_builds_valid_app_state() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let state = make_state(&dir);
        // Verify the state has the expected defaults
        assert!(state.monitor.is_none());
        assert!(state.config.ha_token.is_empty());
        // Smoke-test: build a router using AppState directly via oneshot
        let router = build_router(&dir);
        let resp = router
            .oneshot(Request::builder().uri("/health").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        drop(state);
    }

    #[tokio::test]
    async fn handler_list_actors_with_registered_actor_returns_entry() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let sys = wactorz_core::ActorSystem::default();
        let (entry, _rx) = make_test_actor_entry("list-test", false);
        sys.registry.register(entry).await;
        let config = RuntimeConfig { data_dir: dir.path().to_str().unwrap().to_string(), ..Default::default() };
        let resp = RestServer::new(sys, "127.0.0.1:0".parse().unwrap(), config, dir.path().to_str().unwrap().to_string())
            .router()
            .oneshot(Request::builder().uri("/actors").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let bytes = body_bytes(resp).await;
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(v.as_array().unwrap().len(), 1);
        assert_eq!(v[0]["name"], "list-test");
    }

    #[tokio::test]
    async fn handler_stop_actor_mailbox_closed_returns_500() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let sys = wactorz_core::ActorSystem::default();
        let (entry, rx) = make_test_actor_entry("stop-closed", false);
        let id = entry.id.clone();
        sys.registry.register(entry).await;
        drop(rx); // channel receiver dropped → next send() will fail

        let config = RuntimeConfig { data_dir: dir.path().to_str().unwrap().to_string(), ..Default::default() };
        let resp = RestServer::new(sys, "127.0.0.1:0".parse().unwrap(), config, dir.path().to_str().unwrap().to_string())
            .router()
            .oneshot(Request::builder().method("DELETE").uri(format!("/actors/{id}")).body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::INTERNAL_SERVER_ERROR);
    }

    #[tokio::test]
    async fn handler_pause_actor_mailbox_closed_returns_500() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let sys = wactorz_core::ActorSystem::default();
        let (entry, rx) = make_test_actor_entry("pause-closed", false);
        let id = entry.id.clone();
        sys.registry.register(entry).await;
        drop(rx);

        let config = RuntimeConfig { data_dir: dir.path().to_str().unwrap().to_string(), ..Default::default() };
        let resp = RestServer::new(sys, "127.0.0.1:0".parse().unwrap(), config, dir.path().to_str().unwrap().to_string())
            .router()
            .oneshot(Request::builder().method("POST").uri(format!("/actors/{id}/pause")).body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::INTERNAL_SERVER_ERROR);
    }

    #[tokio::test]
    async fn handler_resume_actor_mailbox_closed_returns_500() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let sys = wactorz_core::ActorSystem::default();
        let (entry, rx) = make_test_actor_entry("resume-closed", false);
        let id = entry.id.clone();
        sys.registry.register(entry).await;
        drop(rx);

        let config = RuntimeConfig { data_dir: dir.path().to_str().unwrap().to_string(), ..Default::default() };
        let resp = RestServer::new(sys, "127.0.0.1:0".parse().unwrap(), config, dir.path().to_str().unwrap().to_string())
            .router()
            .oneshot(Request::builder().method("POST").uri(format!("/actors/{id}/resume")).body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::INTERNAL_SERVER_ERROR);
    }

    #[tokio::test]
    async fn handler_chat_mailbox_closed_returns_500() {
        use tower::ServiceExt;
        use axum::http::Request;
        use axum::body::Body;

        let dir = TempDir::new().unwrap();
        let sys = wactorz_core::ActorSystem::default();
        let (entry, rx) = make_test_actor_entry("main-actor", false);
        sys.registry.register(entry).await;
        drop(rx); // mailbox closed

        let config = RuntimeConfig { data_dir: dir.path().to_str().unwrap().to_string(), ..Default::default() };
        let resp = RestServer::new(sys, "127.0.0.1:0".parse().unwrap(), config, dir.path().to_str().unwrap().to_string())
            .router()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/chat")
                    .header("Content-Type", "application/json")
                    .body(Body::from(r#"{"message": "hello"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::INTERNAL_SERVER_ERROR);
    }
}
