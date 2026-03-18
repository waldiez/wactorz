//! Package installer agent.
//!
//! [`InstallerAgent`] runs `pip install` (or `pip3 install`) as a subprocess
//! and streams progress to MQTT.  Used by MainActor to install Python
//! dependencies before spawning new dynamic agents.

use anyhow::Result;
use async_trait::async_trait;
use std::sync::Arc;
use tokio::sync::mpsc;

use wactorz_core::{Actor, ActorConfig, ActorMetrics, ActorState, EventPublisher, Message};

pub struct InstallerAgent {
    config:     ActorConfig,
    state:      ActorState,
    metrics:    Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher:  Option<EventPublisher>,
}

impl InstallerAgent {
    pub fn new(config: ActorConfig) -> Self {
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config,
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

    /// Run `pip install <packages>` and return (success, output).
    async fn pip_install(&self, packages: &[String]) -> (bool, String) {
        if packages.is_empty() {
            return (true, "No packages to install.".into());
        }

        // Try pip3 first, fall back to pip
        let pip = if which_pip("pip3") { "pip3" } else { "pip" };
        let mut args = vec!["install", "--quiet"];
        let pkg_refs: Vec<&str> = packages.iter().map(|s| s.as_str()).collect();
        args.extend_from_slice(&pkg_refs);

        tracing::info!("[{}] Running: {} install {}", self.config.name, pip, packages.join(" "));

        match tokio::process::Command::new(pip)
            .args(&args)
            .output()
            .await
        {
            Ok(out) => {
                let _stdout = String::from_utf8_lossy(&out.stdout).to_string();
                let stderr = String::from_utf8_lossy(&out.stderr).to_string();
                let success = out.status.success();
                let output = if success {
                    format!("Installed: {}", packages.join(", "))
                } else {
                    format!("pip error:\n{stderr}")
                };
                (success, output)
            }
            Err(e) => (false, format!("Failed to run pip: {e}")),
        }
    }

    fn now_ms() -> u64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as u64
    }
}

fn which_pip(cmd: &str) -> bool {
    std::process::Command::new("which")
        .arg(cmd)
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

#[async_trait]
impl Actor for InstallerAgent {
    fn id(&self)      -> String { self.config.id.clone() }
    fn name(&self)    -> &str   { &self.config.name }
    fn state(&self)   -> ActorState { self.state.clone() }
    fn metrics(&self) -> Arc<ActorMetrics> { Arc::clone(&self.metrics) }
    fn mailbox(&self) -> mpsc::Sender<Message> { self.mailbox_tx.clone() }

    async fn on_start(&mut self) -> Result<()> {
        self.state = ActorState::Running;
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                wactorz_mqtt::topics::spawn(&self.config.id),
                &serde_json::json!({
                    "agentId":   self.config.id,
                    "agentName": self.config.name,
                    "agentType": "installer",
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
        Ok(())
    }

    async fn handle_message(&mut self, message: Message) -> Result<()> {
        use wactorz_core::message::MessageType;

        let packages: Vec<String> = match &message.payload {
            MessageType::Text { content } => {
                // Parse space- or comma-separated package names
                content.split([' ', ','])
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty())
                    .collect()
            }
            MessageType::Task { payload, .. } => {
                // Expect {"packages": ["pkg1", "pkg2"]}
                payload.get("packages")
                    .and_then(|v| v.as_array())
                    .map(|arr| arr.iter()
                        .filter_map(|v| v.as_str().map(|s| s.to_string()))
                        .collect())
                    .unwrap_or_default()
            }
            _ => return Ok(()),
        };

        let (success, output) = self.pip_install(&packages).await;

        if let Some(pub_) = &self.publisher {
            pub_.publish(
                wactorz_mqtt::topics::chat(&self.config.id),
                &serde_json::json!({
                    "from":      self.config.name,
                    "to":        message.from.as_deref().unwrap_or("main"),
                    "content":   output,
                    "success":   success,
                    "packages":  packages,
                    "timestampMs": Self::now_ms(),
                }),
            );
        }

        if success {
            self.metrics.record_processed();
        } else {
            self.metrics.record_failed();
        }
        Ok(())
    }

    async fn on_heartbeat(&mut self) -> Result<()> {
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                wactorz_mqtt::topics::heartbeat(&self.config.id),
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
            .ok_or_else(|| anyhow::anyhow!("InstallerAgent already running"))?;
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
                            if let wactorz_core::message::MessageType::Command {
                                command: wactorz_core::message::ActorCommand::Stop
                            } = &m.payload { break; }
                            if let Err(e) = self.handle_message(m).await {
                                tracing::error!("[{}] {e}", self.config.name);
                                self.metrics.record_failed();
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
