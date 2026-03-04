//! FusekiAgent — SPARQL knowledge-graph interface (NATO: FERN / Foxtrot).
//!
//! Connects to an Apache Jena Fuseki triple store via the SPARQL 1.1 HTTP
//! protocol. No API key required — uses standard HTTP GET with `?query=` param.
//!
//! ## Commands
//!
//! | Command | Description |
//! |---------|-------------|
//! | `query <sparql>` | Execute a SELECT / CONSTRUCT query |
//! | `ask <sparql>` | Execute an ASK query → true/false |
//! | `prefixes` | List common RDF prefix bindings |
//! | `datasets` | List available Fuseki datasets |
//! | `help` | Show this message |
//!
//! ## Environment
//!
//! - `FUSEKI_URL` — Fuseki base URL (default: `http://fuseki:3030`)
//! - `FUSEKI_DATASET` — dataset path (default: `/ds`)

use anyhow::Result;
use async_trait::async_trait;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc;

use agentflow_core::{
    Actor, ActorConfig, ActorMetrics, ActorState, EventPublisher, Message,
};

const COMMON_PREFIXES: &str = "\
PREFIX rdf:    <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs:   <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl:    <http://www.w3.org/2002/07/owl#>
PREFIX xsd:    <http://www.w3.org/2001/XMLSchema#>
PREFIX dc:     <http://purl.org/dc/elements/1.1/>
PREFIX dcterms:<http://purl.org/dc/terms/>
PREFIX foaf:   <http://xmlns.com/foaf/0.1/>
PREFIX schema: <https://schema.org/>
PREFIX skos:   <http://www.w3.org/2004/02/skos/core#>";

const HELP: &str = "\
**FERN — FusekiAgent** 🌿
_SPARQL knowledge-graph interface_

| Command | Description |
|---------|-------------|
| `query <sparql>` | SELECT / CONSTRUCT / DESCRIBE |
| `ask <sparql>` | ASK query → true or false |
| `prefixes` | Common RDF prefix bindings |
| `datasets` | List Fuseki datasets |
| `help` | This message |

**Example:**
```sparql
query SELECT * WHERE { ?s ?p ?o } LIMIT 5
```";

// ── Agent ─────────────────────────────────────────────────────────────────────

pub struct FusekiAgent {
    config: ActorConfig,
    state: ActorState,
    metrics: Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher: Option<EventPublisher>,
    sparql_endpoint: String,
    admin_endpoint: String,
}

