//! Key-management agent — **WIK** (Waldiez Intelligence Keys).
//!
//! Manages LLM provider credentials, monitors API errors, and automatically
//! fails over to the next-priority provider when the active one hits errors
//! or rate limits.  Also tracks per-provider usage counts for WIF integration.
//!
//! NATO node: **kilo** (K → Keys)
//!
//! ## Failover behaviour
//!
//! WIK listens on `system/llm/error`.  After `error_threshold` consecutive
//! errors from the active provider it publishes `system/llm/switch` with the
//! next-priority provider config, which LlmAgent / MainActor applies live.
//!
//! Default threshold: **3** consecutive errors (configurable via `set threshold`).
//!
//! ## Usage (via IO bar)
//!
//! ```text
//! @wik-agent status                               → all providers + active
//! @wik-agent add anthropic <key> [model]          → register/update
//! @wik-agent add gemini <key> [model]             → register fallback
//! @wik-agent add openai <key> [model]             → register (⚠ warnings shown)
//! @wik-agent priority anthropic gemini openai     → set fallback order
//! @wik-agent switch gemini [reason]               → manually activate
//! @wik-agent test [provider]                      → ping provider API
//! @wik-agent usage                                → call + error counts
//! @wik-agent rotate <provider>                    → flag for key rotation
//! @wik-agent set threshold <n>                    → errors before failover (default 3)
//! @wik-agent help
//! ```

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

// ── GPT/OpenAI warning ─────────────────────────────────────────────────────────

const OPENAI_WARNING: &str = "⚠ **OpenAI / GPT — read before using as fallback**\n\n\
**Cost**\n\
• GPT-4o is significantly more expensive than Gemini Flash or Claude Haiku\n\
• Rate-limit overages bill automatically — set a spend cap at platform.openai.com\n\
• _Recommendation_: use Gemini as first fallback; GPT as last-resort only\n\n\
**Data & Privacy**\n\
• OpenAI may use API inputs to improve models **by default** on some plans\n\
• To opt out: platform.openai.com → Settings → Data Controls → \"Improve model for everyone\" → OFF\n\
• Zero-retention is available on Enterprise / API tiers with a Data Processing Addendum (DPA)\n\
• Inputs sent to OpenAI may be stored for up to 30 days for abuse monitoring\n\n\
_Type `@wik-agent add openai <key> --confirm` to register anyway._";

// ── Data model ─────────────────────────────────────────────────────────────────

#[derive(Clone)]
struct ProviderEntry {
    name:         String, // "anthropic" | "gemini" | "openai" | "ollama"
    api_key:      String,
    model:        String,
    base_url:     Option<String>,
    priority:     usize,  // 1 = highest
    call_count:   u64,
    error_count:  u64,
    rotate_flag:  bool,
    active:       bool,
}

impl ProviderEntry {
    fn default_model(name: &str) -> &'static str {
        match name {
            "anthropic" => "claude-sonnet-4-6",
            "gemini"    => "gemini-2.0-flash",
            "openai"    => "gpt-4o",
            "ollama"    => "llama3",
            _           => "unknown",
        }
    }
}

// ── WikAgent ───────────────────────────────────────────────────────────────────

pub struct WikAgent {
    config:          ActorConfig,
    state:           ActorState,
    metrics:         Arc<ActorMetrics>,
    mailbox_tx:      mpsc::Sender<Message>,
    mailbox_rx:      Option<mpsc::Receiver<Message>>,
    publisher:       Option<EventPublisher>,
    providers:       Arc<Mutex<Vec<ProviderEntry>>>,
    /// Consecutive errors received from the current active provider.
    consecutive_errors: Arc<Mutex<u32>>,
    /// How many consecutive errors before triggering failover.
    error_threshold: Arc<Mutex<u32>>,
}

