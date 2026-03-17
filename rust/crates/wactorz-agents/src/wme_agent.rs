//! Media-expert agent — **WME** (Waldiez Media Expert).
//!
//! Tracks your media consumption (movies, shows, books, podcasts), manages a
//! watchlist/readlist queue, computes binge/read-time estimates, and dispenses
//! expert media tips. No external APIs required.
//!
//! ## Usage (via IO bar)
//!
//! ```text
//! @wme-agent add movie "Inception" 9 [scifi]        → log a movie with rating
//! @wme-agent add show "Breaking Bad" s1e3 8.5 [drama]→ log a show episode
//! @wme-agent add book "Dune" 150 [scifi]            → log pages read
//! @wme-agent add podcast "Lex Fridman #420" 95 [tech]→ log podcast (mins)
//! @wme-agent queue add movie "Oppenheimer"          → add to watchlist
//! @wme-agent queue list                             → show queue
//! @wme-agent queue done "Oppenheimer" 8.5           → mark watched + rate
//! @wme-agent queue drop "Oppenheimer"               → remove from queue
//! @wme-agent stats                                  → full consumption stats
//! @wme-agent stats movie|show|book|podcast          → stats by type
//! @wme-agent top [n] [movie|show|book|podcast]      → top-rated entries
//! @wme-agent calc binge "Breaking Bad" 5 45         → binge N seasons × M min/ep
//! @wme-agent calc read 400 30                       → read N pages @ M pages/hr
//! @wme-agent tips streaming|reading|podcasts|focus  → media advice
//! @wme-agent help                                   → this message
//! ```
//!
//! All data is held in memory — restarting the agent resets the log.

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
struct MediaEntry {
    title: String,
    media_type: String, // "movie" | "show" | "book" | "podcast"
    rating: Option<f64>,
    genre: Option<String>,
    #[expect(dead_code)]
    progress: Option<String>, // "s1e3", "150 pages", "95 min", …
    #[expect(dead_code)]
    ts_ms: u64,
}

#[derive(Clone)]
struct QueueItem {
    title: String,
    media_type: String,
    #[expect(dead_code)]
    added_ms: u64,
}

// ── WmeAgent ───────────────────────────────────────────────────────────────────

pub struct WmeAgent {
    config: ActorConfig,
    state: ActorState,
    metrics: Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher: Option<EventPublisher>,
    log: Arc<Mutex<Vec<MediaEntry>>>,
    queue: Arc<Mutex<Vec<QueueItem>>>,
}

