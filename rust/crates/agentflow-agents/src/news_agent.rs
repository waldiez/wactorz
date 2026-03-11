//! News headlines agent.
//!
//! [`NewsAgent`] fetches current headlines on demand.
//!
//! **Sources (no API key needed by default):**
//!
//! - [Hacker News](https://hacker-news.firebaseio.com) — tech/startup news (default)
//! - Any RSS/Atom feed URL via `NEWS_RSS_URL` env var
//!
//! ## Usage (via IO bar)
//!
//! ```text
//! @news-agent                 → top 5 HackerNews stories
//! @news-agent 10              → top 10 stories
//! @news-agent top             → same as above
//! @news-agent ask             → HN "Ask HN" stories
//! @news-agent show            → HN "Show HN" stories
//! @news-agent new             → newest HN stories
//! @news-agent jobs            → HN job postings
//! @news-agent help            → show usage
//! ```
//!
//! The agent does **not** poll; it only fetches when it receives a message.
//! It is stoppable and pausable — consumes no resources when idle.

use anyhow::Result;
use async_trait::async_trait;
use std::sync::Arc;
use tokio::sync::mpsc;

use agentflow_core::{
    Actor, ActorConfig, ActorMetrics, ActorState, EventPublisher, Message,
};

/// Default number of stories to show.
const DEFAULT_STORY_COUNT: usize = 5;
/// Maximum stories to fetch.
const MAX_STORY_COUNT: usize = 20;

const HTTP_TIMEOUT_SECS: u64 = 12;

/// Hacker News Firebase API base URL.
const HN_API: &str = "https://hacker-news.firebaseio.com/v0";

pub struct NewsAgent {
    config: ActorConfig,
    state: ActorState,
    metrics: Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher: Option<EventPublisher>,
    http: reqwest::Client,
}

impl NewsAgent {
    pub fn new(config: ActorConfig) -> Self {
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        let http = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(HTTP_TIMEOUT_SECS))
            .user_agent("AgentFlow-NewsAgent/1.0")
            .build()
            .unwrap_or_default();
        Self {
            config,
            state: ActorState::Initializing,
            metrics: Arc::new(ActorMetrics::new()),
            mailbox_tx: tx,
            mailbox_rx: Some(rx),
            publisher: None,
            http,
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

    /// Fetch story IDs from a HN endpoint (top/new/best/ask/show/job).
    async fn hn_story_ids(&self, feed: &str) -> Result<Vec<u64>> {
        let url = format!("{HN_API}/{feed}stories.json");
        let ids: Vec<u64> = self.http.get(&url).send().await?.json().await?;
        Ok(ids)
    }

    /// Fetch and format HN stories.
    async fn fetch_hn(&self, feed: &str, count: usize) -> Result<String> {
        let ids = self.hn_story_ids(feed).await?;
        let take = count.min(ids.len()).min(MAX_STORY_COUNT);

        // Fetch stories concurrently
        let mut handles = Vec::with_capacity(take);
        for &id in ids.iter().take(take) {
            let client = self.http.clone();
            handles.push(tokio::spawn(async move {
                let url = format!("{HN_API}/item/{id}.json");
                client
                    .get(&url)
                    .send()
                    .await
                    .ok()?
                    .json::<serde_json::Value>()
                    .await
                    .ok()
            }));
        }

        let mut lines = Vec::with_capacity(take);
        for (i, handle) in handles.into_iter().enumerate() {
            if let Ok(Some(item)) = handle.await {
                let title = item.get("title").and_then(|v| v.as_str()).unwrap_or("(no title)");
                let url   = item.get("url").and_then(|v| v.as_str()).unwrap_or("");
                let score = item.get("score").and_then(|v| v.as_i64()).unwrap_or(0);
                let hn_url = format!("https://news.ycombinator.com/item?id={}", ids[i]);

                let link = if url.is_empty() { hn_url.clone() } else { url.to_string() };
                lines.push(format!("{}. **[{title}]({link})** — ⬆ {score} · [HN]({hn_url})", i + 1));
            }
        }

        let feed_label = match feed {
            "top"  => "Top",
            "new"  => "Newest",
            "best" => "Best",
            "ask"  => "Ask HN",
            "show" => "Show HN",
            "job"  => "Jobs",
            other  => other,
        };

        if lines.is_empty() {
            return Ok(format!("No {feed_label} stories found right now."));
        }

        Ok(format!(
            "**Hacker News — {feed_label} Stories** (top {take})\n\n{}\n\n*Source: [news.ycombinator.com](https://news.ycombinator.com)*",
            lines.join("\n")
        ))
    }
}

#[async_trait]
impl Actor for NewsAgent {
    fn id(&self)       -> String              { self.config.id.clone() }
    fn name(&self)     -> &str                { &self.config.name }
    fn state(&self)    -> ActorState          { self.state.clone() }
    fn metrics(&self)  -> Arc<ActorMetrics>   { Arc::clone(&self.metrics) }
    fn mailbox(&self)  -> mpsc::Sender<Message> { self.mailbox_tx.clone() }
    fn is_protected(&self) -> bool            { self.config.protected }

