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

use wactorz_agents::{
    CatalogAgent, DynamicAgent, FusekiAgent, HomeAssistantAgent, IOAgent, InstallerAgent,
    LlmConfig, LlmProvider, MainActor, ManualAgent, MonitorAgent, WeatherAgent,
};
use wactorz_core::{ActorConfig, ActorSystem, EventPublisher, Supervisor, SupervisorStrategy};
use wactorz_interfaces::ws::WsEnvelope;
use wactorz_interfaces::{RestServer, RuntimeConfig, WsBridge};
use wactorz_mqtt::{MqttClient, MqttConfig};

/// AgentFlow: async multi-agent orchestration framework
#[derive(Debug, Parser)]
#[command(name = "wactorz", version, about)]
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

    /// LLM provider (anthropic | openai | ollama | nim | gemini)
    #[arg(long, default_value = "anthropic", env = "LLM_PROVIDER")]
    pub llm_provider: String,

    /// LLM model name
    #[arg(long, default_value = "claude-sonnet-4-6", env = "LLM_MODEL")]
    pub llm_model: String,

    /// LLM API key
    #[arg(long, env = "LLM_API_KEY")]
    pub llm_api_key: Option<String>,

    /// NVIDIA NIM model (e.g. meta/llama-3.3-70b-instruct).
    /// Implies --llm-provider nim when set.
    #[arg(long, env = "NIM_MODEL")]
    pub nim_model: Option<String>,

    /// MQTT WebSocket port (used by frontend MQTT.js client)
    #[arg(long, default_value_t = 9001, env = "MQTT_WS_PORT")]
    pub mqtt_ws_port: u16,

    /// Home Assistant base URL
    #[arg(long, default_value = "", env = "HA_URL")]
    pub ha_url: String,

    /// Home Assistant long-lived access token
    #[arg(long, default_value = "", env = "HA_TOKEN")]
    pub ha_token: String,

    /// Apache Jena Fuseki URL
    #[arg(long, default_value = "", env = "FUSEKI_URL")]
    pub fuseki_url: String,

    /// Fuseki dataset name
    #[arg(long, default_value = "", env = "FUSEKI_DATASET")]
    pub fuseki_dataset: String,

    /// Default weather location
    #[arg(long, default_value = "", env = "WEATHER_DEFAULT_LOCATION")]
    pub weather_default_location: String,

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
                .unwrap_or_else(|_| "wactorz=info,tower_http=debug".into()),
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
        client_id: "wactorz-server".into(),
        ..Default::default()
    };

    let (mqtt_client, mut event_loop) = MqttClient::new(mqtt_config)?;
    let mqtt_client = Arc::new(mqtt_client);

    // WebSocket broadcast channel
    let (ws_tx, _) = tokio::sync::broadcast::channel::<WsEnvelope>(100);
    let ws_tx_for_mqtt = ws_tx.clone();

    // Registry clone for routing inbound chat messages → actor mailboxes
    let registry_for_route = system.registry.clone();
    let registry_for_qa = system.registry.clone();
    // WIK receives system/llm/error; LlmAgent/MainActor receives system/llm/switch
    let registry_for_wik = system.registry.clone();
    let registry_for_switch = system.registry.clone();

    // Start MQTT event loop task
    tokio::spawn(async move {
        MqttClient::run_event_loop(&mut event_loop, move |evt| {
            if let wactorz_mqtt::MqttEvent::Incoming { topic, payload } = evt {
                tracing::debug!("MQTT in: {topic}");
                if let Ok(json_val) = serde_json::from_slice::<serde_json::Value>(&payload) {
                    // Broadcast to WebSocket clients
                    let envelope = WsEnvelope {
                        topic: topic.clone(),
                        payload: json_val.clone(),
                    };
                    let _ = ws_tx_for_mqtt.send(envelope);

                    // ── system/llm/error → forward to wik-agent ──────────────
                    if topic == wactorz_mqtt::topics::SYSTEM_LLM_ERROR {
                        let reg = registry_for_wik.clone();
                        let payload_str = serde_json::to_string(&json_val).unwrap_or_default();
                        tokio::spawn(async move {
                            if let Some(entry) = reg.get_by_name("wik-agent").await {
                                let msg = wactorz_core::Message::text(
                                    Some("system".to_string()),
                                    Some(entry.id.clone()),
                                    payload_str,
                                );
                                let _ = reg.send(&entry.id, msg).await;
                            }
                        });
                    }

                    // ── system/llm/switch → forward to main-actor as Task ─────
                    if topic == wactorz_mqtt::topics::SYSTEM_LLM_SWITCH {
                        let reg = registry_for_switch.clone();
                        let switch_payload = json_val.clone();
                        tokio::spawn(async move {
                            if let Some(entry) = reg.get_by_name("main-actor").await {
                                let msg = wactorz_core::Message::new(
                                    Some("wik-agent".to_string()),
                                    Some(entry.id.clone()),
                                    wactorz_core::message::MessageType::Task {
                                        task_id: "wik/switch".to_string(),
                                        description: "LLM provider switch".to_string(),
                                        payload: switch_payload,
                                    },
                                );
                                let _ = reg.send(&entry.id, msg).await;
                            }
                        });
                    }

                    // Forward all chat messages to QA agent for passive inspection
                    if topic.ends_with("/chat") {
                        let reg_qa = registry_for_qa.clone();
                        let qa_content = serde_json::to_string(&json_val).unwrap_or_default();
                        tokio::spawn(async move {
                            if let Some(entry) = reg_qa.get_by_name("qa-agent").await {
                                let msg = wactorz_core::Message::text(
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
                            if topic == wactorz_mqtt::topics::IO_CHAT {
                                // io/chat → look up io-agent by name
                                let reg = registry_for_route.clone();
                                tokio::spawn(async move {
                                    if let Some(entry) = reg.get_by_name("io-agent").await {
                                        let msg = wactorz_core::Message::text(
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
                                    let msg = wactorz_core::Message::text(
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
    if let Err(e) = mqtt_client.subscribe(wactorz_mqtt::topics::IO_CHAT).await {
        tracing::warn!("MQTT subscribe io/chat failed: {e}");
    }
    if let Err(e) = mqtt_client.subscribe("system/llm/#").await {
        tracing::warn!("MQTT subscribe system/llm/# failed: {e}");
    }
    if let Err(e) = mqtt_client.subscribe("nodes/#").await {
        tracing::warn!("MQTT subscribe nodes/# failed: {e}");
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
    let (llm_provider, llm_model) = if let Some(nim_model) = &args.nim_model {
        (LlmProvider::Nim, nim_model.clone())
    } else {
        let p = match args.llm_provider.as_str() {
            "openai" => LlmProvider::OpenAI,
            "ollama" => LlmProvider::Ollama,
            "gemini" => LlmProvider::Gemini,
            "nim" => LlmProvider::Nim,
            _ => LlmProvider::Anthropic,
        };
        (p, args.llm_model.clone())
    };
    let llm_config = LlmConfig {
        provider: llm_provider,
        model: llm_model,
        api_key: args.llm_api_key.clone(),
        ..Default::default()
    };

    // ── Supervisor + agents ───────────────────────────────────────────────────.
    // NATO node names:
    //   alpha=main-actor  bravo=monitor  charlie=io
    //   delta=installer   echo=code-agent (DynamicAgent)
    //   foxtrot=manual    golf=home-assistant

    let mut sup = Supervisor::new(system.clone());

    {
        let lc = llm_config.clone();
        let sys = system.clone();
        let pub_ = publisher.clone();
        sup.supervise(
            "main-actor",
            Arc::new(move || {
                let c = ActorConfig::new_with_node("main-actor", "alpha").protected();
                Box::new(MainActor::new(c, lc.clone(), sys.clone()).with_publisher(pub_.clone()))
            }),
            SupervisorStrategy::OneForOne,
            10,
            60.0,
            2.0,
        );
    }
    {
        let sys = system.clone();
        let pub_ = publisher.clone();
        sup.supervise(
            "monitor-agent",
            Arc::new(move || {
                let c = ActorConfig::new_with_node("monitor-agent", "bravo").protected();
                Box::new(MonitorAgent::new(c, sys.clone()).with_publisher(pub_.clone()))
            }),
            SupervisorStrategy::OneForOne,
            10,
            60.0,
            1.0,
        );
    }
    {
        let sys = system.clone();
        let pub_ = publisher.clone();
        sup.supervise(
            "io-agent",
            Arc::new(move || {
                let c = ActorConfig::new_with_node("io-agent", "charlie");
                Box::new(IOAgent::new(c, sys.clone()).with_publisher(pub_.clone()))
            }),
            SupervisorStrategy::OneForOne,
            10,
            60.0,
            1.0,
        );
    }
    {
        let pub_ = publisher.clone();
        sup.supervise(
            "installer-agent",
            Arc::new(move || {
                let c = ActorConfig::new_with_node("installer-agent", "delta");
                Box::new(InstallerAgent::new(c).with_publisher(pub_.clone()))
            }),
            SupervisorStrategy::OneForOne,
            5,
            60.0,
            2.0,
        );
    }
    {
        let pub_ = publisher.clone();
        sup.supervise(
            "code-agent",
            Arc::new(move || {
                let c = ActorConfig::new_with_node("code-agent", "echo");
                Box::new(DynamicAgent::new(c, "").with_publisher(pub_.clone()))
            }),
            SupervisorStrategy::OneForOne,
            5,
            60.0,
            1.0,
        );
    }
    {
        let lc = llm_config.clone();
        let pub_ = publisher.clone();
        sup.supervise(
            "manual-agent",
            Arc::new(move || {
                let c = ActorConfig::new_with_node("manual-agent", "foxtrot");
                Box::new(ManualAgent::new(c, lc.clone()).with_publisher(pub_.clone()))
            }),
            SupervisorStrategy::OneForOne,
            5,
            60.0,
            1.0,
        );
    }
    {
        let pub_ = publisher.clone();
        let ha_url = args.ha_url.clone();
        let ha_token = args.ha_token.clone();
        sup.supervise(
            "home-assistant-agent",
            Arc::new(move || {
                let c = ActorConfig::new_with_node("home-assistant-agent", "golf");
                Box::new(
                    HomeAssistantAgent::new(c)
                        .with_ha_config(ha_url.clone(), ha_token.clone())
                        .with_publisher(pub_.clone()),
                )
            }),
            SupervisorStrategy::OneForOne,
            5,
            60.0,
            2.0,
        );
    }
    {
        let pub_ = publisher.clone();
        let weather_location = args.weather_default_location.clone();
        sup.supervise(
            "weather-agent",
            Arc::new(move || {
                let c = ActorConfig::new_with_node("weather-agent", "hotel");
                Box::new(
                    WeatherAgent::new(c)
                        .with_default_location(weather_location.clone())
                        .with_publisher(pub_.clone()),
                )
            }),
            SupervisorStrategy::OneForOne,
            5,
            60.0,
            1.0,
        );
    }
    {
        let pub_ = publisher.clone();
        let fuseki_url = args.fuseki_url.clone();
        let fuseki_dataset = args.fuseki_dataset.clone();
        sup.supervise(
            "fuseki-agent",
            Arc::new(move || {
                let c = ActorConfig::new_with_node("fuseki-agent", "india");
                Box::new(
                    FusekiAgent::new(c)
                        .with_fuseki_config(fuseki_url.clone(), fuseki_dataset.clone())
                        .with_publisher(pub_.clone()),
                )
            }),
            SupervisorStrategy::OneForOne,
            5,
            60.0,
            2.0,
        );
    }

    {
        let pub_ = publisher.clone();
        sup.supervise(
            "catalog",
            Arc::new(move || {
                let c = ActorConfig::new_with_node("catalog", "juliet").protected();
                Box::new(CatalogAgent::new(c).with_publisher(pub_.clone()))
            }),
            SupervisorStrategy::OneForOne,
            5,
            60.0,
            1.0,
        );
    }

    sup.start().await?;
    info!(
        "Supervisor started — 10 agents (main, monitor, io, installer, code, manual, home-assistant, weather, fuseki, catalog)"
    );

    // ── REST server ───────────────────────────────────────────────────────────
    let rest_addr: SocketAddr = args.api_addr;
    let system_for_rest = system.clone();
    let runtime_cfg = RuntimeConfig {
        ha_url: args.ha_url.clone(),
        ha_token: args.ha_token.clone(),
        fuseki_url: args.fuseki_url.clone(),
        fuseki_dataset: args.fuseki_dataset.clone(),
        weather_default_location: args.weather_default_location.clone(),
        mqtt_host: args.mqtt_host.clone(),
        mqtt_port: args.mqtt_port,
        mqtt_ws_port: args.mqtt_ws_port,
        llm_provider: args.llm_provider.clone(),
        llm_model: args.llm_model.clone(),
    };
    tokio::spawn(async move {
        let server = RestServer::new(system_for_rest, rest_addr, runtime_cfg);
        if let Err(e) = server.serve().await {
            tracing::error!("REST error: {e}");
        }
    });

    // ── WebSocket bridge ──────────────────────────────────────────────────────
    let ws_addr: SocketAddr = args.ws_addr;
    let ws_bridge = WsBridge::new(ws_tx, mqtt_client, args.mqtt_host, args.mqtt_ws_port);
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
        tokio::spawn(wactorz_interfaces::cli::run_cli(system.clone()));
    }

    // ── Wait for shutdown ─────────────────────────────────────────────────────
    tokio::signal::ctrl_c().await?;
    info!("Received Ctrl-C, shutting down…");
    sup.stop().await;
    system.shutdown().await?;
    info!("Goodbye.");
    Ok(())
}
