//! Finance-expert agent — **WIF** (Waldiez Intelligence Finance).
//!
//! Tracks expenses, enforces budget limits, and provides financial
//! calculations and advice on demand. No external APIs required.
//!
//! ## Usage (via IO bar)
//!
//! ```text
//! @wif-agent add 25.50 food coffee        → log expense
//! @wif-agent add 120 rent monthly rent    → log with note
//! @wif-agent budget food 300              → set category budget
//! @wif-agent summary                      → all-time spending report
//! @wif-agent summary today|week|month     → filtered report
//! @wif-agent balance                      → budget vs actuals
//! @wif-agent clear food                   → clear category
//! @wif-agent clear                        → clear everything
//! @wif-agent calc compound 1000 5 10      → compound interest
//! @wif-agent calc loan 200000 4.5 30      → monthly mortgage
//! @wif-agent calc roi 1000 1350           → return on investment
//! @wif-agent calc tax 80000 25            → tax estimate
//! @wif-agent tips saving|investing|debt   → financial tips
//! @wif-agent help                         → this message
//! ```
//!
//! All data is held in memory — restarting the agent resets the ledger.

use anyhow::Result;
use async_trait::async_trait;
use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
    time::{SystemTime, UNIX_EPOCH},
};
use tokio::sync::mpsc;

use wactorz_core::{Actor, ActorConfig, ActorMetrics, ActorState, EventPublisher, Message};

// ── Data model ─────────────────────────────────────────────────────────────────

#[derive(Clone)]
struct Expense {
    amount: f64,
    category: String,
    #[allow(dead_code)] // stored for future retrieval (e.g. export, search)
    note: String,
    ts_ms: u64,
}

// ── WifAgent ───────────────────────────────────────────────────────────────────

pub struct WifAgent {
    config: ActorConfig,
    state: ActorState,
    metrics: Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher: Option<EventPublisher>,
    expenses: Arc<Mutex<Vec<Expense>>>,
    budgets: Arc<Mutex<HashMap<String, f64>>>,
}

