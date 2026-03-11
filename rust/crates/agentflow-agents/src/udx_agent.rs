//! UDX — User and Developer Xpert.
//!
//! An always-available, zero-LLM knowledge agent that answers questions about
//! the AgentFlow system.  It holds a built-in knowledge base covering
//! architecture, agent roles, MQTT topics, the REST API, and deployment
//! options.  Because it needs no external API call it responds instantly and
//! keeps working even when the LLM key is absent.
//!
//! ## Commands
//!
//! | Command                | Description                                      |
//! |------------------------|--------------------------------------------------|
//! | `help`                 | Overview of all available commands               |
//! | `help <topic>`         | Topic-specific help (see topics below)           |
//! | `docs <topic>`         | In-depth documentation for a topic               |
//! | `explain <concept>`    | Explain a concept (actor-model, mqtt, wid, …)    |
//! | `agents`               | List all currently registered agents             |
//! | `status`               | Live system summary (agent count, states)        |
//! | `version`              | Build / runtime info                             |
//!
//! ### Topics / concepts recognised
//!
//! `architecture`, `agents`, `chat`, `dashboard`, `api`, `mqtt`, `deploy`,
//! `actor-model`, `hlc-wid`, `wid`, `rust`, `babylon`, `nautilus`, `io`,
//! `qa`, `monitor`, `dynamic`, `main`, `udx`
//!
//! Anything not recognised returns a friendly suggestion to ask `@main-actor`.

use anyhow::Result;
use async_trait::async_trait;
use std::sync::Arc;
use tokio::sync::mpsc;

use agentflow_core::{
    Actor, ActorConfig, ActorMetrics, ActorState, ActorSystem, EventPublisher, Message,
};

// ── UdxAgent ──────────────────────────────────────────────────────────────────

/// User and Developer Xpert — built-in knowledge agent.
pub struct UdxAgent {
    config:      ActorConfig,
    system:      ActorSystem,
    state:       ActorState,
    metrics:     Arc<ActorMetrics>,
    mailbox_tx:  mpsc::Sender<Message>,
    mailbox_rx:  Option<mpsc::Receiver<Message>>,
    publisher:   Option<EventPublisher>,
}

