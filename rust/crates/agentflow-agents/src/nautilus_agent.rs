//! NautilusAgent — SSH & rsync file-transfer bridge.
//!
//! Named after the *nautilus*: a deep-sea creature with a spiral protective
//! **shell** — mirroring SSH (**Secure Shell**) — and the Jules Verne submarine
//! that autonomously traverses unreachable depths, just as this agent bridges
//! distant filesystems without human intervention.
//!
//! ## Commands (sent as plain text to the agent's MQTT mailbox)
//!
//! | Command                          | Description                           |
//! |----------------------------------|---------------------------------------|
//! | `ping <user@host>`               | Test SSH connectivity (exit code only)|
//! | `exec <user@host> <cmd [args…]>` | Run a command over SSH                |
//! | `sync <[user@host:]src> <dst>`   | rsync pull from remote                |
//! | `push <src> <[user@host:]dst>`   | rsync push to remote                  |
//! | `help`                           | Print available commands              |
//!
//! Results are published back to `agents/{id}/chat` so they appear in the
//! frontend chat panel.
//!
//! ## Security notes
//!
//! Arguments are **never** passed through a shell — each token is a discrete
//! [`std::process::Command`] argument, preventing shell injection.
//! Host-key verification defaults to `accept-new` so first connections work
//! automatically in container environments; set `NAUTILUS_STRICT_HOST_KEYS=1`
//! to enforce strict checking.

use anyhow::Result;
use async_trait::async_trait;
use std::sync::Arc;
use tokio::process::Command;
use tokio::sync::mpsc;

use agentflow_core::{Actor, ActorConfig, ActorMetrics, ActorState, EventPublisher, Message};

// ── Configuration ─────────────────────────────────────────────────────────────

/// Runtime configuration for [`NautilusAgent`].
#[derive(Debug, Clone)]
pub struct NautilusConfig {
    /// Path to the SSH private key (e.g. `~/.ssh/id_rsa`).
    /// If `None`, SSH uses its default key search order.
    pub ssh_key: Option<String>,

    /// SSH connect timeout in seconds (passed as `-o ConnectTimeout=N`).
    pub connect_timeout_secs: u64,

    /// Wall-clock timeout for any single command execution.
    pub exec_timeout_secs: u64,

    /// Extra `rsync` flags applied to every sync/push (e.g. `["--delete"]`).
    pub rsync_extra_flags: Vec<String>,

    /// When `true`, enforce strict SSH host-key checking.
    /// When `false` (default), new host keys are auto-accepted.
    pub strict_host_keys: bool,
}

impl Default for NautilusConfig {
    fn default() -> Self {
        // Read strict-key preference from env at construction time.
        let strict = std::env::var("NAUTILUS_STRICT_HOST_KEYS")
            .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
            .unwrap_or(false);
        Self {
            ssh_key: std::env::var("NAUTILUS_SSH_KEY").ok(),
            connect_timeout_secs: 10,
            exec_timeout_secs: 120,
            rsync_extra_flags: Vec::new(),
            strict_host_keys: strict,
        }
    }
}

// ── Agent struct ──────────────────────────────────────────────────────────────

/// SSH & rsync file-transfer bridge agent.
pub struct NautilusAgent {
    config: ActorConfig,
    nautilus: NautilusConfig,
    state: ActorState,
    metrics: Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher: Option<EventPublisher>,
}

impl NautilusAgent {
    /// Create a new NautilusAgent with the given actor config.
    pub fn new(config: ActorConfig) -> Self {
        Self::with_nautilus_config(config, NautilusConfig::default())
    }

