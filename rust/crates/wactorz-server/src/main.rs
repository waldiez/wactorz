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
use dotenvy::dotenv;
use std::net::SocketAddr;
use std::sync::Arc;
use tracing::info;

use wactorz_agents::{
    CatalogAgent, DynamicAgent, FusekiAgent, HomeAssistantActuatorAgent, HomeAssistantAgent,
    HomeAssistantStateBridgeAgent, IOAgent, InstallerAgent, LlmConfig, LlmProvider, MainActor,
    ManualAgent, MonitorAgent, WeatherAgent,
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

    /// Fuseki username
    #[arg(long, default_value = "", env = "FUSEKI_USER")]
    pub fuseki_user: String,

    /// Fuseki password
    #[arg(long, default_value = "", env = "FUSEKI_PASSWORD")]
    pub fuseki_password: String,

    /// Default weather location
    #[arg(long, default_value = "", env = "WEATHER_DEFAULT_LOCATION")]
    pub weather_default_location: String,

    /// Disable interactive CLI (useful for container deployments)
    #[arg(long, default_value_t = false, env = "NO_CLI")]
    pub no_cli: bool,

    /// Discord bot token
    #[arg(long, default_value = "", env = "DISCORD_TOKEN")]
    pub discord_token: String,

    /// Telegram bot token
    #[arg(long, default_value = "", env = "TELEGRAM_TOKEN")]
    pub telegram_token: String,

    /// Telegram allowed user ID
    #[arg(long, default_value = "", env = "TELEGRAM_ALLOWED_USER_ID")]
    pub telegram_allowed_user_id: String,

    /// Twilio account SID
    #[arg(long, default_value = "", env = "TWILIO_ACCOUNT_SID")]
    pub twilio_account_sid: String,

    /// Twilio auth token
    #[arg(long, default_value = "", env = "TWILIO_AUTH_TOKEN")]
    pub twilio_auth_token: String,

    /// Twilio WhatsApp number (e.g. whatsapp:+14155238886)
    #[arg(long, default_value = "", env = "TWILIO_WHATSAPP_NUMBER")]
    pub twilio_whatsapp_number: String,

    /// REST API key for authentication
    #[arg(long, default_value = "", env = "API_KEY")]
    pub api_key: String,

    /// Path to the built frontend assets directory (served at / and /static/*)
    #[arg(long, default_value = "static/app", env = "STATIC_DIR")]
    pub static_dir: String,

    /// MQTT output topic for the HA state-bridge agent
    #[arg(long, default_value = "ha/state", env = "HA_STATE_BRIDGE_OUTPUT_TOPIC")]
    pub ha_state_bridge_topic: String,

    /// Comma-separated domain allow-list for the HA state-bridge agent (empty = all)
    #[arg(long, default_value = "", env = "HA_STATE_BRIDGE_DOMAINS")]
    pub ha_state_bridge_domains: String,
}

fn normalize_fuseki_endpoint(url: &str, dataset: &str) -> (String, String) {
    let mut base = url.trim().trim_end_matches('/').to_string();
    let mut ds = dataset.trim().trim_matches('/').to_string();

    if base.is_empty() {
        return (base, ds);
    }

    let split_idx = base.find("://").map(|idx| idx + 3).unwrap_or(0);
    let path_start = base[split_idx..].find('/').map(|idx| split_idx + idx);

    if let Some(path_start) = path_start {
        let host = &base[..path_start];
        let path = &base[path_start..];
        let segments: Vec<&str> = path.split('/').filter(|s| !s.is_empty()).collect();
        if let Some(last) = segments.last() {
            if ds.is_empty() {
                ds = (*last).to_string();
                let parent = &segments[..segments.len().saturating_sub(1)];
                base = if parent.is_empty() {
                    host.to_string()
                } else {
                    format!("{}/{}", host, parent.join("/"))
                };
            } else if *last == ds {
                let parent = &segments[..segments.len().saturating_sub(1)];
                base = if parent.is_empty() {
                    host.to_string()
                } else {
                    format!("{}/{}", host, parent.join("/"))
                };
            }
        }
    }

    (base, ds)
}