impl UdxAgent {
    pub fn new(config: ActorConfig, system: ActorSystem) -> Self {
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config,
            system,
            state:      ActorState::Initializing,
            metrics:    Arc::new(ActorMetrics::new()),
            mailbox_tx: tx,
            mailbox_rx: Some(rx),
            publisher:  None,
        }
    }

    pub fn with_publisher(mut self, p: EventPublisher) -> Self {
        self.publisher = Some(p);
        self
    }

    fn now_ms() -> u64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as u64
    }

    /// Publish a chat reply back to the frontend.
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

    /// Main dispatch: parse command + args, build response.
    async fn dispatch(&self, raw: &str) -> String {
        let text  = raw.trim();
        let lower = text.to_lowercase();
        let mut parts = lower.splitn(2, char::is_whitespace);
        let cmd  = parts.next().unwrap_or("");
        let rest = parts.next().unwrap_or("").trim();

        match cmd {
            "help"    => self.cmd_help(rest),
            "docs"    => Self::cmd_docs(rest),
            "explain" => Self::cmd_explain(rest),
            "version" => Self::cmd_version(),
            "agents"  => self.cmd_agents().await,
            "status"  => self.cmd_status().await,
            _         => Self::fallback(text),
        }
    }

    // ── Commands ──────────────────────────────────────────────────────────────

    fn cmd_help(&self, topic: &str) -> String {
        if topic.is_empty() {
            return format!(
                "**UDX — User and Developer Xpert** · `{}`\n\n\
                 **Commands**\n\
                 `help [topic]`     → this message, or topic-specific help\n\
                 `docs <topic>`     → in-depth docs on a topic\n\
                 `explain <thing>`  → explain a concept\n\
                 `agents`           → list all live agents\n\
                 `status`           → system health snapshot\n\
                 `version`          → build info\n\n\
                 **Topics**: `architecture` · `agents` · `chat` · `dashboard` · `api` · `mqtt` · `deploy`\n\
                 **Concepts**: `actor-model` · `hlc-wid` · `rust` · `babylon` · agent names\n\n\
                 For open questions try `@main-actor <your question>`.",
                self.config.id
            );
        }
        // topic-specific quick help
        match topic {
            "chat" => Self::doc_chat(),
            "dashboard" => Self::doc_dashboard(),
            "api" => Self::doc_api(),
            "mqtt" => Self::doc_mqtt(),
            "deploy" => Self::doc_deploy(),
            "architecture" | "arch" => Self::doc_architecture(),
            "agents" => Self::doc_agents(),
            _ => Self::cmd_explain(topic),
        }
    }

    fn cmd_docs(topic: &str) -> String {
        match topic {
            "architecture" | "arch" => Self::doc_architecture(),
            "agents"                => Self::doc_agents(),
            "chat"                  => Self::doc_chat(),
            "dashboard"             => Self::doc_dashboard(),
            "api"                   => Self::doc_api(),
            "mqtt"                  => Self::doc_mqtt(),
            "deploy"                => Self::doc_deploy(),
            _ => format!(
                "Unknown topic **{topic}**.\n\
                 Available: `architecture` · `agents` · `chat` · `dashboard` · `api` · `mqtt` · `deploy`"
            ),
        }
    }

    fn cmd_explain(concept: &str) -> String {
        match concept {
            "actor-model" | "actor" | "actors" => "\
                **Actor Model**\n\
                Each agent is an independent *actor* with its own message inbox \
                (a bounded async channel).  Actors never share mutable state — they \
                communicate exclusively by passing immutable messages.  This eliminates \
                data races and makes the system trivially scalable: add more actors, \
                no locks needed.\n\
                AgentFlow actors each run on their own `tokio::spawn` task and expose \
                three lifecycle hooks: `on_start`, `handle_message`, `on_heartbeat`."
                .to_string(),

            "mqtt" => "\
                **MQTT pub/sub**\n\
                All inter-service communication in AgentFlow uses MQTT (Mosquitto broker).\n\
                Topic structure:\n\
                `agents/{id}/spawn`      → agent came online\n\
                `agents/{id}/heartbeat`  → liveness tick (every 10 s)\n\
                `agents/{id}/status`     → state change\n\
                `agents/{id}/alert`      → warning or error\n\
                `agents/{id}/chat`       → message to/from an agent\n\
                `system/health`          → MonitorAgent health digest\n\
                `io/chat`                → frontend → IOAgent gateway\n\n\
                The WebSocket bridge re-broadcasts every MQTT message to the browser \
                so the Babylon.js frontend stays in sync without polling."
                .to_string(),

            "hlc-wid" | "wid" | "hlc" => "\
                **HLC-WID (Hybrid Logical Clock Wide ID)**\n\
                All actor IDs use HLC-WIDs — time-ordered, globally unique identifiers \
                generated by the `waldiez-wid` crate.\n\
                Format: `<hlc-timestamp>-<node-tag>` (e.g. `01JQND5X-a1b2c3d4`)\n\
                Properties:\n\
                • Monotonically increasing (even across clock skew)\n\
                • Embeds wall-clock time for human readability\n\
                • Collision-free across distributed nodes\n\
                Message IDs use plain WIDs (simpler, no node tag needed)."
                .to_string(),

            "rust" => "\
                **Rust backend**\n\
                The server is a single `agentflow` binary built with Tokio async runtime.\n\
                Workspace crates:\n\
                `agentflow-core`       → Actor trait, registry, message types, publisher\n\
                `agentflow-agents`     → all concrete agent implementations\n\
                `agentflow-mqtt`       → MQTT client + topic helpers\n\
                `agentflow-interfaces` → REST (axum) + WebSocket bridge + CLI\n\
                `agentflow-server`     → binary entry point, wires everything together\n\n\
                Build: `cargo build --release --bin agentflow`\n\
                Cross-compile for Linux: use `docker buildx` (see `scripts/build-native.sh`)."
                .to_string(),

            "babylon" | "babylonjs" => "\
                **Babylon.js frontend**\n\
                The dashboard is a Vite + TypeScript SPA using Babylon.js 7.x for 3D rendering.\n\
                Themes (switchable at runtime):\n\
                `Graph`   → spring-force layout, glowing spheres, Bezier chat arcs\n\
                `Galaxy`  → orbiting planets with moons for sub-agents\n\
                `Cards`   → classic card grid (pure HTML, no WebGL)\n\
                `Social`  → Instagram×Twitter hybrid profile cards\n\
                `Graveyard` → stopped agents shown as tombstones\n\n\
                All themes share the same MQTT event stream; switching is instantaneous."
                .to_string(),

            "nautilus" | "nautilus-agent" => "\
                **NautilusAgent** — SSH & rsync file-transfer bridge\n\
                Named after the nautilus shell (SSH = *Secure Shell*) and Jules Verne's submarine.\n\
                Commands: `ping`, `exec`, `sync`, `push`, `help`\n\
                Example:\n\
                `@nautilus-agent ping user@host`\n\
                `@nautilus-agent exec user@host df -h`\n\
                `@nautilus-agent sync user@host:/var/data /mnt/local`\n\
                `@nautilus-agent push ./dist/ user@host:/var/www/html/`\n\n\
                Arguments are never shell-interpolated — each token is a discrete \
                `Command::arg()`, preventing injection attacks.\n\
                Configure via env: `NAUTILUS_SSH_KEY`, `NAUTILUS_STRICT_HOST_KEYS`."
                .to_string(),

            "io" | "io-agent" | "ioagent" => "\
                **IOAgent** — UI gateway\n\
                Bridges the frontend chat bar to the actor system.\n\
                Listens on the fixed MQTT topic `io/chat`.\n\
                Route with `@agent-name` prefix: `@monitor-agent status`\n\
                No prefix → message goes to `main-actor`.\n\
                The agent parses only the first `@name` token; everything after \
                is the message body."
                .to_string(),

            "qa" | "qa-agent" => "\
                **QAAgent** — quality-assurance observer\n\
                Passively inspects every chat message that flows through the broker.\n\
                Flags messages containing harmful patterns (prompt injection, PII leakage, \
                profanity) and publishes a `system/alert` if a policy is violated.\n\
                Protected — cannot be stopped or deleted via the dashboard."
                .to_string(),

            "monitor" | "monitor-agent" => "\
                **MonitorAgent** — health watchdog\n\
                Polls all registered actors every 15 seconds.\n\
                Raises a `severity: error` alert if an actor's last heartbeat is \
                older than 60 seconds.\n\
                Publishes a `system/health` digest on every heartbeat tick.\n\
                Protected — cannot be stopped or deleted via the dashboard."
                .to_string(),

            "dynamic" | "dynamic-agent" => "\
                **DynamicAgent** — runtime script executor\n\
                Spawned on-demand by `MainActor` when it parses a `<spawn>` directive \
                from the LLM response.  Executes Rhai scripts generated at runtime, \
                enabling the LLM to extend the system with new capabilities without \
                a server restart."
                .to_string(),

            "main" | "main-actor" => "\
                **MainActor** — LLM orchestrator\n\
                The central intelligence of AgentFlow.  Receives user messages, \
                calls the configured LLM (Anthropic / OpenAI / Ollama), and parses \
                `<spawn agent-type=\"…\">…</spawn>` blocks in the response to \
                dynamically create new agents.\n\
                Protected — cannot be stopped or deleted via the dashboard."
                .to_string(),

            "udx" | "udx-agent" => "\
                **UDXAgent** — User and Developer Xpert (that's me!)\n\
                A zero-LLM, always-available knowledge agent.\n\
                I answer questions about AgentFlow instantly from a built-in knowledge \
                base — no API key needed, no network round-trip.\n\
                For questions outside my knowledge, ask `@main-actor`."
                .to_string(),

            _ => format!(
                "I don't have built-in docs for **{concept}**.\n\
                 Try `explain actor-model`, `explain mqtt`, `explain rust`, `explain babylon`, \
                 or ask `@main-actor {concept}` for an LLM-powered answer."
            ),
        }
    }

    fn cmd_version() -> String {
        format!(
            "**AgentFlow** · Rust {rust} · built {built}\n\
             Backend crates: `agentflow-core` · `agentflow-agents` · `agentflow-mqtt` · \
             `agentflow-interfaces` · `agentflow-server`\n\
             Frontend: Vite + TypeScript + Babylon.js 7.x\n\
             Default LLM: `claude-sonnet-4-6` (Anthropic)",
            rust  = env!("CARGO_PKG_RUST_VERSION", "unknown"),
            built = env!("CARGO_PKG_VERSION"),
        )
    }

    async fn cmd_agents(&self) -> String {
        let entries = self.system.registry.list().await;
        if entries.is_empty() {
            return "No agents currently registered.".to_string();
        }
        let mut lines = vec![format!("**Live agents** ({})", entries.len())];
        for e in &entries {
            let state = format!("{:?}", e.state).to_lowercase();
            let prot  = if e.protected { " ⭐" } else { "" };
            lines.push(format!("• `{}` — {} [{}]{}", e.name, e.id, state, prot));
        }
        lines.join("\n")
    }

    async fn cmd_status(&self) -> String {
        let entries = self.system.registry.list().await;
        let total    = entries.len();
        let running  = entries.iter().filter(|e| format!("{:?}", e.state).to_lowercase() == "running").count();
        let stopped  = entries.iter().filter(|e| format!("{:?}", e.state).to_lowercase() == "stopped").count();
        let paused   = entries.iter().filter(|e| format!("{:?}", e.state).to_lowercase() == "paused").count();
        let other    = total - running - stopped - paused;
        format!(
            "**System status**\n\
             Total agents : {total}\n\
             Running      : {running}\n\
             Paused       : {paused}\n\
             Stopped      : {stopped}\n\
             Other        : {other}\n\n\
             Use `agents` for the full list, or ask `@monitor-agent` for health alerts."
        )
    }

    fn fallback(text: &str) -> String {
        format!(
            "I don't recognise **{text}** as a UDX command.\n\
             Type `help` for a full command list, or try:\n\
             • `explain <concept>` — e.g. `explain mqtt`\n\
             • `docs <topic>`      — e.g. `docs api`\n\
             • `agents` / `status` / `version`\n\n\
             For open-ended questions: `@main-actor {text}`"
        )
    }

    // ── Static doc pages ──────────────────────────────────────────────────────

    fn doc_architecture() -> String {
        "\
        **AgentFlow Architecture**\n\
        \n\
        ```\n\
        Browser (Babylon.js SPA)\n\
           │  WebSocket (MQTT re-broadcast)\n\
           │  REST  /api/\n\
           ▼\n\
        nginx  ──── /mqtt  ──►  Mosquitto (MQTT broker)\n\
                                    ▲          │\n\
                              MQTT pub/sub      │\n\
                                    │          ▼\n\
                              agentflow-server (Rust binary)\n\
                                ├── MainActor   (LLM orchestrator)\n\
                                ├── MonitorAgent (health watchdog)\n\
                                ├── IOAgent      (UI gateway)\n\
                                ├── QAAgent      (safety observer)\n\
                                ├── NautilusAgent (SSH/rsync)\n\
                                ├── UDXAgent     (knowledge base)\n\
                                └── DynamicAgent (LLM-generated scripts)\n\
        ```\n\
        \n\
        Every agent is an async actor; all communication is via MQTT topics.\n\
        Use `explain actor-model` or `explain mqtt` for deeper dives.".to_string()
    }

    fn doc_agents() -> String {
        "\
        **Agent Roster**\n\n\
        | Agent            | Type         | Protected | Role                          |\n\
        |------------------|--------------|-----------|-------------------------------|\n\
        | main-actor       | orchestrator | ⭐ yes    | LLM brain, spawns sub-agents  |\n\
        | monitor-agent    | monitor      | ⭐ yes    | Health watchdog, alerts       |\n\
        | io-agent         | gateway      | no        | UI↔actor message bridge       |\n\
        | qa-agent         | qa           | ⭐ yes    | Passive safety observer       |\n\
        | nautilus-agent   | transfer     | no        | SSH & rsync file bridge       |\n\
        | udx-agent        | expert       | no        | Built-in knowledge base       |\n\
        | dynamic-*        | dynamic      | no        | LLM-generated script agents   |\n\n\
        Use `explain <agent-name>` for details on any agent.".to_string()
    }

    fn doc_chat() -> String {
        "\
        **Chat Panel**\n\n\
        • Click any agent card / node → opens the chat panel for that agent\n\
        • Type in the IO bar (bottom) to send a message\n\
        • Prefix with `@agent-name` to route to a specific agent:\n\
          `@nautilus-agent ping user@host`\n\
        • Shift+Enter → newline (Enter alone sends)\n\
        • Arrow Up/Down → message history\n\
        • Swipe right (mobile) or press Escape → close the panel\n\
        • 3-dot indicator shows when an agent is processing your message".to_string()
    }

    fn doc_dashboard() -> String {
        "\
        **Dashboard Views**\n\n\
        Switch with the buttons top-right:\n\
        `3D Graph`   → spring-force layout; chat arcs animate between nodes\n\
        `Galaxy`     → orbital view; main-actor at centre, others orbit\n\
        `Cards`      → compact HTML card grid; fastest on low-end devices\n\
        `Social`     → Instagram-style profile cards with stats\n\
        `Graveyard`  → shows stopped/failed agents as tombstones\n\n\
        Controls per card: 💬 Chat · ⏸ Pause · ▶ Resume · ⏹ Stop · 🗑 Delete\n\
        Protected agents (⭐) cannot be stopped or deleted.".to_string()
    }

    fn doc_api() -> String {
        "\
        **REST API**  (`/api/`)\n\n\
        `GET  /api/actors`                → list all actors\n\
        `GET  /api/actors/:id`            → get actor info\n\
        `POST /api/actors/:id/pause`      → pause actor\n\
        `POST /api/actors/:id/resume`     → resume actor\n\
        `DELETE /api/actors/:id`          → stop + remove actor\n\
        `POST /api/chat`                  → send a message (body: `{\"to\":\"…\",\"content\":\"…\"}`)\n\n\
        WebSocket bridge: `ws://host/ws`\n\
        Subscribes once; receives every MQTT message as `{\"topic\":\"…\",\"payload\":{…}}`.".to_string()
    }

    fn doc_mqtt() -> String {
        "\
        **MQTT Topic Reference**\n\n\
        `agents/{id}/spawn`      → `{agentId, agentName, agentType, timestampMs}`\n\
        `agents/{id}/heartbeat`  → `{agentId, agentName, state, timestampMs}`\n\
        `agents/{id}/status`     → `{agentId, state, timestampMs}`\n\
        `agents/{id}/alert`      → `{agentId, severity, message, timestampMs}`\n\
        `agents/{id}/chat`       → `{from, to, content, timestampMs}`\n\
        `system/health`          → `{agentCount, staleAgents[], timestampMs}`\n\
        `system/spawn`           → dynamic-agent spawn announcements\n\
        `io/chat`                → frontend → IOAgent gateway\n\n\
        Broker: Mosquitto TCP 1883 (internal) / WS 9001 via nginx `/mqtt`\n\
        All payloads are camelCase JSON.".to_string()
    }

    fn doc_deploy() -> String {
        "\
        **Deployment Options**\n\n\
        **Full Docker** (`compose.yaml`):\n\
        `docker compose up -d`  — runs everything in containers\n\n\
        **Native binary** (`compose.native.yaml`):\n\
        `bash scripts/package-native.sh`   — builds `agentflow-native-*.tar.gz`\n\
        `bash deploy-native.sh`            — wizard: starts Mosquitto+nginx in Docker,\n\
                                             runs the binary directly on the host\n\
        Benefits: SSH keys work automatically (NautilusAgent), faster startup,\n\
        smaller footprint (~12 MB binary vs 39 MB image).\n\n\
        **systemd** (persistent on reboot):\n\
        `sudo cp systemd/agentflow.service /etc/systemd/system/`\n\
        `sudo systemctl enable --now agentflow`\n\
        `journalctl -u agentflow -f`".to_string()
    }
}

