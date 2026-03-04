//! # agentflow-agents
//!
//! Concrete agent implementations for AgentFlow.
//!
//! - [`LlmAgent`] — wraps Anthropic / OpenAI / Ollama APIs
//! - [`MainActor`] — LLM orchestrator; parses `<spawn>` directives
//! - [`DynamicAgent`] — executes Rhai scripts generated at runtime
//! - [`MonitorAgent`] — health monitor; raises alerts on stale actors
//! - [`IOAgent`] — UI gateway; routes `io/chat` messages to actors
//! - [`NautilusAgent`] — SSH & rsync file-transfer bridge
//! - [`UdxAgent`] — User and Developer Xpert (built-in knowledge base)
//! - [`MlAgent`] — base class for ML-inference agents
//! - [`QAAgent`] — quality-assurance / safety observer
//! - [`WeatherAgent`] — current weather via wttr.in (no API key)
//! - [`NewsAgent`] — headlines via Hacker News API (no API key)
//! - [`WifAgent`] — finance expert: expense tracking, budgets, calculations
//! - [`WizAgent`] — WaldiezCoin economist: in-game economy tracking
//! - [`HomeAssistantAgent`] — Home Assistant device discovery & entity queries
//! - [`FusekiAgent`] — SPARQL knowledge-graph interface (NATO: FERN / Foxtrot)
//! - [`TickAgent`] — in-process scheduler/timer (NATO: CHRON / Tango)

pub mod dynamic_agent;
pub mod fuseki_agent;
pub mod home_assistant_agent;
pub mod io_agent;
pub mod llm_agent;
pub mod main_actor;
pub mod ml_agent;
pub mod monitor_agent;
pub mod nautilus_agent;
pub mod news_agent;
pub mod qa_agent;
pub mod tick_agent;
pub mod udx_agent;
pub mod weather_agent;
pub mod wif_agent;
pub mod wiz_agent;

pub use dynamic_agent::DynamicAgent;
pub use fuseki_agent::FusekiAgent;
pub use home_assistant_agent::HomeAssistantAgent;
pub use io_agent::IOAgent;
pub use llm_agent::{LlmAgent, LlmConfig, LlmProvider};
pub use main_actor::MainActor;
pub use ml_agent::MlAgent;
pub use monitor_agent::MonitorAgent;
pub use nautilus_agent::{NautilusAgent, NautilusConfig};
pub use news_agent::NewsAgent;
pub use qa_agent::QAAgent;
pub use tick_agent::TickAgent;
pub use udx_agent::UdxAgent;
pub use weather_agent::WeatherAgent;
pub use wif_agent::WifAgent;
pub use wiz_agent::WizAgent;
