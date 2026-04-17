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
    extract::{Path, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{delete, get, post},
};
use serde::Deserialize;
use std::net::SocketAddr;
use tower_http::cors::CorsLayer;
use tower_http::services::{ServeDir, ServeFile};
use tower_http::trace::TraceLayer;

use wactorz_core::ActorSystem;
use wactorz_core::message::{ActorCommand, Message};

/// Runtime config exposed via /api/config (mirrors Python's config_handler).
#[derive(Clone, Debug, Default)]
pub struct RuntimeConfig {
    pub ha_url: String,
    pub ha_token: String,
    pub fuseki_url: String,
    pub fuseki_dataset: String,
    pub weather_default_location: String,
    pub mqtt_host: String,
    pub mqtt_port: u16,
    pub mqtt_ws_port: u16,
    pub llm_provider: String,
    pub llm_model: String,
}

/// Shared application state injected into axum handlers.
#[derive(Clone)]
pub struct AppState {
    pub system: ActorSystem,
    pub config: RuntimeConfig,
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
}

impl RestServer {
    pub fn new(
        system: ActorSystem,
        addr: SocketAddr,
        config: RuntimeConfig,
        static_dir: String,
    ) -> Self {
        Self {
            state: AppState { system, config },
            addr,
            static_dir,
        }
    }

    /// Build the axum `Router`.
    pub fn router(&self) -> Router {
        let index_html = format!("{}/index.html", self.static_dir);
        let serve_dir =
            ServeDir::new(&self.static_dir).fallback(ServeFile::new(&index_html));

        Router::new()
            .route("/health", get(health_handler))
            // Native paths
            .route("/actors", get(list_actors_handler))
            .route("/actors/:id", get(get_actor_handler))
            .route("/actors/:id/message", post(send_message_handler))
            .route("/actors/:id", delete(stop_actor_handler))
            .route("/actors/:id/pause", post(pause_actor_handler))
            .route("/actors/:id/resume", post(resume_actor_handler))
            .route("/actors/:id/metrics", get(get_metrics_handler))
            .route("/chat", post(chat_handler))
            // /api/* aliases — match paths the Python backend and frontend expect
            .route("/api/config", get(config_handler))
            .route("/api/actors", get(list_actors_handler))
            .route("/api/actors/:id", get(get_actor_handler))
            .route("/api/actors/:id/message", post(send_message_handler))
            .route("/api/actors/:id", delete(stop_actor_handler))
            .route("/api/actors/:id/pause", post(pause_actor_handler))
            .route("/api/actors/:id/resume", post(resume_actor_handler))
            .route("/api/actors/:id/metrics", get(get_metrics_handler))
            .fallback_service(serve_dir)
            .layer(CorsLayer::permissive())
            .layer(TraceLayer::new_for_http())
            .with_state(self.state.clone())
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