#[tokio::main]
async fn main() -> Result<()> {
    match dotenv() {
        Ok(path) => info!("Loaded .env from {}", path.display()),
        Err(err) => info!("No .env loaded ({err})"),
    }
    // ── Logging ───────────────────────────────────────────────────────────────
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "wactorz=info,tower_http=debug".into()),
        )
        .init();

    let args = Args::parse();
    info!("Starting AgentFlow server");
    let (fuseki_url, fuseki_dataset) =
        normalize_fuseki_endpoint(&args.fuseki_url, &args.fuseki_dataset);
    if !fuseki_url.is_empty() || !fuseki_dataset.is_empty() {
        info!(
            "Fuseki config normalized to base='{}' dataset='{}'",
            fuseki_url, fuseki_dataset
        );
    }

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
    //   alpha=main-actor  bravo=monitor    charlie=io
    //   delta=installer   echo=code-agent  foxtrot=manual
    //   golf=home-assistant  hotel=weather  india=fuseki  juliet=catalog
    //   kilo=ha-actuator  lima=ha-state-bridge

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
        let fuseki_url = fuseki_url.clone();
        let fuseki_dataset = fuseki_dataset.clone();
        let fuseki_user = args.fuseki_user.clone();
        let fuseki_password = args.fuseki_password.clone();
        sup.supervise(
            "fuseki-agent",
            Arc::new(move || {
                let c = ActorConfig::new_with_node("fuseki-agent", "india");
                Box::new(
                    FusekiAgent::new(c)
                        .with_fuseki_config(fuseki_url.clone(), fuseki_dataset.clone())
                        .with_fuseki_auth(fuseki_user.clone(), fuseki_password.clone())
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

    {
        let pub_ = publisher.clone();
        let ha_url = args.ha_url.clone();
        let ha_token = args.ha_token.clone();
        sup.supervise(
            "ha-actuator",
            Arc::new(move || {
                let c = ActorConfig::new_with_node("ha-actuator", "kilo");
                Box::new(
                    HomeAssistantActuatorAgent::new(c)
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
        let sys = system.clone();
        let ha_url = args.ha_url.clone();
        let ha_token = args.ha_token.clone();
        let fuseki_url = fuseki_url.clone();
        let fuseki_dataset = fuseki_dataset.clone();
        let fuseki_user = args.fuseki_user.clone();
        let fuseki_password = args.fuseki_password.clone();
        let output_topic = args.ha_state_bridge_topic.clone();
        let domains: Vec<String> = args
            .ha_state_bridge_domains
            .split(',')
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect();
        sup.supervise(
            "ha-state-bridge",
            Arc::new(move || {
                let c = ActorConfig::new_with_node("ha-state-bridge", "lima");
                Box::new(
                    HomeAssistantStateBridgeAgent::new(c)
                        .with_system(sys.clone())
                        .with_ha_config(
                            ha_url.clone(),
                            ha_token.clone(),
                            output_topic.clone(),
                            domains.clone(),
                        )
                        .with_fuseki_config(fuseki_url.clone(), fuseki_dataset.clone())
                        .with_fuseki_auth(fuseki_user.clone(), fuseki_password.clone())
                        .with_publisher(pub_.clone()),
                )
            }),
            SupervisorStrategy::OneForOne,
            5,
            60.0,
            2.0,
        );
    }

    sup.start().await?;
    info!(
        "Supervisor started — 12 agents (main, monitor, io, installer, code, manual, home-assistant, weather, fuseki, catalog, ha-actuator, ha-state-bridge)"
    );

    // ── REST server ───────────────────────────────────────────────────────────
    let rest_addr: SocketAddr = args.api_addr;
    let system_for_rest = system.clone();
    let static_dir = args.static_dir.clone();
    let runtime_cfg = RuntimeConfig {
        ha_url: args.ha_url.clone(),
        ha_token: args.ha_token.clone(),
        fuseki_url: fuseki_url.clone(),
        fuseki_dataset: fuseki_dataset.clone(),
        fuseki_user: args.fuseki_user.clone(),
        fuseki_password: args.fuseki_password.clone(),
        weather_default_location: args.weather_default_location.clone(),
        mqtt_host: args.mqtt_host.clone(),
        mqtt_port: args.mqtt_port,
        mqtt_ws_port: args.mqtt_ws_port,
        llm_provider: args.llm_provider.clone(),
        llm_model: args.llm_model.clone(),
    };
    // Merge WsBridge (/ws + /mqtt) onto the same port as REST so the frontend
    // can reach all endpoints via window.location.host — same as Python's
    // single-port layout.
    let ws_bridge = WsBridge::new(
        ws_tx,
        mqtt_client,
        system.clone(),
        args.mqtt_host,
        args.mqtt_ws_port,
    );
    tokio::spawn(async move {
        let server = RestServer::new(system_for_rest, rest_addr, runtime_cfg, static_dir)
            .with_ws(ws_bridge.router());
        if let Err(e) = server.serve().await {
            tracing::error!("REST+WS error: {e}");
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