impl WmeAgent {
    pub fn new(config: ActorConfig) -> Self {
        let (tx, rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config,
            state: ActorState::Initializing,
            metrics: Arc::new(ActorMetrics::new()),
            mailbox_tx: tx,
            mailbox_rx: Some(rx),
            publisher: None,
            log: Arc::new(Mutex::new(Vec::new())),
            queue: Arc::new(Mutex::new(Vec::new())),
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
        // add <type> "<title>" [rating] [genre]
        if parts.len() < 2 {
            return "Usage: `add <movie|show|book|podcast> \"<title>\" [rating] [progress] [genre]`\n\n\
                    Examples:\n\
                    • `add movie \"Inception\" 9 scifi`\n\
                    • `add show \"Breaking Bad\" s1e3 8.5 drama`\n\
                    • `add book \"Dune\" 150 scifi`\n\
                    • `add podcast \"Lex Fridman #420\" 95 tech`".to_string();
        }

        let media_type = parts[0].to_lowercase();
        if !["movie", "show", "book", "podcast"].contains(&media_type.as_str()) {
            return format!(
                "Unknown type `{media_type}`. Use: `movie`, `show`, `book`, or `podcast`."
            );
        }

        // Title may be quoted or bare multi-word
        let rest = parts[1..].join(" ");
        let (title, remainder) = if let Some(stripped) = rest.strip_prefix('"') {
            if let Some(end) = stripped.find('"') {
                (
                    stripped[..end].to_string(),
                    stripped[end + 1..].trim().to_string(),
                )
            } else {
                (stripped.trim_matches('"').to_string(), String::new())
            }
        } else {
            let mut words = parts[1..].iter().peekable();
            let title_word = words.next().unwrap_or(&"").to_string();
            let rem: Vec<&str> = words.copied().collect();
            (title_word, rem.join(" "))
        };

        let rem_parts: Vec<&str> = remainder.split_whitespace().collect();

        // For shows: first extra token may be "s1e3" style progress
        let (progress, raw_rating, raw_genre): (Option<String>, Option<&str>, Option<&str>) =
            match media_type.as_str() {
                "show" => {
                    let prog: Option<&str> = rem_parts
                        .first()
                        .copied()
                        .filter(|s| s.to_lowercase().starts_with('s') && s.contains('e'));
                    let offset = if prog.is_some() { 1 } else { 0 };
                    let rat = rem_parts.get(offset).copied();
                    let gn = rem_parts.get(offset + 1).copied();
                    (prog.map(|s| s.to_string()), rat, gn)
                }
                "book" | "podcast" => {
                    let prog: Option<String> = rem_parts.first().copied().and_then(|s| {
                        s.parse::<u64>().ok().map(|n| match media_type.as_str() {
                            "book" => format!("{n} pages"),
                            _ => format!("{n} min"),
                        })
                    });
                    let gn = rem_parts.get(1).copied();
                    (prog, None, gn)
                }
                _ => {
                    // movie: optional rating then genre
                    let rat = rem_parts.first().copied();
                    let gn = rem_parts.get(1).copied();
                    (None, rat, gn)
                }
            };

        let rating: Option<f64> = raw_rating.and_then(|s: &str| {
            s.trim_end_matches('/')
                .parse::<f64>()
                .ok()
                .filter(|&v| (0.0..=10.0).contains(&v))
        });

        let entry = MediaEntry {
            title: title.clone(),
            media_type: media_type.clone(),
            rating,
            genre: raw_genre.map(|s| s.to_string()),
            progress: progress.clone(),
            ts_ms: Self::now_ms(),
        };
        self.log.lock().unwrap().push(entry);

        // Remove from queue if present
        let mut queue = self.queue.lock().unwrap();
        let before = queue.len();
        queue.retain(|q| q.title.to_lowercase() != title.to_lowercase());
        let dequeued = before != queue.len();

        let rating_str = rating.map(|r| format!(" ⭐ {r:.1}/10")).unwrap_or_default();
        let progress_str = progress.map(|p| format!(" _{p}_")).unwrap_or_default();
        let genre_str2 = raw_genre.map(|g| format!(" `{g}`")).unwrap_or_default();
        let dequeued_note = if dequeued {
            "\n✅ Removed from queue."
        } else {
            ""
        };

        let icon = match media_type.as_str() {
            "movie" => "🎬",
            "show" => "📺",
            "book" => "📚",
            "podcast" => "🎙",
            _ => "🎭",
        };
        format!("{icon} Logged **{title}**{rating_str}{progress_str}{genre_str2}{dequeued_note}")
    }

    fn cmd_queue(&self, parts: &[&str]) -> String {
        match parts.first().copied().unwrap_or("list") {
            "add" => {
                if parts.len() < 3 {
                    return "Usage: `queue add <movie|show|book|podcast> \"<title>\"`".to_string();
                }
                let media_type = parts[1].to_lowercase();
                let title = parts[2..].join(" ").trim_matches('"').to_string();
                let mut queue = self.queue.lock().unwrap();
                if queue
                    .iter()
                    .any(|q| q.title.to_lowercase() == title.to_lowercase())
                {
                    return format!("📋 **{title}** is already in the queue.");
                }
                queue.push(QueueItem {
                    title: title.clone(),
                    media_type: media_type.clone(),
                    added_ms: Self::now_ms(),
                });
                let icon = match media_type.as_str() {
                    "movie" => "🎬",
                    "show" => "📺",
                    "book" => "📚",
                    "podcast" => "🎙",
                    _ => "🎭",
                };
                format!(
                    "{icon} Added **{title}** to queue ({} items total)",
                    queue.len()
                )
            }

            "list" | "ls" => {
                let queue = self.queue.lock().unwrap();
                if queue.is_empty() {
                    return "📭 Queue is empty.\n\nTry: `queue add movie \"Oppenheimer\"`"
                        .to_string();
                }
                let mut by_type: HashMap<&str, Vec<&str>> = HashMap::new();
                for q in queue.iter() {
                    by_type
                        .entry(q.media_type.as_str())
                        .or_default()
                        .push(q.title.as_str());
                }
                let mut sections = Vec::new();
                for (t, titles) in &by_type {
                    let icon = match *t {
                        "movie" => "🎬",
                        "show" => "📺",
                        "book" => "📚",
                        "podcast" => "🎙",
                        _ => "🎭",
                    };
                    let rows: Vec<String> = titles.iter().map(|t| format!("  • {t}")).collect();
                    sections.push(format!(
                        "{icon} **{}**\n{}",
                        Self::capitalize(t),
                        rows.join("\n")
                    ));
                }
                sections.sort();
                format!(
                    "**📋 Queue ({} items)**\n\n{}",
                    queue.len(),
                    sections.join("\n\n")
                )
            }

            "done" | "watched" | "read" | "finished" => {
                if parts.len() < 2 {
                    return "Usage: `queue done \"<title>\" [rating]`".to_string();
                }
                let title_parts = if parts.len() > 2 {
                    parts[1..parts.len() - 1].join(" ")
                } else {
                    parts[1..].join(" ")
                };
                let title = title_parts.trim_matches('"').to_string();
                let rating: Option<f64> = parts
                    .last()
                    .and_then(|s| s.parse::<f64>().ok())
                    .filter(|&v| (0.0..=10.0).contains(&v));

                let mut queue = self.queue.lock().unwrap();
                let before = queue.len();
                let removed_type = queue
                    .iter()
                    .find(|q| q.title.to_lowercase() == title.to_lowercase())
                    .map(|q| q.media_type.clone());
                queue.retain(|q| q.title.to_lowercase() != title.to_lowercase());

                if queue.len() == before {
                    return format!("❓ **{title}** not found in queue.");
                }
                drop(queue);

                let media_type = removed_type.unwrap_or_else(|| "movie".to_string());
                let entry = MediaEntry {
                    title: title.clone(),
                    media_type: media_type.clone(),
                    rating,
                    genre: None,
                    progress: None,
                    ts_ms: Self::now_ms(),
                };
                self.log.lock().unwrap().push(entry);

                let rating_str = rating.map(|r| format!(" ⭐ {r:.1}/10")).unwrap_or_default();
                let icon = match media_type.as_str() {
                    "movie" => "🎬",
                    "show" => "📺",
                    "book" => "📚",
                    "podcast" => "🎙",
                    _ => "🎭",
                };
                format!("{icon} Marked **{title}** as done{rating_str} and removed from queue.")
            }

            "drop" | "remove" | "rm" => {
                if parts.len() < 2 {
                    return "Usage: `queue drop \"<title>\"`".to_string();
                }
                let title = parts[1..].join(" ").trim_matches('"').to_string();
                let mut queue = self.queue.lock().unwrap();
                let before = queue.len();
                queue.retain(|q| q.title.to_lowercase() != title.to_lowercase());
                if queue.len() == before {
                    format!("❓ **{title}** not found in queue.")
                } else {
                    format!("🗑 Dropped **{title}** from queue.")
                }
            }

            sub => {
                format!("Unknown queue sub-command `{sub}`. Use: `add`, `list`, `done`, `drop`.")
            }
        }
    }

    fn cmd_stats(&self, filter: Option<&str>) -> String {
        let log = self.log.lock().unwrap();
        if log.is_empty() {
            return "📭 Nothing logged yet.\n\nTry: `add movie \"Inception\" 9 scifi`".to_string();
        }

        let entries: Vec<&MediaEntry> = match filter {
            Some(t) => log.iter().filter(|e| e.media_type == t).collect(),
            None => log.iter().collect(),
        };

        if entries.is_empty() {
            return format!("📭 No `{}` entries yet.", filter.unwrap_or(""));
        }

        let mut by_type: HashMap<&str, (usize, f64, u32)> = HashMap::new(); // count, rating_sum, rated_count
        for e in &entries {
            let rec = by_type.entry(e.media_type.as_str()).or_insert((0, 0.0, 0));
            rec.0 += 1;
            if let Some(r) = e.rating {
                rec.1 += r;
                rec.2 += 1;
            }
        }

        let total = entries.len();
        let total_rated: u32 = by_type.values().map(|r| r.2).sum();
        let total_rating: f64 = by_type.values().map(|r| r.1).sum();
        let avg_overall = if total_rated > 0 {
            total_rating / total_rated as f64
        } else {
            0.0
        };

        let mut rows = Vec::new();
        let mut type_order = vec!["movie", "show", "book", "podcast"];
        type_order.retain(|t| by_type.contains_key(*t));
        for t in type_order {
            if let Some(&(count, rs, rc)) = by_type.get(t) {
                let avg = if rc > 0 {
                    format!("avg ⭐ {:.1}", rs / rc as f64)
                } else {
                    "unrated".to_string()
                };
                let icon = match t {
                    "movie" => "🎬",
                    "show" => "📺",
                    "book" => "📚",
                    "podcast" => "🎙",
                    _ => "🎭",
                };
                rows.push(format!(
                    "  {icon} **{}**: {count} entries — {avg}",
                    Self::capitalize(t)
                ));
            }
        }

        let header = match filter {
            Some(t) => format!("**📊 Media Stats — {}**", Self::capitalize(t)),
            None => "**📊 Media Stats — All Time**".to_string(),
        };
        let avg_line = if total_rated > 0 {
            format!("\n\n**Overall avg rating**: ⭐ {avg_overall:.1}/10 ({total_rated} rated)")
        } else {
            String::new()
        };

        format!(
            "{header}\n\n{}\n\n**Total entries**: {total}{avg_line}",
            rows.join("\n")
        )
    }

    fn cmd_top(&self, parts: &[&str]) -> String {
        let n: usize = parts
            .first()
            .and_then(|s| s.parse().ok())
            .unwrap_or(5)
            .min(20);
        let filter = parts
            .get(
                if parts
                    .first()
                    .and_then(|s| s.parse::<usize>().ok())
                    .is_some()
                {
                    1
                } else {
                    0
                },
            )
            .copied();

        let log = self.log.lock().unwrap();
        let mut rated: Vec<&MediaEntry> = log
            .iter()
            .filter(|e| e.rating.is_some())
            .filter(|e| filter.map(|t| e.media_type == t).unwrap_or(true))
            .collect();

        if rated.is_empty() {
            return "📭 No rated entries yet.\n\nTry: `add movie \"Inception\" 9 scifi`"
                .to_string();
        }

        rated.sort_by(|a, b| {
            b.rating
                .unwrap_or(0.0)
                .partial_cmp(&a.rating.unwrap_or(0.0))
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        let type_label = filter
            .map(|t| format!(" — {}", Self::capitalize(t)))
            .unwrap_or_default();
        let rows: Vec<String> = rated
            .iter()
            .take(n)
            .enumerate()
            .map(|(i, e)| {
                let icon = match e.media_type.as_str() {
                    "movie" => "🎬",
                    "show" => "📺",
                    "book" => "📚",
                    "podcast" => "🎙",
                    _ => "🎭",
                };
                let genre = e
                    .genre
                    .as_deref()
                    .map(|g| format!(" `{g}`"))
                    .unwrap_or_default();
                format!(
                    "  {}. {icon} **{}** ⭐ {:.1}{genre}",
                    i + 1,
                    e.title,
                    e.rating.unwrap_or(0.0)
                )
            })
            .collect();

        format!("**🏆 Top {n}{type_label}**\n\n{}", rows.join("\n"))
    }

    fn cmd_calc(&self, parts: &[&str]) -> String {
        match parts.first().copied().unwrap_or("") {
            "binge" => {
                // binge <title_or_N_episodes> <seasons> <mins_per_ep>
                // Simplified: binge <num_episodes> <mins_per_ep>
                //  OR:        binge <seasons> <eps_per_season> <mins_per_ep>
                if parts.len() < 3 {
                    return "Usage: `calc binge <episodes> <mins_per_ep>`\n\
                             OR:    `calc binge <seasons> <eps_per_season> <mins_per_ep>`\n\n\
                             Examples:\n\
                             • `calc binge 62 47`  → 62 episodes × 47 min\n\
                             • `calc binge 5 13 45` → 5 seasons × 13 eps × 45 min"
                        .to_string();
                }

                let (total_eps, mins): (u64, u64) = if parts.len() >= 4 {
                    let seasons: u64 = parts[1].parse().unwrap_or(0);
                    let eps: u64 = parts[2].parse().unwrap_or(0);
                    let mins: u64 = parts[3].parse().unwrap_or(0);
                    (seasons * eps, mins)
                } else {
                    let eps: u64 = parts[1].parse().unwrap_or(0);
                    let mins: u64 = parts[2].parse().unwrap_or(0);
                    (eps, mins)
                };

                if total_eps == 0 || mins == 0 {
                    return "Episodes and minutes per episode must be positive.".to_string();
                }

                let total_mins = total_eps * mins;
                let hours = total_mins / 60;
                let rem_mins = total_mins % 60;
                let days_8h = (total_mins as f64 / 480.0).ceil() as u64; // 8 h/day
                let days_4h = (total_mins as f64 / 240.0).ceil() as u64; // 4 h/day

                format!(
                    "**📺 Binge Calculator**\n\n\
                     Episodes   : {total_eps} × {mins} min\n\
                     Total time : **{hours}h {rem_mins}m** ({total_mins} min)\n\n\
                     At 4 h/day → **{days_4h} days**\n\
                     At 8 h/day → **{days_8h} days**\n\n\
                     _Popcorn budget not included._"
                )
            }

            "read" => {
                // read <pages> <pages_per_hour>
                if parts.len() < 3 {
                    return "Usage: `calc read <pages> <pages_per_hour>`\n\n\
                             Example: `calc read 400 30`  → 400 pages @ 30 pp/h"
                        .to_string();
                }
                let pages: u64 = parts[1].parse().unwrap_or(0);
                let speed: u64 = parts[2].parse().unwrap_or(0);
                if pages == 0 || speed == 0 {
                    return "Pages and speed must be positive.".to_string();
                }

                let total_mins = pages * 60 / speed;
                let hours = total_mins / 60;
                let rem_mins = total_mins % 60;
                let sessions_30 = (total_mins as f64 / 30.0).ceil() as u64;
                let sessions_60 = (total_mins as f64 / 60.0).ceil() as u64;

                format!(
                    "**📚 Reading Calculator**\n\n\
                     Pages : {pages} @ {speed} pp/h\n\
                     Time  : **{hours}h {rem_mins}m** ({total_mins} min)\n\n\
                     In 30-min sessions → **{sessions_30} sessions**\n\
                     In 60-min sessions → **{sessions_60} sessions**"
                )
            }

            "listen" => {
                // listen <episodes> <mins_each>
                if parts.len() < 3 {
                    return "Usage: `calc listen <episodes> <mins_per_episode>`\n\n\
                             Example: `calc listen 10 45`  → 10 episodes × 45 min"
                        .to_string();
                }
                let eps: u64 = parts[1].parse().unwrap_or(0);
                let mins: u64 = parts[2].parse().unwrap_or(0);
                if eps == 0 || mins == 0 {
                    return "Episodes and duration must be positive.".to_string();
                }

                let total = eps * mins;
                let hours = total / 60;
                let rem = total % 60;
                let at_1_5x = total * 2 / 3;
                let h15 = at_1_5x / 60;
                let r15 = at_1_5x % 60;

                format!(
                    "**🎙 Podcast Queue Calculator**\n\n\
                     {eps} episodes × {mins} min\n\
                     Normal speed : **{hours}h {rem}m**\n\
                     At 1.5×      : **{h15}h {r15}m**"
                )
            }

            _ => "**calc** subcommands:\n\n\
                 ```\n\
                 calc binge <eps> <mins/ep>                   binge time\n\
                 calc binge <seasons> <eps/season> <mins/ep>  binge time\n\
                 calc read <pages> <pages/hr>                 reading time\n\
                 calc listen <episodes> <mins/ep>             podcast queue time\n\
                 ```"
            .to_string(),
        }
    }

    fn cmd_tips(&self, topic: &str) -> String {
        match topic {
            "streaming" | "stream" => {
                "**📺 Streaming Tips**\n\n\
                 1. **Watch before cancelling** — use free trials strategically (one at a time)\n\
                 2. **Download for offline** — save mobile data on commutes\n\
                 3. **Avoid spoilers** — use `queue add` to park titles; watch in order\n\
                 4. **Skip intros/recaps** — reclaim ~5 min per episode on long binges\n\
                 5. **1.25× speed** — comfortable for most content, saves ~20% of time\n\n\
                 _Use `calc binge` to plan how many days a series will take._".to_string()
            }
            "reading" | "read" | "books" => {
                "**📚 Reading Tips**\n\n\
                 1. **Read daily** — even 20 pages/day = ~15 books a year\n\
                 2. **Two-book rule** — keep a light read alongside a dense one\n\
                 3. **No phone in bed** — replace doom-scrolling with 30 min of reading\n\
                 4. **Take notes** — highlight + jot key ideas; retention increases 3×\n\
                 5. **DNF guilt-free** — abandon books you're not enjoying after 50 pages\n\n\
                 _Use `calc read` to see how long your next book will take._".to_string()
            }
            "podcasts" | "podcast" => {
                "**🎙 Podcast Tips**\n\n\
                 1. **Queue by theme** — batch similar episodes for deeper absorption\n\
                 2. **Speed up gradually** — 1.0× → 1.25× → 1.5×; brain adapts in a week\n\
                 3. **Active listening** — note one insight per episode before moving on\n\
                 4. **Commute pairing** — assign podcasts to routine tasks (gym, dishes)\n\
                 5. **Trim the backlog** — unsubscribe from shows you skip for 3+ episodes\n\n\
                 _Use `calc listen` to size your podcast backlog._".to_string()
            }
            "focus" | "productivity" => {
                "**🎯 Focus & Deep Work Media Tips**\n\n\
                 1. **Instrumental only** — lyrics activate language centres and fragment focus\n\
                 2. **Lo-fi / ambient** — consistent low-stimulation audio masks distractions\n\
                 3. **Volume at 50–60%** — loud audio elevates cortisol over long sessions\n\
                 4. **No queue anxiety** — log content in `queue` and forget it; reclaim mental RAM\n\
                 5. **Media fasts** — 1 day/week screen-free improves creative output measurably\n\n\
                 _Try: `queue add podcast \"Deep Work recap\"` to park ideas for later._".to_string()
            }
            "movies" | "film" | "cinema" => {
                "**🎬 Film Tips**\n\n\
                 1. **Rate right after** — memory of emotional impact fades within hours\n\
                 2. **Director filmographies** — watch all films by one director back-to-back\n\
                 3. **Criterion / A24** — reliable quality signals for arthouse discovery\n\
                 4. **First 10 minutes rule** — if it doesn't hook you, it rarely improves\n\
                 5. **Discuss afterwards** — verbalising a film doubles long-term retention\n\n\
                 _Use `top 10 movie` to review your highest-rated films._".to_string()
            }
            _ => {
                "**WME Media Tips** — pick a topic:\n\n\
                 ```\n\
                 tips streaming    — streaming platform strategy\n\
                 tips reading      — books and reading habits\n\
                 tips podcasts     — podcast consumption\n\
                 tips focus        — media for deep work\n\
                 tips movies       — film watching habits\n\
                 ```".to_string()
            }
        }
    }

    fn capitalize(s: &str) -> String {
        let mut c = s.chars();
        match c.next() {
            None => String::new(),
            Some(f) => f.to_uppercase().collect::<String>() + c.as_str(),
        }
    }

    fn dispatch(&self, text: &str) -> String {
        let arg = text.strip_prefix("@wme-agent").unwrap_or(text).trim();

        let parts: Vec<&str> = arg.split_whitespace().collect();
        let cmd = parts.first().copied().unwrap_or("help");

        match cmd {
            "add" => self.cmd_add(&parts[1..]),
            "queue" => self.cmd_queue(&parts[1..]),
            "stats" => self.cmd_stats(parts.get(1).copied()),
            "top" => self.cmd_top(&parts[1..]),
            "calc" => self.cmd_calc(&parts[1..]),
            "tips" => self.cmd_tips(parts.get(1).copied().unwrap_or("")),
            "help" | "" => "**WME — Media Expert** 🎬\n\
                 _Waldiez Media Expert_\n\n\
                 ```\n\
                 add movie|show|book|podcast \"<title>\" …  log media\n\
                 queue add <type> \"<title>\"               add to watchlist\n\
                 queue list                               show queue\n\
                 queue done \"<title>\" [rating]            mark finished\n\
                 queue drop \"<title>\"                     remove from queue\n\
                 stats [movie|show|book|podcast]         consumption stats\n\
                 top [n] [movie|show|book|podcast]       highest rated\n\
                 calc binge <eps> <mins/ep>              binge time\n\
                 calc read <pages> <pp/hr>               reading time\n\
                 calc listen <eps> <mins/ep>             podcast queue time\n\
                 tips [streaming|reading|podcasts|focus] media advice\n\
                 help                                    this message\n\
                 ```"
            .to_string(),
            _ => format!("Unknown command: `{cmd}`. Type `help` for the full command list."),
        }
    }
}

// ── Actor implementation ────────────────────────────────────────────────────────

#[async_trait]
impl Actor for WmeAgent {
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
                    "agentType":   "media",
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
            .ok_or_else(|| anyhow::anyhow!("WmeAgent already running"))?;

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