    /// Create a new NautilusAgent with custom SSH/rsync configuration.
    pub fn with_nautilus_config(config: ActorConfig, nautilus: NautilusConfig) -> Self {
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config,
            nautilus,
            state: ActorState::Initializing,
            metrics: Arc::new(ActorMetrics::new()),
            mailbox_tx: tx,
            mailbox_rx: Some(rx),
            publisher: None,
        }
    }

    /// Attach an [`EventPublisher`] for MQTT output.
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

    /// Send a chat reply back to the frontend.
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

    /// Build the SSH option flags shared by all SSH/rsync invocations.
    fn ssh_opts(&self) -> Vec<String> {
        let mut opts = vec![format!(
            "ConnectTimeout={}",
            self.nautilus.connect_timeout_secs
        )];
        if !self.nautilus.strict_host_keys {
            opts.push("StrictHostKeyChecking=accept-new".to_string());
        }
        opts
    }

    /// Build a base `ssh` [`Command`] ready for additional arguments.
    fn ssh_cmd(&self) -> Command {
        let mut cmd = Command::new("ssh");
        for opt in self.ssh_opts() {
            cmd.args(["-o", &opt]);
        }
        if let Some(key) = &self.nautilus.ssh_key {
            cmd.args(["-i", key]);
        }
        cmd
    }

    // ── Command handlers ──────────────────────────────────────────────────────

    /// `ping <user@host>` — test SSH connectivity.
    async fn cmd_ping(&self, host: &str) {
        if host.is_empty() {
            self.reply("Usage: `ping <user@host>`");
            return;
        }
        self.reply(&format!("Pinging `{host}`…"));

        let result = tokio::time::timeout(
            std::time::Duration::from_secs(self.nautilus.connect_timeout_secs + 2),
            self.ssh_cmd().args([host, "exit"]).output(),
        )
        .await;

        match result {
            Ok(Ok(out)) if out.status.success() => {
                self.reply(&format!("✓ `{host}` is reachable via SSH."));
            }
            Ok(Ok(out)) => {
                let stderr = String::from_utf8_lossy(&out.stderr);
                self.reply(&format!(
                    "✗ SSH to `{host}` failed (exit {}):\n```\n{stderr}\n```",
                    out.status.code().unwrap_or(-1)
                ));
            }
            Ok(Err(e)) => {
                self.reply(&format!("✗ Could not launch ssh: {e}"));
            }
            Err(_) => {
                self.reply(&format!("✗ Connection to `{host}` timed out."));
            }
        }
    }

    /// `exec <user@host> <command [args…]>` — run a remote command.
    async fn cmd_exec(&self, host: &str, remote_args: &[&str]) {
        if host.is_empty() || remote_args.is_empty() {
            self.reply("Usage: `exec <user@host> <command [args…]>`");
            return;
        }

        let display_cmd = remote_args.join(" ");
        self.reply(&format!("Running `{display_cmd}` on `{host}`…"));

        // Each remote token is a discrete argument — no shell interpolation.
        let result = tokio::time::timeout(
            std::time::Duration::from_secs(self.nautilus.exec_timeout_secs),
            self.ssh_cmd().arg(host).args(remote_args).output(),
        )
        .await;

        match result {
            Ok(Ok(out)) => {
                let stdout = String::from_utf8_lossy(&out.stdout);
                let stderr = String::from_utf8_lossy(&out.stderr);
                let code = out.status.code().unwrap_or(-1);
                let status_icon = if out.status.success() { "✓" } else { "✗" };
                let mut response =
                    format!("{status_icon} `{display_cmd}` on `{host}` (exit {code})");
                if !stdout.trim().is_empty() {
                    response.push_str(&format!("\n```\n{}\n```", stdout.trim()));
                }
                if !stderr.trim().is_empty() {
                    response.push_str(&format!("\nstderr:\n```\n{}\n```", stderr.trim()));
                }
                self.reply(&response);
            }
            Ok(Err(e)) => {
                self.reply(&format!("✗ Could not launch ssh: {e}"));
            }
            Err(_) => {
                self.reply(&format!(
                    "✗ Command timed out after {}s.",
                    self.nautilus.exec_timeout_secs
                ));
            }
        }
    }

    /// `sync <[user@host:]src> <dst>` — rsync pull from remote to local.
    async fn cmd_sync(&self, src: &str, dst: &str) {
        if src.is_empty() || dst.is_empty() {
            self.reply("Usage: `sync <[user@host:]src-path> <local-dst-path>`");
            return;
        }
        self.rsync(src, dst, "sync").await;
    }

    /// `push <src> <[user@host:]dst>` — rsync push from local to remote.
    async fn cmd_push(&self, src: &str, dst: &str) {
        if src.is_empty() || dst.is_empty() {
            self.reply("Usage: `push <local-src-path> <[user@host:]dst-path>`");
            return;
        }
        self.rsync(src, dst, "push").await;
    }

    /// Shared rsync executor used by both `sync` and `push`.
    async fn rsync(&self, src: &str, dst: &str, direction: &str) {
        self.reply(&format!("Starting rsync {direction}: `{src}` → `{dst}`…"));

        // Build SSH options string for rsync's -e flag
        let mut ssh_parts = vec!["ssh".to_string()];
        for opt in self.ssh_opts() {
            ssh_parts.push("-o".to_string());
            ssh_parts.push(opt);
        }
        if let Some(key) = &self.nautilus.ssh_key {
            ssh_parts.push("-i".to_string());
            ssh_parts.push(key.clone());
        }
        let ssh_e = ssh_parts.join(" ");

        let mut cmd = Command::new("rsync");
        cmd.args(["-avz", "--progress", "-e", &ssh_e]);
        for flag in &self.nautilus.rsync_extra_flags {
            cmd.arg(flag);
        }
        cmd.args([src, dst]);

        let result = tokio::time::timeout(
            std::time::Duration::from_secs(self.nautilus.exec_timeout_secs),
            cmd.output(),
        )
        .await;

        match result {
            Ok(Ok(out)) => {
                let stdout = String::from_utf8_lossy(&out.stdout);
                let stderr = String::from_utf8_lossy(&out.stderr);
                let code = out.status.code().unwrap_or(-1);
                let icon = if out.status.success() { "✓" } else { "✗" };
                let mut response =
                    format!("{icon} rsync {direction} `{src}` → `{dst}` (exit {code})");
                if !stdout.trim().is_empty() {
                    // Keep last 20 lines of rsync output to avoid flooding chat
                    let lines: Vec<&str> = stdout.trim().lines().collect();
                    let tail = if lines.len() > 20 {
                        let skip = lines.len() - 20;
                        format!("… ({} lines omitted) …\n{}", skip, lines[skip..].join("\n"))
                    } else {
                        lines.join("\n")
                    };
                    response.push_str(&format!("\n```\n{tail}\n```"));
                }
                if !stderr.trim().is_empty() {
                    response.push_str(&format!("\nstderr:\n```\n{}\n```", stderr.trim()));
                }
                self.reply(&response);
            }
            Ok(Err(e)) => {
                self.reply(&format!(
                    "✗ Could not launch rsync: {e}\n(Is rsync installed in the container?)"
                ));
            }
            Err(_) => {
                self.reply(&format!(
                    "✗ rsync timed out after {}s.",
                    self.nautilus.exec_timeout_secs
                ));
            }
        }
    }

    /// Dispatch a parsed text command to the appropriate handler.
    async fn dispatch(&self, text: &str) {
        let tokens: Vec<&str> = text.split_whitespace().collect();
        match tokens.as_slice() {
            [] => {
                self.reply("Empty command. Type `help` for usage.");
            }
            ["help" | "?"] => {
                self.reply(
                    "**NautilusAgent** — SSH & rsync bridge\n\n\
                     | Command | Description |\n\
                     |---------|-------------|\n\
                     | `ping <user@host>` | Test SSH connectivity |\n\
                     | `exec <user@host> <cmd [args…]>` | Run remote command |\n\
                     | `sync <[user@host:]src> <dst>` | rsync pull |\n\
                     | `push <src> <[user@host:]dst>` | rsync push |\n\
                     | `help` | Show this message |",
                );
            }
            ["ping", host] => {
                self.cmd_ping(host).await;
            }
            ["exec", host, rest @ ..] => {
                self.cmd_exec(host, rest).await;
            }
            ["sync", src, dst] => {
                self.cmd_sync(src, dst).await;
            }
            ["push", src, dst] => {
                self.cmd_push(src, dst).await;
            }
            [cmd, ..] => {
                self.reply(&format!("Unknown command: `{cmd}`. Type `help` for usage."));
            }
        }
    }
}