// ── Actor impl ────────────────────────────────────────────────────────────────

#[async_trait]
impl Actor for UdxAgent {
    fn id(&self)      -> String          { self.config.id.clone() }
    fn name(&self)    -> &str            { &self.config.name }
    fn state(&self)   -> ActorState      { self.state.clone() }
    fn metrics(&self) -> Arc<ActorMetrics> { Arc::clone(&self.metrics) }
    fn mailbox(&self) -> mpsc::Sender<Message> { self.mailbox_tx.clone() }
    fn is_protected(&self) -> bool       { self.config.protected }

    async fn on_start(&mut self) -> Result<()> {
        self.state = ActorState::Running;
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                agentflow_mqtt::topics::spawn(&self.config.id),
                &serde_json::json!({
                    "agentId":   self.config.id,
                    "agentName": self.config.name,
                    "agentType": "expert",
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
        Ok(())
    }

    async fn handle_message(&mut self, message: Message) -> Result<()> {
        use agentflow_core::message::MessageType;
        let content = match &message.payload {
            MessageType::Text { content }        => content.clone(),
            MessageType::Task { description, .. } => description.clone(),
            _ => return Ok(()),
        };
        let response = self.dispatch(content.trim()).await;
        self.reply(&response);
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
        let mut rx = self
            .mailbox_rx
            .take()
            .ok_or_else(|| anyhow::anyhow!("UdxAgent already running"))?;
        let mut hb = tokio::time::interval(std::time::Duration::from_secs(
            self.config.heartbeat_interval_secs,
        ));
        hb.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            tokio::select! {
                biased;
                msg = rx.recv() => {
                    match msg {
                        None    => break,
                        Some(m) => {
                            self.metrics.record_received();
                            if let agentflow_core::message::MessageType::Command {
                                command: agentflow_core::message::ActorCommand::Stop
                            } = &m.payload {
                                break;
                            }
                            match self.handle_message(m).await {
                                Ok(_)  => self.metrics.record_processed(),
                                Err(e) => {
                                    tracing::error!("[{}] {e}", self.config.name);
                                    self.metrics.record_failed();
                                }
                            }
                        }
                    }
                }
                _ = hb.tick() => {
                    self.metrics.record_heartbeat();
                    if let Err(e) = self.on_heartbeat().await {
                        tracing::error!("[{}] heartbeat: {e}", self.config.name);
                    }
                }
            }
        }
        self.state = ActorState::Stopped;
        self.on_stop().await
    }
}