    async fn on_start(&mut self) -> Result<()> {
        self.state = ActorState::Running;
        if let Some(pub_) = &self.publisher {
            pub_.publish(
                agentflow_mqtt::topics::spawn(&self.config.id),
                &serde_json::json!({
                    "agentId":   self.config.id,
                    "agentName": self.config.name,
                    "agentType": "data",
                    "timestampMs": Self::now_ms(),
                }),
            );
        }
        Ok(())
    }

    async fn handle_message(&mut self, message: Message) -> Result<()> {
        use agentflow_core::message::MessageType;

        let content = match &message.payload {
            MessageType::Text { content } => content.trim().to_string(),
            MessageType::Task { description, .. } => description.trim().to_string(),
            _ => return Ok(()),
        };

        // Strip @news-agent prefix if present
        let arg = content
            .strip_prefix("@news-agent")
            .unwrap_or(&content)
            .trim()
            .to_lowercase();

        let parts: Vec<&str> = arg.split_whitespace().collect();
        let first = parts.first().copied().unwrap_or("");

        // Determine feed and count
        let (feed, count) = match first {
            "" | "top"  => ("top",  parts.get(1).and_then(|s| s.parse().ok()).unwrap_or(DEFAULT_STORY_COUNT)),
            "new"       => ("new",  parts.get(1).and_then(|s| s.parse().ok()).unwrap_or(DEFAULT_STORY_COUNT)),
            "best"      => ("best", parts.get(1).and_then(|s| s.parse().ok()).unwrap_or(DEFAULT_STORY_COUNT)),
            "ask"       => ("ask",  parts.get(1).and_then(|s| s.parse().ok()).unwrap_or(DEFAULT_STORY_COUNT)),
            "show"      => ("show", parts.get(1).and_then(|s| s.parse().ok()).unwrap_or(DEFAULT_STORY_COUNT)),
            "jobs" | "job" => ("job", DEFAULT_STORY_COUNT),
            n if n.parse::<usize>().is_ok() => ("top", n.parse().unwrap_or(DEFAULT_STORY_COUNT)),
            "help" => {
                self.reply(
                    "**NewsAgent** — headlines via Hacker News (no API key needed)\n\n\
                     ```\n\
                     @news-agent              # top 5 stories\n\
                     @news-agent 10           # top 10 stories\n\
                     @news-agent new          # newest\n\
                     @news-agent best         # all-time best\n\
                     @news-agent ask          # Ask HN\n\
                     @news-agent show         # Show HN\n\
                     @news-agent jobs         # job postings\n\
                     @news-agent help         # this message\n\
                     ```"
                );
                return Ok(());
            }
            _ => ("top", DEFAULT_STORY_COUNT),
        };

        let count = count.min(MAX_STORY_COUNT);
        self.reply(&format!("📰 Fetching top {count} {feed} stories from Hacker News…"));

        match self.fetch_hn(feed, count).await {
            Ok(report)  => self.reply(&report),
            Err(e)      => self.reply(&format!("⚠ Could not fetch news: {e}")),
        }

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
            .ok_or_else(|| anyhow::anyhow!("NewsAgent already running"))?;
        let mut hb = tokio::time::interval(std::time::Duration::from_secs(
            self.config.heartbeat_interval_secs,
        ));
        hb.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            tokio::select! {
                biased;
                msg = rx.recv() => match msg {
                    None => break,
                    Some(m) => {
                        self.metrics.record_received();
                        if let agentflow_core::message::MessageType::Command {
                            command: agentflow_core::message::ActorCommand::Stop
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