impl FusekiAgent {
    pub fn new(config: ActorConfig) -> Self {
        let base = std::env::var("FUSEKI_URL")
            .unwrap_or_else(|_| "http://fuseki:3030".to_string())
            .trim_end_matches('/')
            .to_string();
        let dataset = std::env::var("FUSEKI_DATASET")
            .unwrap_or_else(|_| "/ds".to_string());
        let dataset = if dataset.starts_with('/') {
            dataset
        } else {
            format!("/{dataset}")
        };

        let sparql_endpoint = format!("{base}{dataset}/sparql");
        let admin_endpoint  = format!("{base}/$/datasets");

        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config,
            state: ActorState::Initializing,
            metrics: Arc::new(ActorMetrics::new()),
            mailbox_tx: tx,
            mailbox_rx: Some(rx),
            publisher: None,
            sparql_endpoint,
            admin_endpoint,
        }
    }

    pub fn with_publisher(mut self, p: EventPublisher) -> Self {
        self.publisher = Some(p);
        self
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    fn now_ms() -> u64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as u64
    }

    fn reply(&self, content: &str) {
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                agentflow_mqtt::topics::chat(&self.config.id),
                &serde_json::json!({
                    "from":        self.config.name,
                    "to":          "user",
                    "content":     content,
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
    }

    fn http_client() -> Result<reqwest::Client, String> {
        reqwest::Client::builder()
            .timeout(Duration::from_secs(20))
            .build()
            .map_err(|e| format!("HTTP client error: {e}"))
    }

    // ── Command handlers ──────────────────────────────────────────────────────

    async fn cmd_query(&self, sparql: &str) {
        if sparql.is_empty() {
            self.reply("Usage: `query <sparql>`\n\nExample: `query SELECT * WHERE { ?s ?p ?o } LIMIT 5`");
            return;
        }
        let client = match Self::http_client() {
            Ok(c) => c,
            Err(e) => { self.reply(&format!("✗ {e}")); return; }
        };

        let result = client
            .get(&self.sparql_endpoint)
            .query(&[("query", sparql), ("format", "json")])
            .header("Accept", "application/sparql-results+json,application/json")
            .send()
            .await;

        match result {
            Err(e) => {
                self.reply(&format!("✗ Query failed: {e}"));
            }
            Ok(resp) if !resp.status().is_success() => {
                let code = resp.status().as_u16();
                let body = resp.text().await.unwrap_or_default();
                self.reply(&format!("✗ Fuseki returned HTTP {code}:\n```\n{}\n```", &body[..body.len().min(400)]));
            }
            Ok(resp) => {
                match resp.json::<serde_json::Value>().await {
                    Err(e) => self.reply(&format!("✗ JSON parse error: {e}")),
                    Ok(data) => self.reply(&Self::format_select_results(&data)),
                }
            }
        }
    }

    async fn cmd_ask(&self, sparql: &str) {
        let sparql = if sparql.trim().to_uppercase().starts_with("ASK") {
            sparql.to_string()
        } else {
            format!("ASK {{ {sparql} }}")
        };
        if sparql.trim().is_empty() {
            self.reply("Usage: `ask <sparql>`\n\nExample: `ask ASK { <http://ex.org/> a owl:Class }`");
            return;
        }
        let client = match Self::http_client() {
            Ok(c) => c,
            Err(e) => { self.reply(&format!("✗ {e}")); return; }
        };

        match client
            .get(&self.sparql_endpoint)
            .query(&[("query", &sparql), ("format", &"json".to_string())])
            .header("Accept", "application/sparql-results+json,application/json")
            .send()
            .await
        {
            Err(e) => self.reply(&format!("✗ ASK failed: {e}")),
            Ok(resp) if !resp.status().is_success() => {
                self.reply(&format!("✗ Fuseki HTTP {}", resp.status().as_u16()));
            }
            Ok(resp) => {
                match resp.json::<serde_json::Value>().await {
                    Err(e) => self.reply(&format!("✗ JSON parse error: {e}")),
                    Ok(data) => {
                        let boolean = data.get("boolean").and_then(|v| v.as_bool()).unwrap_or(false);
                        let icon = if boolean { "✓" } else { "✗" };
                        self.reply(&format!(
                            "**ASK Result:** {icon} `{}`\n\nQuery: `{sparql}`",
                            if boolean { "true" } else { "false" }
                        ));
                    }
                }
            }
        }
    }

    async fn cmd_datasets(&self) {
        let client = match Self::http_client() {
            Ok(c) => c,
            Err(e) => { self.reply(&format!("✗ {e}")); return; }
        };
        match client.get(&self.admin_endpoint).send().await {
            Err(e) => self.reply(&format!("✗ Cannot reach Fuseki: {e}")),
            Ok(resp) if !resp.status().is_success() => {
                self.reply(&format!(
                    "✗ HTTP {} — is the Fuseki admin API enabled?",
                    resp.status().as_u16()
                ));
            }
            Ok(resp) => {
                match resp.json::<serde_json::Value>().await {
                    Err(e) => self.reply(&format!("✗ JSON parse error: {e}")),
                    Ok(data) => {
                        let empty = vec![];
                        let datasets = data.get("datasets")
                            .and_then(|v| v.as_array())
                            .unwrap_or(&empty);
                        if datasets.is_empty() {
                            self.reply("No datasets found.");
                            return;
                        }
                        let mut lines = vec![format!("**Fuseki Datasets ({}):**\n", datasets.len())];
                        for ds in datasets {
                            let name  = ds.get("ds.name").and_then(|v| v.as_str()).unwrap_or("?");
                            let state = ds.get("ds.state").and_then(|v| v.as_str()).unwrap_or("?");
                            lines.push(format!("- `{name}` — {state}"));
                        }
                        self.reply(&lines.join("\n"));
                    }
                }
            }
        }
    }

    fn format_select_results(data: &serde_json::Value) -> String {
        let vars: Vec<&str> = data
            .get("head")
            .and_then(|h| h.get("vars"))
            .and_then(|v| v.as_array())
            .map(|arr| arr.iter().filter_map(|v| v.as_str()).collect())
            .unwrap_or_default();

        let empty_bindings = vec![];
        let bindings = data
            .get("results")
            .and_then(|r| r.get("bindings"))
            .and_then(|b| b.as_array())
            .unwrap_or(&empty_bindings);

        if vars.is_empty() && bindings.is_empty() {
            return "Query returned no results.".to_string();
        }
        if bindings.is_empty() {
            return format!("Query returned 0 rows. Columns: {}", vars.join(", "));
        }

        let mut lines = vec![format!(
            "**Results** ({} rows, columns: {}):\n",
            bindings.len(),
            vars.join(", ")
        )];

        for (i, row) in bindings.iter().take(20).enumerate() {
            let parts: Vec<String> = vars.iter().map(|var| {
                let cell = row.get(*var).cloned().unwrap_or(serde_json::Value::Null);
                let val  = cell.get("value").and_then(|v| v.as_str()).unwrap_or("null");
                let typ  = cell.get("type").and_then(|v| v.as_str()).unwrap_or("");
                if typ == "uri" {
                    // shorten well-known namespaces
                    let shortened = [
                        ("rdf:", "http://www.w3.org/1999/02/22-rdf-syntax-ns#"),
                        ("rdfs:", "http://www.w3.org/2000/01/rdf-schema#"),
                        ("owl:", "http://www.w3.org/2002/07/owl#"),
                        ("xsd:", "http://www.w3.org/2001/XMLSchema#"),
                    ]
                    .iter()
                    .find_map(|(pfx, ns)| val.strip_prefix(ns).map(|s| format!("{pfx}{s}")))
                    .unwrap_or_else(|| val.to_string());
                    format!("`{var}`={shortened:?}")
                } else {
                    format!("`{var}`={val:?}")
                }
            }).collect();
            lines.push(format!("{}. {}", i + 1, parts.join(" | ")));
        }

        if bindings.len() > 20 {
            lines.push(format!("… and {} more rows", bindings.len() - 20));
        }
        lines.join("\n")
    }

    async fn dispatch(&self, text: &str) {
        // Strip agent prefix
        let text = {
            let lower = text.to_lowercase();
            if lower.starts_with("@fern-agent")
                || lower.starts_with("@fern_agent")
                || lower.starts_with("@fuseki-agent")
                || lower.starts_with("@fuseki_agent")
            {
                text.splitn(2, char::is_whitespace).nth(1).unwrap_or("").trim()
            } else {
                text.trim()
            }
        };

        let tokens: Vec<&str> = text.splitn(2, char::is_whitespace).collect();
        match tokens.as_slice() {
            [] | ["" | "help" | "?"] => self.reply(HELP),
            ["prefixes"] => {
                self.reply(&format!("**Common RDF Prefixes:**\n\n```sparql\n{COMMON_PREFIXES}\n```"));
            }
            ["datasets"] => self.cmd_datasets().await,
            ["ask", rest] => self.cmd_ask(rest).await,
            ["query", rest] => self.cmd_query(rest).await,
            [other, ..] => {
                // Try as raw SPARQL
                let up = other.to_uppercase();
                if up == "SELECT" || up == "CONSTRUCT" || up == "DESCRIBE" || up == "PREFIX" || up == "ASK" {
                    self.cmd_query(text).await;
                } else {
                    self.reply(&format!("Unknown command: `{other}`. Type `help`."));
                }
            }
        }
    }
}

// ── Actor impl ────────────────────────────────────────────────────────────────

#[async_trait]
impl Actor for FusekiAgent {
    fn id(&self) -> String { self.config.id.clone() }
    fn name(&self) -> &str { &self.config.name }
    fn state(&self) -> ActorState { self.state.clone() }
    fn metrics(&self) -> Arc<ActorMetrics> { Arc::clone(&self.metrics) }
    fn mailbox(&self) -> mpsc::Sender<Message> { self.mailbox_tx.clone() }
    fn is_protected(&self) -> bool { self.config.protected }

    async fn on_start(&mut self) -> Result<()> {
        self.state = ActorState::Running;
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                agentflow_mqtt::topics::spawn(&self.config.id),
                &serde_json::json!({
                    "agentId":   self.config.id,
                    "agentName": self.config.name,
                    "agentType": "librarian",
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
        tracing::info!("[fern] started — endpoint: {}", self.sparql_endpoint);
        Ok(())
    }

    async fn handle_message(&mut self, message: Message) -> Result<()> {
        use agentflow_core::message::MessageType;
        let text = match &message.payload {
            MessageType::Text { content } => content.trim().to_string(),
            MessageType::Task { description, .. } => description.trim().to_string(),
            _ => return Ok(()),
        };
        if text.is_empty() { return Ok(()); }
        self.dispatch(&text).await;
        self.metrics.record_processed();
        Ok(())
    }

    async fn on_heartbeat(&mut self) -> Result<()> {
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                agentflow_mqtt::topics::heartbeat(&self.config.id),
                &serde_json::json!({
                    "agentId":   self.config.id,
                    "agentName": self.config.name,
                    "state":     self.state,
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
        Ok(())
    }

    async fn run(&mut self) -> Result<()> {
        self.on_start().await?;
        let mut rx = self.mailbox_rx.take()
            .ok_or_else(|| anyhow::anyhow!("FusekiAgent already running"))?;
        let mut hb = tokio::time::interval(Duration::from_secs(self.config.heartbeat_interval_secs));
        hb.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            tokio::select! {
                biased;
                msg = rx.recv() => match msg {
                    None => break,
                    Some(m) => {
                        self.metrics.record_received();
                        if let agentflow_core::message::MessageType::Command {
                            command: agentflow_core::message::ActorCommand::Stop,
                        } = &m.payload { break; }
                        if let Err(e) = self.handle_message(m).await {
                            tracing::error!("[fern] {e}");
                            self.metrics.record_failed();
                        }
                    }
                },
                _ = hb.tick() => {
                    self.metrics.record_heartbeat();
                    if let Err(e) = self.on_heartbeat().await {
                        tracing::error!("[fern] heartbeat: {e}");
                    }
                }
            }
        }
        self.state = ActorState::Stopped;
        self.on_stop().await
    }
}
