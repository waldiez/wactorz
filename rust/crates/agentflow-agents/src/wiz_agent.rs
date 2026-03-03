//! WaldiezCoin Economist — **WIZ** (Waldiez In-game Zeal).
//!
//! Tracks the in-game WaldiezCoin (Ƿ) economy:
//! earns on agent activity, deducts on QA flags and alerts.
//!
//! ## Economy rules (autonomous, via MQTT routing)
//!
//! | Event              | Delta |
//! |--------------------|-------|
//! | `agents/*/spawn`   |  +10  |
//! | `agents/*/heartbeat` | +2 |
//! | `system/health`    |   +5  |
//! | `system/qa-flag`   |   −5  |
//! | `agents/*/alert`   |   −3  |
//!
//! ## Usage (via IO bar)
//!
//! ```text
//! @wiz-agent balance              → current Ƿ balance
//! @wiz-agent history [n]          → last n transactions (default 10)
//! @wiz-agent earn <n> [reason]    → credit n coins manually
//! @wiz-agent debit <n> [reason]   → debit n coins manually
//! @wiz-agent help                 → this message
//! ```
//!
//! Publishes `system/coin` → `{ balance, delta, reason, timestampMs }`.

use anyhow::Result;
use async_trait::async_trait;
use std::{
    sync::{Arc, Mutex},
    time::{SystemTime, UNIX_EPOCH},
};
use tokio::sync::mpsc;

use agentflow_core::{
    Actor, ActorConfig, ActorMetrics, ActorState, EventPublisher, Message,
};

// ── Data model ─────────────────────────────────────────────────────────────────

#[derive(Clone)]
struct CoinEntry {
    delta:  i64,
    reason: String,
    ts_ms:  u64,
}

// ── WizAgent ───────────────────────────────────────────────────────────────────

pub struct WizAgent {
    config:     ActorConfig,
    state:      ActorState,
    metrics:    Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher:  Option<EventPublisher>,
    balance:    Arc<Mutex<i64>>,
    history:    Arc<Mutex<Vec<CoinEntry>>>,
}

