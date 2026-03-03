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
//! - [`MlAgent`] — base class for ML-inference agents
//! - [`QAAgent`] — quality-assurance / safety observer

pub mod dynamic_agent;
pub mod io_agent;
pub mod llm_agent;
pub mod main_actor;
pub mod ml_agent;
pub mod monitor_agent;
pub mod nautilus_agent;
pub mod qa_agent;

pub use dynamic_agent::DynamicAgent;
pub use io_agent::IOAgent;
pub use llm_agent::{LlmAgent, LlmConfig, LlmProvider};
pub use main_actor::MainActor;
pub use ml_agent::MlAgent;
pub use monitor_agent::MonitorAgent;
pub use nautilus_agent::{NautilusAgent, NautilusConfig};
pub use qa_agent::QAAgent;