// ── Actor impl ────────────────────────────────────────────────────────────────

#[async_trait]
impl Actor for NautilusAgent {
    fn id(&self) -> String {
        self.config.id.clone()
    }
    fn name(&self) -> &str {
        &self.config.name
    }
    fn state(&self) -> ActorState {
        self.state.clone()
    }
    fn metrics(&self) -> Arc<ActorMetrics> {
        Arc::clone(&self.metrics)
    }
    fn mailbox(&self) -> mpsc::Sender<Message> {
        self.mailbox_tx.clone()
    }
    fn is_protected(&self) -> bool {
        self.config.protected
    }

    async fn on_start(&mut self) -> Result<()> {
        self.state = ActorState::Running;
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                agentflow_mqtt::topics::spawn(&self.config.id),
                &serde_json::json!({
                    "agentId":   self.config.id,
                    "agentName": self.config.name,
                    "agentType": "transfer",
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
        tracing::info!(
            "[nautilus] started — SSH key: {:?}, strict keys: {}",
            self.nautilus.ssh_key,
            self.nautilus.strict_host_keys,
        );
        Ok(())
    }

    async fn handle_message(&mut self, message: Message) -> Result<()> {
        use agentflow_core::message::MessageType;
        let text = match &message.payload {
            MessageType::Text { content } => content.trim().to_string(),
            MessageType::Task { description, .. } => description.trim().to_string(),
            _ => return Ok(()),
        };
        if text.is_empty() {
            return Ok(());
        }
        tracing::debug!("[nautilus] command: {text}");
        self.dispatch(&text).await;
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
            .ok_or_else(|| anyhow::anyhow!("NautilusAgent already running"))?;
        let mut hb = tokio::time::interval(std::time::Duration::from_secs(
            self.config.heartbeat_interval_secs,
        ));
        hb.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            tokio::select! {
                biased;
                msg = rx.recv() => {
                    match msg {
                        None => break,
                        Some(m) => {
                            self.metrics.record_received();
                            if let agentflow_core::message::MessageType::Command {
                                command: agentflow_core::message::ActorCommand::Stop,
                            } = &m.payload
                            {
                                break;
                            }
                            match self.handle_message(m).await {
                                Ok(_) => self.metrics.record_processed(),
                                Err(e) => {
                                    tracing::error!("[nautilus] {e}");
                                    self.metrics.record_failed();
                                }
                            }
                        }
                    }
                }
                _ = hb.tick() => {
                    self.metrics.record_heartbeat();
                    if let Err(e) = self.on_heartbeat().await {
                        tracing::error!("[nautilus] heartbeat: {e}");
                    }
                }
            }
        }
        self.state = ActorState::Stopped;
        self.on_stop().await
    }
}
