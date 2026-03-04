//! TickAgent — in-process scheduler/timer (NATO: CHRON / Tango).
//!
//! Schedule one-shot messages or recurring reminders without any external
//! cron daemon. All timers live in-process via Tokio tasks.
//!
//! ## Commands
//!
//! | Command | Description |
//! |---------|-------------|
//! | `at <HH:MM> <message>` | Fire once at clock time (today/tomorrow) |
//! | `in <n> <unit> <message>` | Fire after delay (s/m/h/d) |
//! | `every <n> <unit> <message>` | Recurring timer |
//! | `list` | Show all pending timers |
//! | `cancel <id-prefix>` | Cancel a timer |
//! | `clear` | Cancel all timers |
//! | `help` | Show this message |

use anyhow::Result;
use async_trait::async_trait;
use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::sync::{mpsc, Mutex};

use agentflow_core::{
    Actor, ActorConfig, ActorMetrics, ActorState, EventPublisher, Message,
};

const HELP: &str = "\
**CHRON — TickAgent** ⏱
_In-process scheduler — no external cron needed_

| Command | Description |
|---------|-------------|
| `at <HH:MM> <message>` | Fire once at clock time today/tomorrow |
| `in <n> <unit> <msg>` | Fire after delay (s/m/h/d) |
| `every <n> <unit> <msg>` | Recurring timer |
| `list` | Show pending timers |
| `cancel <id>` | Cancel timer by ID prefix |
| `clear` | Cancel all timers |
| `help` | This message |

**Examples:**
```
in 5 m check the oven
at 09:00 Good morning, team!
every 1 h status check
```";

// ── Timer ─────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
struct Timer {
    id: String,
    message: String,
    /// Unix timestamp (ms) of next fire
    fire_at_ms: u64,
    /// 0 = one-shot; >0 = interval in ms
    interval_ms: u64,
}

impl Timer {
    fn label(&self) -> String {
        let now_ms = now_ms();
        if self.fire_at_ms <= now_ms {
            return "overdue".to_string();
        }
        let remaining_s = (self.fire_at_ms - now_ms) / 1000;
        if remaining_s < 60 {
            format!("in {remaining_s}s")
        } else if remaining_s < 3600 {
            format!("in {}m", remaining_s / 60)
        } else if remaining_s < 86400 {
            format!("in {:.1}h", remaining_s as f64 / 3600.0)
        } else {
            format!("in {:.1}d", remaining_s as f64 / 86400.0)
        }
    }

    fn kind(&self) -> &str {
        if self.interval_ms > 0 { "every" } else { "once" }
    }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

/// Parse `<n> <unit>` into milliseconds. Returns None on failure.
fn parse_ms(n_str: &str, unit: &str) -> Option<u64> {
    let n: f64 = n_str.parse().ok()?;
    if n <= 0.0 {
        return None;
    }
    let factor_ms: f64 = match unit.to_lowercase().trim_end_matches('s') {
        "s" | "sec" | "second"  => 1_000.0,
        "m" | "min" | "minute"  => 60_000.0,
        "h" | "hr"  | "hour"    => 3_600_000.0,
        "d" | "day"             => 86_400_000.0,
        _ => return None,
    };
    Some((n * factor_ms) as u64)
}

/// Parse `HH:MM` into Unix timestamp (ms) for the next occurrence.
fn parse_hhmm(s: &str) -> Option<u64> {
    let parts: Vec<&str> = s.trim().splitn(2, ':').collect();
    if parts.len() != 2 { return None; }
    let h: u32 = parts[0].parse().ok()?;
    let m: u32 = parts[1].parse().ok()?;
    if h > 23 || m > 59 { return None; }

    let now_ms = now_ms();
    let secs_since_epoch = now_ms / 1000;
    let day_start = (secs_since_epoch / 86400) * 86400;
    let target_secs = day_start + (h as u64) * 3600 + (m as u64) * 60;

    // If already past, schedule for tomorrow
    let target_secs = if target_secs * 1000 <= now_ms {
        target_secs + 86400
    } else {
        target_secs
    };
    Some(target_secs * 1000)
}

fn short_id() -> String {
    // Generate a short unique ID without uuid dep
    let ms = now_ms();
    format!("{ms:x}{:04x}", rand_u16())
}

fn rand_u16() -> u16 {
    // Simple LCG using process id + time as seed
    let seed = std::process::id() as u64 ^ now_ms();
    (seed.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407) >> 48) as u16
}

// ── Agent ─────────────────────────────────────────────────────────────────────