impl WifAgent {
    pub fn new(config: ActorConfig) -> Self {
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config,
            state: ActorState::Initializing,
            metrics: Arc::new(ActorMetrics::new()),
            mailbox_tx: tx,
            mailbox_rx: Some(rx),
            publisher: None,
            expenses: Arc::new(Mutex::new(Vec::new())),
            budgets: Arc::new(Mutex::new(HashMap::new())),
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
                wactorz_mqtt::topics::chat(&self.config.id),
                &serde_json::json!({
                    "from":        self.config.name,
                    "to":          "user",
                    "content":     content,
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
    }

    // ── Command handlers ────────────────────────────────────────────────────────

    fn cmd_add(&self, parts: &[&str]) -> String {
        if parts.is_empty() {
            return "Usage: `add <amount> [category] [note…]`\n\nExample: `add 25.50 food coffee`"
                .to_string();
        }

        let raw = parts[0].trim_start_matches(['$', '€', '£', '¥', '+']);
        let amount: f64 = match raw.parse() {
            Ok(v) if v > 0.0 => v,
            Ok(_) => return "Amount must be positive.".to_string(),
            Err(_) => return format!("Invalid amount: `{}`", parts[0]),
        };

        let category = parts.get(1).copied().unwrap_or("misc").to_lowercase();
        let note = parts.get(2..).map(|p| p.join(" ")).unwrap_or_default();

        let expense = Expense {
            amount,
            category: category.clone(),
            note: note.clone(),
            ts_ms: Self::now_ms(),
        };
        let mut expenses = self.expenses.lock().unwrap();
        expenses.push(expense);

        let total: f64 = expenses
            .iter()
            .filter(|e| e.category == category)
            .map(|e| e.amount)
            .sum();

        let budgets = self.budgets.lock().unwrap();
        let budget_line = if let Some(&budget) = budgets.get(&category) {
            let pct = (total / budget * 100.0).min(999.0);
            let remaining = budget - total;
            let icon = if pct >= 100.0 {
                "🔴"
            } else if pct >= 80.0 {
                "🟡"
            } else {
                "🟢"
            };
            format!(
                "\n{icon} **{category}** budget: ${total:.2} / ${budget:.2} ({pct:.0}%) — ${remaining:.2} left"
            )
        } else {
            format!("\n📊 **{category}** running total: ${total:.2}")
        };

        let note_part = if note.is_empty() {
            String::new()
        } else {
            format!(" _{note}_")
        };
        format!("✅ Logged **${amount:.2}** → `{category}`{note_part}{budget_line}")
    }

    fn cmd_budget(&self, parts: &[&str]) -> String {
        if parts.len() < 2 {
            return "Usage: `budget <category> <amount>`\n\nExample: `budget food 300`".to_string();
        }
        let category = parts[0].to_lowercase();
        let raw = parts[1].trim_start_matches(['$', '€', '£', '¥']);
        let amount: f64 = match raw.parse() {
            Ok(v) if v >= 0.0 => v,
            Ok(_) => return "Budget must be ≥ 0.".to_string(),
            Err(_) => return format!("Invalid amount: `{}`", parts[1]),
        };

        let mut budgets = self.budgets.lock().unwrap();
        let verb = if budgets.contains_key(&category) {
            "Updated"
        } else {
            "Set"
        };
        budgets.insert(category.clone(), amount);

        let expenses = self.expenses.lock().unwrap();
        let spent: f64 = expenses
            .iter()
            .filter(|e| e.category == category)
            .map(|e| e.amount)
            .sum();
        let pct = if amount > 0.0 {
            spent / amount * 100.0
        } else {
            0.0
        };
        let icon = if pct >= 100.0 {
            "🔴"
        } else if pct >= 80.0 {
            "🟡"
        } else {
            "🟢"
        };

        format!(
            "📋 {verb} budget: **{category}** → **${amount:.2}**\n{icon} Currently at ${spent:.2} ({pct:.0}%)"
        )
    }

    fn cmd_summary(&self, period: &str) -> String {
        let expenses = self.expenses.lock().unwrap();
        if expenses.is_empty() {
            return "📭 No expenses recorded yet.\n\nTry: `add 25 food coffee` to get started."
                .to_string();
        }

        let now_ms = Self::now_ms();
        let cutoff_ms: u64 = match period {
            "today" => now_ms.saturating_sub(86_400_000),
            "week" => now_ms.saturating_sub(7 * 86_400_000),
            "month" => now_ms.saturating_sub(30 * 86_400_000),
            _ => 0,
        };

        let filtered: Vec<&Expense> = expenses.iter().filter(|e| e.ts_ms >= cutoff_ms).collect();
        if filtered.is_empty() {
            return format!("📭 No expenses for `{period}`. Try: `summary all`");
        }

        let total: f64 = filtered.iter().map(|e| e.amount).sum();
        let mut by_cat: HashMap<String, f64> = HashMap::new();
        for e in &filtered {
            *by_cat.entry(e.category.clone()).or_default() += e.amount;
        }

        let mut cats: Vec<(String, f64)> = by_cat.into_iter().collect();
        cats.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));

        let budgets = self.budgets.lock().unwrap();
        let period_label = match period {
            "today" => "Today",
            "week" => "This Week",
            "month" => "This Month",
            _ => "All Time",
        };

        let rows: Vec<String> = cats
            .iter()
            .map(|(cat, amt)| {
                let frac = if total > 0.0 { amt / total } else { 0.0 };
                let bar = Self::mini_bar(frac);
                let budget_note = budgets
                    .get(cat)
                    .map(|&b| {
                        let pct = amt / b * 100.0;
                        let icon = if pct >= 100.0 {
                            "🔴"
                        } else if pct >= 80.0 {
                            "🟡"
                        } else {
                            "🟢"
                        };
                        format!(" {icon} {pct:.0}% of ${b:.0}")
                    })
                    .unwrap_or_default();
                format!("  {bar} **{cat}**: ${amt:.2}{budget_note}")
            })
            .collect();

        format!(
            "**💰 Expense Summary — {period_label}**\n\n{}\n\n**Total: ${total:.2}** ({n} transactions)",
            rows.join("\n"),
            n = filtered.len(),
        )
    }

