//! AgentFlow server entry point.
//!
//! Starts the full system:
//! 1. Parses CLI arguments (via [`clap`])
//! 2. Initialises structured logging (via [`tracing_subscriber`])
//! 3. Creates the [`ActorSystem`]
//! 4. Connects to MQTT broker
//! 5. Spawns [`MainActor`] and [`MonitorAgent`]
//! 6. Starts the REST API + WebSocket bridge
//! 7. Optionally starts the interactive CLI
//! 8. Awaits a Ctrl-C signal then shuts down gracefully

use anyhow::Result;
use clap::Parser;
use std::net::SocketAddr;
use std::sync::Arc;
use tracing::info;

use agentflow_agents::{IOAgent, LlmConfig, LlmProvider, MainActor, MonitorAgent, NautilusAgent, QAAgent, UdxAgent};
use agentflow_core::{ActorConfig, ActorSystem, EventPublisher};
use agentflow_interfaces::{RestServer, WsBridge};
use agentflow_interfaces::ws::WsEnvelope;
use agentflow_mqtt::{MqttClient, MqttConfig};

/// AgentFlow: async multi-agent orchestration framework
#[derive(Debug, Parser)]
#[command(name = "agentflow", version, about)]
pub struct Args {
    /// MQTT broker host
    #[arg(long, default_value = "localhost", env = "MQTT_HOST")]
    pub mqtt_host: String,

    /// MQTT broker port
    #[arg(long, default_value_t = 1883, env = "MQTT_PORT")]
    pub mqtt_port: u16,

    /// REST API listen address
    #[arg(long, default_value = "127.0.0.1:8080", env = "API_ADDR")]
    pub api_addr: SocketAddr,

    /// WebSocket bridge listen address
    #[arg(long, default_value = "127.0.0.1:8081", env = "WS_ADDR")]
    pub ws_addr: SocketAddr,

    /// LLM provider (anthropic | openai | ollama)
    #[arg(long, default_value = "anthropic", env = "LLM_PROVIDER")]
    pub llm_provider: String,

    /// LLM model name
    #[arg(long, default_value = "claude-sonnet-4-6", env = "LLM_MODEL")]
    pub llm_model: String,

    /// LLM API key
    #[arg(long, env = "LLM_API_KEY")]
    pub llm_api_key: Option<String>,

    /// Disable interactive CLI (useful for container deployments)
    #[arg(long, default_value_t = false, env = "NO_CLI")]
    pub no_cli: bool,
}