pub struct TickAgent {
    config: ActorConfig,
    state: ActorState,
    metrics: Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher: Option<EventPublisher>,
    timers: Arc<Mutex<HashMap<String, Timer>>>,
}

impl TickAgent {
    pub fn new(config: ActorConfig) -> Self {
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config,
            state: ActorState::Initializing,
            metrics: Arc::new(ActorMetrics::new()),
            mailbox_tx: tx,
            mailbox_rx: Some(rx),
            publisher: None,
            timers: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    pub fn with_publisher(mut self, p: EventPublisher) -> Self {
        self.publisher = Some(p);
        self
    }

    fn now_ms_static() -> u64 { now_ms() }

    fn reply(&self, content: &str) {
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                agentflow_mqtt::topics::chat(&self.config.id),
                &serde_json::json!({
                    "from":        self.config.name,
                    "to":          "user",
                    "content":     content,
                    "timestampMs": Self::now_ms_static(),
                }),
            );
        }
    }

    // ── Command handlers ──────────────────────────────────────────────────────

    async fn cmd_at(&self, time_str: &str, message: &str) -> String {
        let Some(fire_at_ms) = parse_hhmm(time_str) else {
            return format!("Invalid time `{time_str}`. Use HH:MM (24-h), e.g. `14:30`.");
        };
        let id = short_id();
        let remaining_s = (fire_at_ms.saturating_sub(now_ms())) / 1000;
        let (h, m) = (remaining_s / 3600, (remaining_s % 3600) / 60);
        let label = if h > 0 { format!("{h}h {m}m") } else { format!("{m}m") };

        let timer = Timer { id: id.clone(), message: message.to_string(), fire_at_ms, interval_ms: 0 };
        self.timers.lock().await.insert(id.clone(), timer);

        self.spawn_timer_task(&id, fire_at_ms, 0, message.to_string());

        format!("✓ Timer `{id}` set for **{time_str}** (in {label}).\n\nMessage: _{message}_")
    }

    async fn cmd_in(&self, n_str: &str, unit: &str, message: &str) -> String {
        let Some(delay_ms) = parse_ms(n_str, unit) else {
            return format!("Invalid delay `{n_str} {unit}`. Use e.g. `5 m`, `2 h`, `30 s`.");
        };
        let fire_at_ms = now_ms() + delay_ms;
        let id = short_id();
        let timer = Timer { id: id.clone(), message: message.to_string(), fire_at_ms, interval_ms: 0 };
        self.timers.lock().await.insert(id.clone(), timer);
        self.spawn_timer_task(&id, fire_at_ms, 0, message.to_string());
        format!("✓ Timer `{id}` — fires in **{n_str} {unit}**.\n\nMessage: _{message}_")
    }

    async fn cmd_every(&self, n_str: &str, unit: &str, message: &str) -> String {
        let Some(interval_ms) = parse_ms(n_str, unit) else {
            return format!("Invalid interval `{n_str} {unit}`. Use e.g. `5 m`, `1 h`.");
        };
        if interval_ms < 60_000 {
            return "Minimum recurring interval is 1 minute.".to_string();
        }
        let fire_at_ms = now_ms() + interval_ms;
        let id = short_id();
        let timer = Timer { id: id.clone(), message: message.to_string(), fire_at_ms, interval_ms };
        self.timers.lock().await.insert(id.clone(), timer);
        self.spawn_timer_task(&id, fire_at_ms, interval_ms, message.to_string());
        format!("✓ Recurring timer `{id}` — every **{n_str} {unit}**.\n\nMessage: _{message}_")
    }

    async fn cmd_list(&self) -> String {
        let timers = self.timers.lock().await;
        if timers.is_empty() {
            return "No active timers. Use `in`, `at`, or `every` to schedule one.".to_string();
        }
        let mut list: Vec<&Timer> = timers.values().collect();
        list.sort_by_key(|t| t.fire_at_ms);
        let mut lines = vec![format!("**Active Timers ({}):**\n", list.len())];
        for t in list {
            let msg_preview = if t.message.len() > 60 { &t.message[..60] } else { &t.message };
            lines.push(format!("- `{}` [{}] {} — _{msg_preview}_", t.id, t.kind(), t.label()));
        }
        lines.join("\n")
    }

    async fn cmd_cancel(&self, prefix: &str) -> String {
        if prefix.is_empty() {
            return "Usage: `cancel <id-prefix>`  (use `list` to see IDs)".to_string();
        }
        let mut timers = self.timers.lock().await;
        let matches: Vec<String> = timers.keys()
            .filter(|id| id.starts_with(prefix))
            .cloned()
            .collect();
        if matches.is_empty() {
            return format!("No timer found matching `{prefix}`.");
        }
        if matches.len() > 1 {
            return format!("Ambiguous prefix `{prefix}` matches {} timers. Be more specific.", matches.len());
        }
        timers.remove(&matches[0]);
        format!("✓ Timer `{}` cancelled.", matches[0])
    }

    async fn cmd_clear(&self) -> String {
        let mut timers = self.timers.lock().await;
        let count = timers.len();
        timers.clear();
        format!("✓ Cleared {count} timer(s).")
    }

    // ── Timer tasks ───────────────────────────────────────────────────────────

    fn spawn_timer_task(&self, id: &str, fire_at_ms: u64, interval_ms: u64, message: String) {
        let timers = Arc::clone(&self.timers);
        let pub_ = self.publisher.clone();
        let agent_id = self.config.id.clone();
        let agent_name = self.config.name.clone();
        let timer_id = id.to_string();

        tokio::spawn(async move {
            loop {
                let delay = fire_at_ms.saturating_sub(now_ms());
                if delay > 0 {
                    tokio::time::sleep(Duration::from_millis(delay)).await;
                }

                // Fire
                if let Some(pub_) = &pub_ {
                    let short = &timer_id[..timer_id.len().min(12)];
                    pub_.publish(
                        agentflow_mqtt::topics::chat(&agent_id),
                        &serde_json::json!({
                            "from":        agent_name,
                            "to":          "user",
                            "content":     format!("⏰ **Timer `{short}…`** fired!\n\n{message}"),
                            "timestampMs": now_ms(),
                        }),
                    );
                }

                if interval_ms == 0 {
                    timers.lock().await.remove(&timer_id);
                    break;
                }

                // Advance to next interval
                let mut locked = timers.lock().await;
                if let Some(t) = locked.get_mut(&timer_id) {
                    t.fire_at_ms = now_ms() + interval_ms;
                    let next_fire = t.fire_at_ms;
                    drop(locked);
                    // wait for next fire
                    let wait = next_fire.saturating_sub(now_ms());
                    tokio::time::sleep(Duration::from_millis(wait)).await;
                } else {
                    // Timer was cancelled
                    break;
                }
            }
        });
    }

    async fn dispatch(&self, text: &str) {
        // Strip agent prefix
        let text = {
            let lower = text.to_lowercase();
            if lower.starts_with("@chron-agent")
                || lower.starts_with("@chron_agent")
                || lower.starts_with("@tick-agent")
                || lower.starts_with("@tick_agent")
            {
                text.splitn(2, char::is_whitespace).nth(1).unwrap_or("").trim()
            } else {
                text.trim()
            }
        };

        let parts: Vec<&str> = text.splitn(4, char::is_whitespace).collect();
        let reply = match parts.as_slice() {
            [] | ["" | "help" | "?"] => HELP.to_string(),
            ["list"] => self.cmd_list().await,
            ["clear"] => self.cmd_clear().await,
            ["cancel" | "del" | "rm", id] => self.cmd_cancel(id).await,
            ["at", time_str, msg] => self.cmd_at(time_str, msg).await,
            ["at", time_str, w1, rest] => {
                self.cmd_at(time_str, &format!("{w1} {rest}")).await
            }
            ["in", n, unit, msg] => self.cmd_in(n, unit, msg).await,
            ["every", n, unit, msg] => self.cmd_every(n, unit, msg).await,
            [cmd, ..] => format!("Unknown command: `{cmd}`. Type `help`."),
        };
        self.reply(&reply);
    }
}

// ── Actor impl ────────────────────────────────────────────────────────────────

#[async_trait]
impl Actor for TickAgent {
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
                    "agentType": "scheduler",
                    "timestampMs": now_ms(),
                }),
            );
        }
        tracing::info!("[chron] started");
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
                    "timestampMs": now_ms(),
                }),
            );
        }
        Ok(())
    }

    async fn run(&mut self) -> Result<()> {
        self.on_start().await?;
        let mut rx = self.mailbox_rx.take()
            .ok_or_else(|| anyhow::anyhow!("TickAgent already running"))?;
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
                            tracing::error!("[chron] {e}");
                            self.metrics.record_failed();
                        }
                    }
                },
                _ = hb.tick() => {
                    self.metrics.record_heartbeat();
                    if let Err(e) = self.on_heartbeat().await {
                        tracing::error!("[chron] heartbeat: {e}");
                    }
                }
            }
        }
        self.state = ActorState::Stopped;
        self.on_stop().await
    }
}