impl WikAgent {
    pub fn new(config: ActorConfig) -> Self {
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config,
            state:              ActorState::Initializing,
            metrics:            Arc::new(ActorMetrics::new()),
            mailbox_tx:         tx,
            mailbox_rx:         Some(rx),
            publisher:          None,
            providers:          Arc::new(Mutex::new(Vec::new())),
            consecutive_errors: Arc::new(Mutex::new(0)),
            error_threshold:    Arc::new(Mutex::new(3)),
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

    /// Publish `system/llm/switch` to trigger a live provider swap in LlmAgent.
    fn publish_switch(&self, entry: &ProviderEntry, reason: &str) {
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                agentflow_mqtt::topics::SYSTEM_LLM_SWITCH,
                &serde_json::json!({
                    "provider":    entry.name,
                    "model":       entry.model,
                    "apiKey":      entry.api_key,
                    "baseUrl":     entry.base_url,
                    "reason":      reason,
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
    }

    /// Called when a `system/llm/error` arrives.  Increments error count for
    /// the active provider and triggers failover when threshold is reached.
    fn handle_llm_error(&self, payload: &serde_json::Value) {
        let incoming_provider = payload
            .get("provider").and_then(|v| v.as_str()).unwrap_or("");
        let error_msg = payload
            .get("error").and_then(|v| v.as_str()).unwrap_or("unknown error");

        let mut providers = self.providers.lock().unwrap();

        // Update error counter for that provider
        if let Some(entry) = providers.iter_mut().find(|e| e.active && e.name == incoming_provider) {
            entry.error_count += 1;
        }

        let mut consecutive = self.consecutive_errors.lock().unwrap();
        *consecutive += 1;

        let threshold = *self.error_threshold.lock().unwrap();
        tracing::warn!(
            "[wik-agent] LLM error #{} from '{incoming_provider}': {error_msg}",
            *consecutive,
        );

        if *consecutive < threshold {
            return; // not yet — wait for more
        }

        // Threshold reached — find the next provider in priority order
        let next = {
            let active_priority = providers.iter()
                .find(|e| e.active)
                .map(|e| e.priority)
                .unwrap_or(0);
            providers.iter()
                .filter(|e| !e.active && e.priority > active_priority && !e.api_key.is_empty())
                .min_by_key(|e| e.priority)
                .cloned()
        };

        match next {
            None => {
                tracing::error!("[wik-agent] failover: no more providers available!");
                self.reply(&format!(
                    "🔴 **WIK failover failed** — no more providers in queue.\n\n\
                     `{incoming_provider}` has hit {threshold} consecutive errors.\n\n\
                     Add a fallback provider:\n\
                     `@wik-agent add gemini <key>`"
                ));
            }
            Some(ref next_entry) => {
                let reason = format!(
                    "auto-failover: {incoming_provider} hit {threshold} consecutive errors"
                );
                tracing::info!("[wik-agent] ⚡ failover: {incoming_provider} → {}", next_entry.name);

                // Mark active/inactive
                for e in providers.iter_mut() {
                    e.active = e.name == next_entry.name;
                }
                *consecutive = 0;
                drop(providers);
                drop(consecutive);

                self.reply(&format!(
                    "⚡ **WIK Auto-Failover**\n\n\
                     `{incoming_provider}` → **`{}`** ({})\n\n\
                     _{reason}_\n\n\
                     Use `@wik-agent status` to review. `@wik-agent switch {incoming_provider}` to revert.",
                    next_entry.name, next_entry.model,
                ));
                self.publish_switch(next_entry, &reason);
            }
        }
    }

    // ── Command handlers ────────────────────────────────────────────────────────

    fn cmd_status(&self) -> String {
        let providers = self.providers.lock().unwrap();
        if providers.is_empty() {
            return "📭 No providers configured.\n\n\
                    Add one: `@wik-agent add anthropic <key>`".to_string();
        }

        let mut sorted: Vec<&ProviderEntry> = providers.iter().collect();
        sorted.sort_by_key(|e| e.priority);

        let threshold = *self.error_threshold.lock().unwrap();
        let consecutive = *self.consecutive_errors.lock().unwrap();

        let rows: Vec<String> = sorted.iter().map(|e| {
            let active_icon = if e.active { "▶" } else { "  " };
            let rotate_flag = if e.rotate_flag { " 🔄" } else { "" };
            let key_hint = if e.api_key.len() > 8 {
                format!("{}…{}", &e.api_key[..4], &e.api_key[e.api_key.len()-4..])
            } else {
                "••••••••".to_string()
            };
            format!(
                "{active_icon} **{}** [P{}] `{}` · key: `{key_hint}` · calls: {} · errors: {}{rotate_flag}",
                e.name, e.priority, e.model, e.call_count, e.error_count,
            )
        }).collect();

        let active_name = sorted.iter().find(|e| e.active).map(|e| e.name.as_str()).unwrap_or("none");
        format!(
            "**🔑 WIK — Key Status**\n\n\
             Active: **{active_name}** · threshold: {threshold} errors · consecutive now: {consecutive}\n\n{}",
            rows.join("\n")
        )
    }

    fn cmd_add(&self, parts: &[&str]) -> String {
        if parts.len() < 2 {
            return "Usage: `add <anthropic|gemini|openai|ollama> <api_key> [model]`".to_string();
        }

        let name = parts[0].to_lowercase();
        if !["anthropic", "gemini", "openai", "ollama"].contains(&name.as_str()) {
            return format!("Unknown provider `{name}`. Use: anthropic, gemini, openai, ollama.");
        }

        // OpenAI: require --confirm or show warning first
        let has_confirm = parts.iter().any(|&p| p == "--confirm");
        if name == "openai" && !has_confirm {
            return OPENAI_WARNING.to_string();
        }

        let api_key = parts[1].to_string();
        let model   = parts.get(2)
            .filter(|&&s| s != "--confirm")
            .map(|&s| s.to_string())
            .unwrap_or_else(|| ProviderEntry::default_model(&name).to_string());

        let mut providers = self.providers.lock().unwrap();

        if let Some(existing) = providers.iter_mut().find(|e| e.name == name) {
            let old_key_hint = if existing.api_key.len() > 4 {
                format!("{}…", &existing.api_key[..4])
            } else { "••••".to_string() };
            existing.api_key = api_key;
            existing.model   = model.clone();
            return format!("🔑 Updated **{name}** (was `{old_key_hint}`) → model `{model}`");
        }

        // Assign next priority
        let next_priority = providers.iter().map(|e| e.priority).max().unwrap_or(0) + 1;
        let is_first      = providers.is_empty();
        providers.push(ProviderEntry {
            name:        name.clone(),
            api_key,
            model:       model.clone(),
            base_url:    None,
            priority:    next_priority,
            call_count:  0,
            error_count: 0,
            rotate_flag: false,
            active:      is_first,
        });

        let active_note = if is_first { " — set as **active** (first provider)" } else { "" };
        format!("✅ Registered **{name}** · `{model}` [P{next_priority}]{active_note}")
    }

    fn cmd_priority(&self, parts: &[&str]) -> String {
        if parts.is_empty() {
            return "Usage: `priority <provider1> <provider2> …`\n\nExample: `priority anthropic gemini openai`".to_string();
        }

        let mut providers = self.providers.lock().unwrap();
        let mut updated = Vec::new();

        for (i, &name) in parts.iter().enumerate() {
            let priority = i + 1;
            if let Some(e) = providers.iter_mut().find(|e| e.name == name) {
                e.priority = priority;
                updated.push(format!("  {}. {name}", priority));
            } else {
                return format!("❓ Provider `{name}` not found. Add it first with `add {name} <key>`.");
            }
        }

        format!("✅ Priority order updated:\n\n{}", updated.join("\n"))
    }

    fn cmd_switch(&self, parts: &[&str]) -> String {
        if parts.is_empty() {
            return "Usage: `switch <provider> [reason]`\n\nExample: `switch gemini manual override`".to_string();
        }
        let name = parts[0].to_lowercase();
        let reason = if parts.len() > 1 { parts[1..].join(" ") } else { "manual switch".to_string() };

        let mut providers = self.providers.lock().unwrap();
        let found = providers.iter().any(|e| e.name == name);
        if !found {
            return format!("❓ Provider `{name}` not registered. Use `add {name} <key>` first.");
        }

        let target = providers.iter().find(|e| e.name == name).cloned().unwrap();
        let prev = providers.iter().find(|e| e.active).map(|e| e.name.clone()).unwrap_or_else(|| "none".to_string());
        for e in providers.iter_mut() { e.active = e.name == name; }

        *self.consecutive_errors.lock().unwrap() = 0;
        drop(providers);

        self.publish_switch(&target, &reason);
        format!("⚡ Switched: **{prev}** → **{name}** (`{}`)\n\n_Reason: {reason}_", target.model)
    }

    fn cmd_usage(&self) -> String {
        let providers = self.providers.lock().unwrap();
        if providers.is_empty() {
            return "📭 No providers registered yet.".to_string();
        }

        let total_calls:  u64 = providers.iter().map(|e| e.call_count).sum();
        let total_errors: u64 = providers.iter().map(|e| e.error_count).sum();

        let mut sorted: Vec<&ProviderEntry> = providers.iter().collect();
        sorted.sort_by_key(|e| e.priority);

        let rows: Vec<String> = sorted.iter().map(|e| {
            let bar = if total_calls > 0 {
                let frac = e.call_count as f64 / total_calls as f64;
                let filled = (frac * 10.0).round() as usize;
                format!("[{}{}]", "█".repeat(filled), "░".repeat(10 - filled))
            } else { "[░░░░░░░░░░]".to_string() };
            let err_rate = if e.call_count > 0 {
                format!("{:.1}% err", e.error_count as f64 / e.call_count as f64 * 100.0)
            } else { "no calls".to_string() };
            format!("  **{}** {bar} {} calls · {err_rate}", e.name, e.call_count)
        }).collect();

        format!(
            "**📊 WIK Usage**\n\n{}\n\n**Total**: {} calls · {} errors\n\n\
             _Use `@wif-agent add misc \"LLM API\" <cost>` to log spend._",
            rows.join("\n"), total_calls, total_errors,
        )
    }

    fn cmd_rotate(&self, parts: &[&str]) -> String {
        if parts.is_empty() {
            return "Usage: `rotate <provider>`\n\nFlags the provider key for rotation reminder.".to_string();
        }
        let name = parts[0].to_lowercase();
        let mut providers = self.providers.lock().unwrap();
        match providers.iter_mut().find(|e| e.name == name) {
            None    => format!("❓ Provider `{name}` not found."),
            Some(e) => {
                e.rotate_flag = true;
                format!("🔄 **{name}** flagged for key rotation.\n\nWhen ready:\n`@wik-agent add {name} <new_key>`")
            }
        }
    }

    fn cmd_set(&self, parts: &[&str]) -> String {
        if parts.len() < 2 {
            return "Usage: `set threshold <n>`\n\nExample: `set threshold 5`".to_string();
        }
        match (parts[0], parts[1].parse::<u32>()) {
            ("threshold", Ok(n)) if n > 0 => {
                *self.error_threshold.lock().unwrap() = n;
                format!("✅ Failover threshold set to **{n}** consecutive errors.")
            }
            ("threshold", _) => "Threshold must be a positive integer.".to_string(),
            (k, _) => format!("Unknown setting `{k}`. Currently only `threshold` is supported."),
        }
    }

    fn cmd_test(&self, parts: &[&str]) -> String {
        let name = parts.first().copied();
        let providers = self.providers.lock().unwrap();

        let targets: Vec<&ProviderEntry> = match name {
            Some(n) => providers.iter().filter(|e| e.name == n).collect(),
            None    => providers.iter().filter(|e| e.active).collect(),
        };

        if targets.is_empty() {
            return "❓ No matching provider found.".to_string();
        }

        // We can't do async HTTP here (sync context), so just report config sanity.
        let results: Vec<String> = targets.iter().map(|e| {
            let key_ok = !e.api_key.is_empty();
            let icon = if key_ok { "✅" } else { "❌" };
            let note = if key_ok {
                format!("key present · model `{}` · P{}", e.model, e.priority)
            } else {
                "no API key set".to_string()
            };
            format!("  {icon} **{}** — {note}", e.name)
        }).collect();

        format!(
            "**🔍 WIK Provider Check**\n\n{}\n\n\
             _Live ping not available from agent context — \
             errors will surface via `system/llm/error` on first call._",
            results.join("\n")
        )
    }

    fn dispatch(&self, text: &str) -> Option<String> {
        let arg = text
            .strip_prefix("@wik-agent")
            .unwrap_or(text)
            .trim();

        // Transparently handle system/llm/error JSON payloads routed by main.rs
        if let Ok(val) = serde_json::from_str::<serde_json::Value>(arg) {
            if val.get("consecutiveErrors").is_some() || val.get("provider").is_some() {
                self.handle_llm_error(&val);
                return None; // no user-visible reply for internal events
            }
        }

        let parts: Vec<&str> = arg.split_whitespace().collect();
        let cmd = parts.first().copied().unwrap_or("help");

        Some(match cmd {
            "status"   => self.cmd_status(),
            "add"      => self.cmd_add(&parts[1..]),
            "priority" => self.cmd_priority(&parts[1..]),
            "switch"   => self.cmd_switch(&parts[1..]),
            "usage"    => self.cmd_usage(),
            "rotate"   => self.cmd_rotate(&parts[1..]),
            "set"      => self.cmd_set(&parts[1..]),
            "test"     => self.cmd_test(&parts[1..]),
            "help" | "" => {
                "**WIK — Key Manager** 🔑\n\
                 _Waldiez Intelligence Keys · NATO: kilo_\n\n\
                 ```\n\
                 add <anthropic|gemini|openai|ollama> <key> [model]\n\
                 status                    all providers + active\n\
                 priority <p1> <p2> …      set failover order\n\
                 switch <provider> [why]   manual activate\n\
                 test [provider]           config sanity check\n\
                 usage                     call + error counts\n\
                 rotate <provider>         flag key for rotation\n\
                 set threshold <n>         errors before failover\n\
                 help                      this message\n\
                 ```\n\n\
                 Auto-failover: after `threshold` consecutive errors WIK\n\
                 switches to the next-priority provider automatically."
                    .to_string()
            }
            _ => format!("Unknown command: `{cmd}`. Type `help` for the full command list."),
        })
    }
}

// ── Actor implementation ────────────────────────────────────────────────────────

#[async_trait]
impl Actor for WikAgent {
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
                    "agentType":   "keymaster",
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
            .ok_or_else(|| anyhow::anyhow!("WikAgent already running"))?;

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