#[tokio::main]
async fn main() -> Result<()> {
    // ── Logging ───────────────────────────────────────────────────────────────
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "agentflow=info,tower_http=debug".into()),
        )
        .init();

    let args = Args::parse();
    info!("Starting AgentFlow server");

    // ── Publisher channel ─────────────────────────────────────────────────────
    let (publisher, mut pub_rx) = EventPublisher::channel();

    // ── Actor system ──────────────────────────────────────────────────────────
    let system = ActorSystem::with_publisher(publisher.clone());

    // ── MQTT ──────────────────────────────────────────────────────────────────
    let mqtt_config = MqttConfig {
        host: args.mqtt_host.clone(),
        port: args.mqtt_port,
        client_id: "agentflow-server".into(),
        ..Default::default()
    };

    let (mqtt_client, mut event_loop) = MqttClient::new(mqtt_config)?;
    let mqtt_client = Arc::new(mqtt_client);

    // WebSocket broadcast channel
    let (ws_tx, _) = tokio::sync::broadcast::channel::<WsEnvelope>(100);
    let ws_tx_for_mqtt = ws_tx.clone();

    // Registry clone for routing inbound chat messages → actor mailboxes
    let registry_for_route  = system.registry.clone();
    let registry_for_qa     = system.registry.clone();

    // Start MQTT event loop task
    tokio::spawn(async move {
        MqttClient::run_event_loop(&mut event_loop, move |evt| {
            if let agentflow_mqtt::MqttEvent::Incoming { topic, payload } = evt {
                tracing::debug!("MQTT in: {topic}");
                if let Ok(json_val) = serde_json::from_slice::<serde_json::Value>(&payload) {
                    // Broadcast to WebSocket clients
                    let envelope = WsEnvelope {
                        topic: topic.clone(),
                        payload: json_val.clone(),
                    };
                    let _ = ws_tx_for_mqtt.send(envelope);

                    // Forward all chat messages to QA agent for passive inspection
                    if topic.ends_with("/chat") {
                        let reg_qa = registry_for_qa.clone();
                        let qa_content = serde_json::to_string(&json_val).unwrap_or_default();
                        tokio::spawn(async move {
                            if let Some(entry) = reg_qa.get_by_name("qa-agent").await {
                                let msg = agentflow_core::Message::text(
                                    Some("mqtt-router".to_string()),
                                    Some(entry.id.clone()),
                                    qa_content,
                                );
                                let _ = reg_qa.send(&entry.id, msg).await;
                            }
                        });
                    }

                    // Route agents/{id}/chat from user → actor mailbox
                    if topic.ends_with("/chat") {
                        let from = json_val.get("from").and_then(|v| v.as_str()).unwrap_or("");
                        let content = json_val
                            .get("content")
                            .and_then(|v| v.as_str())
                            .unwrap_or("")
                            .to_string();

                        if !content.is_empty() && (from == "user" || from.is_empty()) {
                            if topic == agentflow_mqtt::topics::IO_CHAT {
                                // io/chat → look up io-agent by name
                                let reg = registry_for_route.clone();
                                tokio::spawn(async move {
                                    if let Some(entry) = reg.get_by_name("io-agent").await {
                                        let msg = agentflow_core::Message::text(
                                            Some("user".to_string()),
                                            Some(entry.id.clone()),
                                            content,
                                        );
                                        let _ = reg.send(&entry.id, msg).await;
                                    }
                                });
                            } else if let Some(actor_id) = topic
                                .strip_prefix("agents/")
                                .and_then(|s| s.strip_suffix("/chat"))
                            {
                                // agents/{id}/chat → send directly to that actor
                                let reg = registry_for_route.clone();
                                let id = actor_id.to_string();
                                tokio::spawn(async move {
                                    let msg = agentflow_core::Message::text(
                                        Some("user".to_string()),
                                        Some(id.clone()),
                                        content,
                                    );
                                    let _ = reg.send(&id, msg).await;
                                });
                            }
                        }
                    }
                }
            }
        })
        .await;
    });

    // Subscribe to all agent and system topics, plus the IO gateway topic
    if let Err(e) = mqtt_client.subscribe("agents/#").await {
        tracing::warn!("MQTT subscribe failed (broker may not be running): {e}");
    }
    if let Err(e) = mqtt_client.subscribe("system/#").await {
        tracing::warn!("MQTT subscribe failed (broker may not be running): {e}");
    }
    if let Err(e) = mqtt_client.subscribe(agentflow_mqtt::topics::IO_CHAT).await {
        tracing::warn!("MQTT subscribe io/chat failed: {e}");
    }

    // Publisher bridge task: drain pub_rx → MQTT
    let mqtt_for_bridge = Arc::clone(&mqtt_client);
    tokio::spawn(async move {
        while let Some((topic, payload)) = pub_rx.recv().await {
            if let Err(e) = mqtt_for_bridge.publish_raw(&topic, payload).await {
                tracing::error!("MQTT publish error: {e}");
            }
        }
    });

    // ── LLM config ────────────────────────────────────────────────────────────
    let llm_provider = match args.llm_provider.as_str() {
        "openai" => LlmProvider::OpenAI,
        "ollama" => LlmProvider::Ollama,
        _ => LlmProvider::Anthropic,
    };
    let llm_config = LlmConfig {
        provider: llm_provider,
        model: args.llm_model.clone(),
        api_key: args.llm_api_key.clone(),
        ..Default::default()
    };

    // ── Spawn core agents ─────────────────────────────────────────────────────
    let main_config = ActorConfig::new("main-actor").protected();
    let main_actor = Box::new(
        MainActor::new(main_config, llm_config, system.clone())
            .with_publisher(publisher.clone()),
    );
    system.spawn_actor(main_actor).await?;
    info!("MainActor spawned");

    let monitor_config = ActorConfig::new("monitor-agent").protected();
    let monitor = Box::new(
        MonitorAgent::new(monitor_config, system.clone())
            .with_publisher(publisher.clone()),
    );
    system.spawn_actor(monitor).await?;
    info!("MonitorAgent spawned");

    let io_config = ActorConfig::new("io-agent");
    let io_agent = Box::new(
        IOAgent::new(io_config, system.clone())
            .with_publisher(publisher.clone()),
    );
    system.spawn_actor(io_agent).await?;
    info!("IOAgent spawned");

    let qa_config = ActorConfig::new("qa-agent").protected();
    let qa_agent = Box::new(
        QAAgent::new(qa_config)
            .with_publisher(publisher.clone()),
    );
    system.spawn_actor(qa_agent).await?;
    info!("QAAgent spawned");

    let nautilus_config = ActorConfig::new("nautilus-agent");
    let nautilus_agent = Box::new(
        NautilusAgent::new(nautilus_config)
            .with_publisher(publisher.clone()),
    );
    system.spawn_actor(nautilus_agent).await?;
    info!("NautilusAgent spawned");

    let udx_config = ActorConfig::new("udx-agent");
    let udx_agent = Box::new(
        UdxAgent::new(udx_config, system.clone())
            .with_publisher(publisher.clone()),
    );
    system.spawn_actor(udx_agent).await?;
    info!("UdxAgent spawned");

    // ── REST server ───────────────────────────────────────────────────────────
    let rest_addr: SocketAddr = args.api_addr;
    let system_for_rest = system.clone();
    tokio::spawn(async move {
        let server = RestServer::new(system_for_rest, rest_addr);
        if let Err(e) = server.serve().await {
            tracing::error!("REST error: {e}");
        }
    });

    // ── WebSocket bridge ──────────────────────────────────────────────────────
    let ws_addr: SocketAddr = args.ws_addr;
    let ws_bridge = WsBridge::new(ws_tx);
    tokio::spawn(async move {
        let router = ws_bridge.router();
        match tokio::net::TcpListener::bind(ws_addr).await {
            Ok(listener) => {
                tracing::info!("WS bridge listening on {ws_addr}");
                if let Err(e) = axum::serve(listener, router).await {
                    tracing::error!("WS bridge error: {e}");
                }
            }
            Err(e) => tracing::error!("WS bind error: {e}"),
        }
    });

    // ── CLI (optional) ────────────────────────────────────────────────────────
    if !args.no_cli {
        tokio::spawn(agentflow_interfaces::cli::run_cli(system.clone()));
    }

    // ── Wait for shutdown ─────────────────────────────────────────────────────
    tokio::signal::ctrl_c().await?;
    info!("Received Ctrl-C, shutting down…");
    system.shutdown().await?;
    info!("Goodbye.");
    Ok(())
}