    fn mini_bar(fraction: f64) -> &'static str {
        match (fraction * 5.0).round() as usize {
            0 => "▁",
            1 => "▂",
            2 => "▄",
            3 => "▆",
            4 => "▇",
            _ => "█",
        }
    }

    fn cmd_balance(&self) -> String {
        let budgets = self.budgets.lock().unwrap();
        if budgets.is_empty() {
            return "📋 No budgets set yet.\n\nTry: `budget food 300` then `add 25 food coffee`"
                .to_string();
        }

        let expenses = self.expenses.lock().unwrap();
        let mut sorted: Vec<String> = budgets.keys().cloned().collect();
        sorted.sort();

        let mut rows = Vec::new();
        let mut total_budget = 0.0f64;
        let mut total_spent = 0.0f64;

        for cat in &sorted {
            let budget = budgets[cat];
            let spent: f64 = expenses
                .iter()
                .filter(|e| e.category == *cat)
                .map(|e| e.amount)
                .sum();
            let pct = if budget > 0.0 {
                (spent / budget * 100.0).min(999.9)
            } else {
                0.0
            };
            let filled = ((pct / 100.0 * 10.0).min(10.0)) as usize;
            let bar = format!("[{}{}]", "█".repeat(filled), "░".repeat(10 - filled));
            let icon = if pct >= 100.0 {
                "🔴"
            } else if pct >= 80.0 {
                "🟡"
            } else {
                "🟢"
            };
            let remaining = budget - spent;
            let rem_str = if remaining >= 0.0 {
                format!("${remaining:.2} left")
            } else {
                format!("${:.2} over", remaining.abs())
            };
            rows.push(format!(
                "{icon} **{cat}**: {bar} ${spent:.2} / ${budget:.2} ({pct:.0}%) — {rem_str}"
            ));
            total_budget += budget;
            total_spent += spent;
        }

        let overall_pct = if total_budget > 0.0 {
            (total_spent / total_budget * 100.0).min(999.9)
        } else {
            0.0
        };
        let overall_icon = if overall_pct >= 100.0 {
            "🔴"
        } else if overall_pct >= 80.0 {
            "🟡"
        } else {
            "🟢"
        };
        rows.push(String::new());
        rows.push(format!(
            "{overall_icon} **TOTAL**: ${total_spent:.2} / ${total_budget:.2} ({overall_pct:.0}%)"
        ));

        format!("**📊 Budget Balance**\n\n{}", rows.join("\n"))
    }

    fn cmd_clear(&self, category: Option<&str>) -> String {
        let mut expenses = self.expenses.lock().unwrap();
        match category {
            None => {
                let n = expenses.len();
                expenses.clear();
                format!("🗑 Cleared all {n} expenses.")
            }
            Some(cat) => {
                let before = expenses.len();
                expenses.retain(|e| e.category != cat);
                let removed = before - expenses.len();
                format!("🗑 Cleared {removed} expenses from `{cat}`.")
            }
        }
    }

    fn cmd_calc(&self, parts: &[&str]) -> String {
        match parts.first().copied().unwrap_or("") {
            "compound" | "ci" => {
                if parts.len() < 4 {
                    return "Usage: `calc compound <principal> <rate%> <years>`\n\nExample: `calc compound 10000 7 20`".to_string();
                }
                let p: f64 = parts[1].trim_start_matches('$').parse().unwrap_or(0.0);
                let r: f64 = parts[2].trim_end_matches('%').parse::<f64>().unwrap_or(0.0) / 100.0;
                let t: f64 = parts[3].parse().unwrap_or(0.0);
                if p <= 0.0 || t <= 0.0 {
                    return "Principal and years must be positive.".to_string();
                }
                let n = 12.0; // monthly compounding
                let fv = p * (1.0 + r / n).powf(n * t);
                let int = fv - p;
                let rate_pct = r * 100.0;
                let gain_pct = if p > 0.0 { int / p * 100.0 } else { 0.0 };
                format!(
                    "**📈 Compound Interest (monthly)**\n\nPrincipal : ${p:.2}\nRate      : {rate_pct:.2}% p.a.\nTerm      : {t} years\n\n→ Future Value  : **${fv:.2}**\n→ Interest Earned: **${int:.2}** ({gain_pct:.0}% gain)"
                )
            }

            "loan" | "mortgage" => {
                if parts.len() < 4 {
                    return "Usage: `calc loan <principal> <rate%> <years>`\n\nExample: `calc loan 300000 4.5 30`".to_string();
                }
                let p: f64 = parts[1].trim_start_matches('$').parse().unwrap_or(0.0);
                let r: f64 =
                    parts[2].trim_end_matches('%').parse::<f64>().unwrap_or(0.0) / 100.0 / 12.0;
                let n: f64 = parts[3].parse::<f64>().unwrap_or(0.0) * 12.0;
                if p <= 0.0 || n <= 0.0 {
                    return "Principal and years must be positive.".to_string();
                }
                let (monthly, total, interest) = if r == 0.0 {
                    let m = p / n;
                    (m, p, 0.0)
                } else {
                    let m = p * r * (1.0 + r).powf(n) / ((1.0 + r).powf(n) - 1.0);
                    (m, m * n, m * n - p)
                };
                let rate_pct: f64 = parts[2].trim_end_matches('%').parse().unwrap_or(0.0);
                format!(
                    "**🏠 Loan / Mortgage Calculator**\n\nPrincipal : ${p:.2}\nRate      : {rate_pct:.2}% p.a.\nTerm      : {} years\n\n→ Monthly Payment : **${monthly:.2}**\n→ Total Repaid    : **${total:.2}**\n→ Total Interest  : **${interest:.2}**",
                    parts[3],
                )
            }

            "roi" => {
                if parts.len() < 3 {
                    return "Usage: `calc roi <initial> <final>`\n\nExample: `calc roi 5000 7500`"
                        .to_string();
                }
                let initial: f64 = parts[1].trim_start_matches('$').parse().unwrap_or(0.0);
                let final_val: f64 = parts[2].trim_start_matches('$').parse().unwrap_or(0.0);
                if initial == 0.0 {
                    return "Initial value cannot be zero.".to_string();
                }
                let roi = (final_val - initial) / initial * 100.0;
                let gain = final_val - initial;
                let icon = if gain >= 0.0 { "📈" } else { "📉" };
                format!(
                    "{icon} **Return on Investment**\n\nInitial : ${initial:.2}\nFinal   : ${final_val:.2}\nGain    : ${gain:+.2}\n\n→ ROI: **{roi:+.2}%**"
                )
            }

            "tax" => {
                if parts.len() < 2 {
                    return "Usage: `calc tax <income> [rate%]`\n\nExample: `calc tax 75000 25`"
                        .to_string();
                }
                let income: f64 = parts[1].trim_start_matches('$').parse().unwrap_or(0.0);
                let rate: f64 = parts
                    .get(2)
                    .and_then(|s| s.trim_end_matches('%').parse().ok())
                    .unwrap_or(25.0);
                let tax = income * rate / 100.0;
                let net = income - tax;
                format!(
                    "**💸 Tax Estimate**\n\nGross Income : ${income:.2}\nTax Rate     : {rate:.1}%\n\n→ Tax         : **${tax:.2}**\n→ Net Income  : **${net:.2}**\n\n_Note: simplified estimate — consult a tax professional._"
                )
            }

            _ => "**calc** subcommands:\n\n\
                 ```\n\
                 calc compound <principal> <rate%> <years>  — compound interest\n\
                 calc loan <principal> <rate%> <years>       — loan / mortgage\n\
                 calc roi <initial> <final>                  — return on investment\n\
                 calc tax <income> [rate%]                   — tax estimate (default 25%)\n\
                 ```"
            .to_string(),
        }
    }

    fn cmd_tips(&self, topic: &str) -> String {
        match topic {
            "saving" | "save" => "**💡 Saving Tips**\n\n\
                 1. **50/30/20 rule** — 50% needs · 30% wants · 20% savings\n\
                 2. **Pay yourself first** — automate a transfer on payday\n\
                 3. **Emergency fund** — target 3–6 months of expenses\n\
                 4. **Cut subscriptions** — review monthly recurring charges\n\
                 5. **Track everything** — use `add <amount> <category>` to log expenses\n\n\
                 _Try: `budget food 300` then `add 25 food coffee` to start tracking._"
                .to_string(),
            "investing" | "invest" => "**📈 Investing Tips**\n\n\
                 1. **Start early** — compound interest is exponential; time matters most\n\
                 2. **Diversify** — spread across asset classes, geographies\n\
                 3. **Low-cost index funds** — outperform most active funds long-term\n\
                 4. **Dollar-cost average** — invest fixed amounts on a schedule\n\
                 5. **Don't time the market** — time *in* the market beats timing it\n\n\
                 _Try: `calc compound 10000 8 30` to see long-term growth._"
                .to_string(),
            "debt" => "**⚡ Debt Elimination Tips**\n\n\
                 1. **Avalanche method** — pay highest-interest debt first (saves most money)\n\
                 2. **Snowball method** — pay smallest balance first (psychological wins)\n\
                 3. **Never miss minimums** — late fees and credit damage compound fast\n\
                 4. **Refinance wisely** — lower rates can cut years off repayment\n\
                 5. **No new debt** — stop accumulating while paying off existing debt\n\n\
                 _Try: `calc loan 20000 18.9 5` to see credit-card debt cost._"
                .to_string(),
            "budget" => "**📋 Budgeting Tips**\n\n\
                 1. **Zero-based budget** — give every dollar a job\n\
                 2. **Set category limits** — use `budget <category> <amount>`\n\
                 3. **Check balance weekly** — use `balance` to see spend vs budget\n\
                 4. **Review monthly** — adjust budgets to reflect actual life\n\
                 5. **Include fun money** — rigid budgets fail; build in discretionary\n\n\
                 _Use `summary month` for a monthly breakdown._"
                .to_string(),
            _ => "**WIF Financial Tips** — pick a topic:\n\n\
                 ```\n\
                 tips saving     — spending reduction strategies\n\
                 tips investing  — growing wealth over time\n\
                 tips debt       — paying off debt efficiently\n\
                 tips budget     — budgeting best practices\n\
                 ```"
            .to_string(),
        }
    }

    fn dispatch(&self, text: &str) -> String {
        let arg = text.strip_prefix("@wif-agent").unwrap_or(text).trim();

        let parts: Vec<&str> = arg.split_whitespace().collect();
        let cmd = parts.first().copied().unwrap_or("help");

        match cmd {
            "add" => self.cmd_add(&parts[1..]),
            "budget" => self.cmd_budget(&parts[1..]),
            "summary" => self.cmd_summary(parts.get(1).copied().unwrap_or("all")),
            "balance" => self.cmd_balance(),
            "clear" => self.cmd_clear(parts.get(1).copied()),
            "calc" => self.cmd_calc(&parts[1..]),
            "tips" => self.cmd_tips(parts.get(1).copied().unwrap_or("")),
            "help" | "" => "**WIF — Finance Expert** 💹\n\
                 _Waldiez Intelligence Finance_\n\n\
                 ```\n\
                 add <amount> [category] [note]       log an expense\n\
                 budget <category> <amount>           set budget limit\n\
                 summary [today|week|month|all]       spending report\n\
                 balance                              budget vs actuals\n\
                 clear [category]                     reset expenses\n\
                 calc compound <p> <rate%> <years>    compound interest\n\
                 calc loan <p> <rate%> <years>        loan / mortgage\n\
                 calc roi <initial> <final>            return on invest\n\
                 calc tax <income> [rate%]             tax estimate\n\
                 tips [saving|investing|debt|budget]  financial advice\n\
                 help                                 this message\n\
                 ```"
            .to_string(),
            _ => format!("Unknown command: `{cmd}`. Type `help` for the full command list."),
        }
    }
}

// ── Actor implementation ────────────────────────────────────────────────────────

#[async_trait]
impl Actor for WifAgent {
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
                wactorz_mqtt::topics::spawn(&self.config.id),
                &serde_json::json!({
                    "agentId":     self.config.id,
                    "agentName":   self.config.name,
                    "agentType":   "financier",
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
        Ok(())
    }

    async fn handle_message(&mut self, message: Message) -> Result<()> {
        use wactorz_core::message::MessageType;

        let content = match &message.payload {
            MessageType::Text { content } => content.trim().to_string(),
            MessageType::Task { description, .. } => description.trim().to_string(),
            _ => return Ok(()),
        };

        let reply = self.dispatch(&content);
        self.reply(&reply);
        Ok(())
    }

    async fn on_heartbeat(&mut self) -> Result<()> {
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                wactorz_mqtt::topics::heartbeat(&self.config.id),
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
            .ok_or_else(|| anyhow::anyhow!("WifAgent already running"))?;

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
                        if let wactorz_core::message::MessageType::Command {
                            command: wactorz_core::message::ActorCommand::Stop,
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