impl WizAgent {
    pub fn new(config: ActorConfig) -> Self {
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config,
            state:      ActorState::Initializing,
            metrics:    Arc::new(ActorMetrics::new()),
            mailbox_tx: tx,
            mailbox_rx: Some(rx),
            publisher:  None,
            balance:    Arc::new(Mutex::new(0)),
            history:    Arc::new(Mutex::new(Vec::new())),
        }
    }

    pub fn with_publisher(mut self, p: EventPublisher) -> Self {
        self.publisher = Some(p);
        self
    }

    fn now_ms() -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as u64
    }

    /// Format a millisecond timestamp as HH:MM:SS (UTC).
    fn fmt_time(ts_ms: u64) -> String {
        let secs  = ts_ms / 1000;
        let hh    = (secs / 3600) % 24;
        let mm    = (secs / 60) % 60;
        let ss    = secs % 60;
        format!("{hh:02}:{mm:02}:{ss:02}")
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

    /// Apply a coin delta and publish `system/coin`.
    fn apply_delta(&self, delta: i64, reason: &str) {
        let new_balance = {
            let mut bal = self.balance.lock().unwrap();
            *bal += delta;
            *bal
        };

        {
            let mut hist = self.history.lock().unwrap();
            hist.push(CoinEntry { delta, reason: reason.to_string(), ts_ms: Self::now_ms() });
            if hist.len() > 1000 { hist.remove(0); }
        }

        if let Some(pub_) = &self.publisher {
            pub_.publish(
                "system/coin",
                &serde_json::json!({
                    "balance":     new_balance,
                    "delta":       delta,
                    "reason":      reason,
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
    }

    /// Handle a system-level MQTT event (routed from main.rs).
    fn handle_system_event(&self, event: &str, payload: &serde_json::Value) {
        match event {
            "spawn" => {
                let name = payload
                    .get("agentName")
                    .and_then(|v| v.as_str())
                    .unwrap_or("agent");
                self.apply_delta(10, &format!("Agent spawned: {name}"));
            }
            "heartbeat" => {
                self.apply_delta(2, "Heartbeat received");
            }
            "health" => {
                self.apply_delta(5, "System health OK");
            }
            "qa-flag" => {
                self.apply_delta(-5, "QA flag raised");
            }
            "alert" => {
                self.apply_delta(-3, "Alert received");
            }
            _ => {}
        }
    }

    // ── Command handlers ────────────────────────────────────────────────────────

    fn cmd_balance(&self) -> String {
        let bal = *self.balance.lock().unwrap();
        let sign = if bal >= 0 { "" } else { "" };
        format!(
            "**Ƿ WaldiezCoin Balance**\n\nCurrent balance: **{sign}Ƿ {bal}**\n\n\
             _Earn: agent spawn (+10) · heartbeat (+2) · healthy system (+5)_\n\
             _Lose: QA flag (−5) · alert (−3)_"
        )
    }

    fn cmd_history(&self, n: usize) -> String {
        let hist = self.history.lock().unwrap();
        if hist.is_empty() {
            return "📭 No coin history yet.".to_string();
        }
        let count = n.min(50).min(hist.len());
        let rows: Vec<String> = hist
            .iter()
            .rev()
            .take(count)
            .map(|e| {
                let sign  = if e.delta >= 0 { "+" } else { "" };
                let time  = Self::fmt_time(e.ts_ms);
                format!("  `{time}` {sign}{delta} Ƿ — {reason}", delta = e.delta, reason = e.reason)
            })
            .collect();
        let bal = *self.balance.lock().unwrap();
        format!(
            "**Ƿ Coin History** (last {count})\n\n{}\n\n**Balance: Ƿ {bal}**",
            rows.join("\n")
        )
    }

    fn cmd_earn(&self, parts: &[&str]) -> String {
        if parts.is_empty() {
            return "Usage: `earn <amount> [reason…]`\n\nExample: `earn 50 prize`".to_string();
        }
        let amount: i64 = match parts[0].parse() {
            Ok(v) if v > 0 => v,
            Ok(_)  => return "Amount must be positive.".to_string(),
            Err(_) => return format!("Invalid amount: `{}`", parts[0]),
        };
        let reason = parts.get(1..).map(|p| p.join(" ")).unwrap_or_else(|| "manual earn".to_string());
        self.apply_delta(amount, &reason);
        let bal = *self.balance.lock().unwrap();
        format!("✅ Earned **Ƿ {amount}** — {reason}\n\n**New balance: Ƿ {bal}**")
    }

    fn cmd_debit(&self, parts: &[&str]) -> String {
        if parts.is_empty() {
            return "Usage: `debit <amount> [reason…]`\n\nExample: `debit 20 penalty`".to_string();
        }
        let amount: i64 = match parts[0].parse() {
            Ok(v) if v > 0 => v,
            Ok(_)  => return "Amount must be positive.".to_string(),
            Err(_) => return format!("Invalid amount: `{}`", parts[0]),
        };
        let reason = parts.get(1..).map(|p| p.join(" ")).unwrap_or_else(|| "manual debit".to_string());
        self.apply_delta(-amount, &reason);
        let bal = *self.balance.lock().unwrap();
        format!("📉 Debited **Ƿ {amount}** — {reason}\n\n**New balance: Ƿ {bal}**")
    }

    /// Dispatch an incoming text message — either a system event or a user command.
    /// Returns `None` for silent system events, `Some(reply)` for user commands.
    fn dispatch(&self, text: &str) -> Option<String> {
        // Check for MQTT-routed system event (JSON with __event field)
        if let Ok(json) = serde_json::from_str::<serde_json::Value>(text) {
            if let Some(event) = json.get("__event").and_then(|v| v.as_str()) {
                self.handle_system_event(event, &json);
                return None;
            }
        }

        // User command
        let arg = text
            .strip_prefix("@wiz-agent")
            .unwrap_or(text)
            .trim();

        let parts: Vec<&str> = arg.split_whitespace().collect();
        let cmd = parts.first().copied().unwrap_or("help");

        Some(match cmd {
            "balance"      => self.cmd_balance(),
            "history"      => {
                let n: usize = parts.get(1).and_then(|s| s.parse().ok()).unwrap_or(10);
                self.cmd_history(n)
            }
            "earn"         => self.cmd_earn(&parts[1..]),
            "debit"        => self.cmd_debit(&parts[1..]),
            "help" | ""    => {
                "**WIZ — WaldiezCoin Economist** Ƿ\n\
                 _In-game economy for the AgentFlow swarm_\n\n\
                 ```\n\
                 balance              current Ƿ balance\n\
                 history [n]          last n transactions (default 10)\n\
                 earn <n> [reason]    credit n coins manually\n\
                 debit <n> [reason]   debit n coins manually\n\
                 help                 this message\n\
                 ```\n\n\
                 **Auto-economy:**\n\
                 +10 Ƿ agent spawn · +2 Ƿ heartbeat · +5 Ƿ healthy system\n\
                 −5 Ƿ QA flag · −3 Ƿ alert"
                    .to_string()
            }
            _ => format!("Unknown command: `{cmd}`. Type `help` for the full command list."),
        })
    }
}

// ── Actor implementation ────────────────────────────────────────────────────────

#[async_trait]
impl Actor for WizAgent {
    fn id(&self)           -> String                { self.config.id.clone() }
    fn name(&self)         -> &str                  { &self.config.name }
    fn state(&self)        -> ActorState            { self.state.clone() }
    fn metrics(&self)      -> Arc<ActorMetrics>     { Arc::clone(&self.metrics) }
    fn mailbox(&self)      -> mpsc::Sender<Message> { self.mailbox_tx.clone() }
    fn is_protected(&self) -> bool                  { self.config.protected }

    async fn on_start(&mut self) -> Result<()> {
        self.state = ActorState::Running;
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                agentflow_mqtt::topics::spawn(&self.config.id),
                &serde_json::json!({
                    "agentId":     self.config.id,
                    "agentName":   self.config.name,
                    "agentType":   "coin",
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
        Ok(())
    }

    async fn handle_message(&mut self, message: Message) -> Result<()> {
        use agentflow_core::message::MessageType;

        let content = match &message.payload {
            MessageType::Text { content }         => content.trim().to_string(),
            MessageType::Task { description, .. } => description.trim().to_string(),
            _ => return Ok(()),
        };

        if let Some(reply) = self.dispatch(&content) {
            self.reply(&reply);
        }
        Ok(())
    }

    async fn on_heartbeat(&mut self) -> Result<()> {
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                agentflow_mqtt::topics::heartbeat(&self.config.id),
                &serde_json::json!({
                    "agentId":     self.config.id,
                    "agentName":   self.config.name,
                    "state":       self.state,
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
            .ok_or_else(|| anyhow::anyhow!("WizAgent already running"))?;

        let mut hb = tokio::time::interval(std::time::Duration::from_secs(
            self.config.heartbeat_interval_secs,
        ));
        hb.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

        loop {
            tokio::select! {
                biased;
                msg = rx.recv() => match msg {
                    None    => break,
                    Some(m) => {
                        self.metrics.record_received();
                        if let agentflow_core::message::MessageType::Command {
                            command: agentflow_core::message::ActorCommand::Stop,
                        } = &m.payload { break; }
                        match self.handle_message(m).await {
                            Ok(_)  => self.metrics.record_processed(),
                            Err(e) => {
                                tracing::error!("[{}] {e}", self.config.name);
                                self.metrics.record_failed();
                            }
                        }
                    }
                },
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
