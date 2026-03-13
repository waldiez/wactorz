//! Sound-expert agent — **WIS** (Waldiez Intelligence Sound).
//!
//! Tracks music listening (songs, albums, artists), provides built-in music
//! theory tools (chords, scales, BPM analysis, intervals), and dispenses
//! expert audio tips. No external APIs required.
//!
//! NATO node: **whiskey** (W → WIS)
//!
//! ## Usage (via IO bar)
//!
//! ```text
//! @wis-agent add song "Clair de Lune" Debussy 10 classical
//! @wis-agent add album "Kind of Blue" "Miles Davis" 9.5 jazz
//! @wis-agent add artist Bach 10 classical
//! @wis-agent stats [song|album|artist|genre]
//! @wis-agent top [n] [song|album|artist]
//! @wis-agent theory chord Am              → A C E (minor triad)
//! @wis-agent theory chord Cmaj7           → C E G B
//! @wis-agent theory scale "C major"       → C D E F G A B
//! @wis-agent theory scale "A dorian"      → A B C D E F# G
//! @wis-agent theory bpm 128               → Allegro · beat grid
//! @wis-agent theory interval C G          → Perfect 5th (7 semitones)
//! @wis-agent tips listening|mixing|mastering|practice|gear
//! @wis-agent help
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

use agentflow_core::{Actor, ActorConfig, ActorMetrics, ActorState, EventPublisher, Message};

// ── Music theory constants ──────────────────────────────────────────────────────

const NOTE_NAMES: [&str; 12] = [
    "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B",
];

const INTERVAL_NAMES: [&str; 13] = [
    "Unison (P1)",
    "Minor 2nd (m2)",
    "Major 2nd (M2)",
    "Minor 3rd (m3)",
    "Major 3rd (M3)",
    "Perfect 4th (P4)",
    "Tritone (A4/d5)",
    "Perfect 5th (P5)",
    "Minor 6th (m6)",
    "Major 6th (M6)",
    "Minor 7th (m7)",
    "Major 7th (M7)",
    "Octave (P8)",
];

fn note_index(s: &str) -> Option<usize> {
    match s
        .to_uppercase()
        .replace("♭", "B")
        .replace("♯", "#")
        .as_str()
    {
        "C" | "B#" => Some(0),
        "C#" | "DB" => Some(1),
        "D" => Some(2),
        "D#" | "EB" => Some(3),
        "E" | "FB" => Some(4),
        "F" | "E#" => Some(5),
        "F#" | "GB" => Some(6),
        "G" => Some(7),
        "G#" | "AB" => Some(8),
        "A" => Some(9),
        "A#" | "BB" => Some(10),
        "B" | "CB" => Some(11),
        _ => None,
    }
}

/// Parse a note token like "C", "C#", "Db", "F#".
/// Returns (note_index, remaining_suffix).
fn parse_root(token: &str) -> Option<(usize, &str)> {
    // Try 2-char root first (C#, Db, …)
    if token.len() >= 2 {
        let two = &token[..2];
        if let Some(idx) = note_index(two) {
            return Some((idx, &token[2..]));
        }
    }
    // Fall back to 1-char root
    if !token.is_empty()
        && let Some(idx) = note_index(&token[..1])
    {
        return Some((idx, &token[1..]));
    }
    None
}

fn build_chord(root: usize, quality: &str) -> Option<(&'static str, Vec<usize>)> {
    let q = quality.to_lowercase();
    let (name, intervals): (&'static str, &[usize]) = match q.trim_matches(|c: char| c == ' ') {
        "" | "maj" | "major" => ("major", &[0, 4, 7]),
        "m" | "min" | "minor" | "-" => ("minor", &[0, 3, 7]),
        "dim" | "°" | "diminished" => ("diminished", &[0, 3, 6]),
        "aug" | "+" | "augmented" => ("augmented", &[0, 4, 8]),
        "7" | "dom7" => ("dominant 7th", &[0, 4, 7, 10]),
        "maj7" | "m7" if q == "maj7" => ("major 7th", &[0, 4, 7, 11]),
        "m7" | "min7" => ("minor 7th", &[0, 3, 7, 10]),
        "dim7" | "°7" => ("diminished 7th", &[0, 3, 6, 9]),
        "sus2" => ("sus2", &[0, 2, 7]),
        "sus4" => ("sus4", &[0, 5, 7]),
        "add9" => ("add9", &[0, 4, 7, 14]),
        "6" => ("major 6th", &[0, 4, 7, 9]),
        "m6" => ("minor 6th", &[0, 3, 7, 9]),
        _ => return None,
    };
    let notes: Vec<usize> = intervals.iter().map(|&i| (root + i) % 12).collect();
    Some((name, notes))
}

fn build_scale(root: usize, mode: &str) -> Option<(&'static str, Vec<usize>)> {
    let m = mode.to_lowercase();
    let (name, intervals): (&'static str, &[usize]) = match m.trim() {
        "major" | "ionian" | "maj" => ("Major (Ionian)", &[0, 2, 4, 5, 7, 9, 11]),
        "minor" | "natural minor" | "aeolian" | "min" => {
            ("Natural Minor (Aeolian)", &[0, 2, 3, 5, 7, 8, 10])
        }
        "dorian" => ("Dorian", &[0, 2, 3, 5, 7, 9, 10]),
        "phrygian" => ("Phrygian", &[0, 1, 3, 5, 7, 8, 10]),
        "lydian" => ("Lydian", &[0, 2, 4, 6, 7, 9, 11]),
        "mixolydian" | "mixo" => ("Mixolydian", &[0, 2, 4, 5, 7, 9, 10]),
        "locrian" => ("Locrian", &[0, 1, 3, 5, 6, 8, 10]),
        "harmonic minor" | "harm" => ("Harmonic Minor", &[0, 2, 3, 5, 7, 8, 11]),
        "melodic minor" | "mel" => ("Melodic Minor (asc)", &[0, 2, 3, 5, 7, 9, 11]),
        "pentatonic major" | "pent major" | "pent maj" => ("Pentatonic Major", &[0, 2, 4, 7, 9]),
        "pentatonic minor" | "pent minor" | "pent min" => ("Pentatonic Minor", &[0, 3, 5, 7, 10]),
        "blues" => ("Blues", &[0, 3, 5, 6, 7, 10]),
        _ => return None,
    };
    let notes: Vec<usize> = intervals.iter().map(|&i| (root + i) % 12).collect();
    Some((name, notes))
}

fn bpm_tempo_name(bpm: u32) -> &'static str {
    match bpm {
        0..=59 => "Largo (very slow)",
        60..=65 => "Larghetto",
        66..=75 => "Adagio (slow)",
        76..=107 => "Andante (walking pace)",
        108..=119 => "Moderato",
        120..=155 => "Allegro (fast)",
        156..=175 => "Vivace (lively)",
        176..=199 => "Presto (very fast)",
        _ => "Prestissimo (extremely fast)",
    }
}

// ── Data model ──────────────────────────────────────────────────────────────────

#[derive(Clone)]
struct MusicEntry {
    title: String,
    entry_type: String, // "song" | "album" | "artist"
    artist: Option<String>,
    rating: Option<f64>,
    genre: Option<String>,
    ts_ms: u64,
}

// ── WisAgent ────────────────────────────────────────────────────────────────────

pub struct WisAgent {
    config: ActorConfig,
    state: ActorState,
    metrics: Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    publisher: Option<EventPublisher>,
    log: Arc<Mutex<Vec<MusicEntry>>>,
}

impl WisAgent {
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

    // ── Command handlers ────────────────────────────────────────────────────────

    fn cmd_add(&self, parts: &[&str]) -> String {
        // add <song|album|artist> "<title>" [artist] [rating] [genre]
        if parts.len() < 2 {
            return "Usage: `add <song|album|artist> \"<title>\" [artist] [rating] [genre]`\n\n\
                    Examples:\n\
                    • `add song \"Clair de Lune\" Debussy 10 classical`\n\
                    • `add album \"Kind of Blue\" \"Miles Davis\" 9.5 jazz`\n\
                    • `add artist Bach 10 baroque`"
                .to_string();
        }

        let entry_type = parts[0].to_lowercase();
        if !["song", "album", "artist"].contains(&entry_type.as_str()) {
            return format!("Unknown type `{entry_type}`. Use: `song`, `album`, or `artist`.");
        }

        // Parse (possibly quoted) title
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
            let mut it = parts[1..].iter();
            let t = it.next().unwrap_or(&"").to_string();
            let r: Vec<&str> = it.copied().collect();
            (t, r.join(" "))
        };

        let rem: Vec<&str> = remainder.split_whitespace().collect();

        // For song/album: [artist] [rating] [genre]
        // For artist:     [rating] [genre]
        let (artist, raw_rating, raw_genre): (Option<String>, Option<&str>, Option<&str>) =
            match entry_type.as_str() {
                "artist" => {
                    let rat = rem.first().copied();
                    let gn = rem.get(1).copied();
                    (None, rat, gn)
                }
                _ => {
                    // First rem token: artist if it doesn't parse as a rating
                    let first = rem.first().copied();
                    let is_rating = first
                        .map(|s: &str| s.parse::<f64>().is_ok())
                        .unwrap_or(false);
                    if is_rating {
                        (None, first, rem.get(1).copied())
                    } else {
                        (
                            first.map(|s| s.to_string()),
                            rem.get(1).copied(),
                            rem.get(2).copied(),
                        )
                    }
                }
            };

        let rating: Option<f64> = raw_rating.and_then(|s: &str| {
            s.trim_end_matches('/')
                .parse::<f64>()
                .ok()
                .filter(|&v| (0.0..=10.0).contains(&v))
        });

        self.log.lock().unwrap().push(MusicEntry {
            title: title.clone(),
            entry_type: entry_type.clone(),
            artist: artist.clone(),
            rating,
            genre: raw_genre.map(|s| s.to_string()),
            ts_ms: Self::now_ms(),
        });

        let icon = match entry_type.as_str() {
            "song" => "🎵",
            "album" => "💿",
            _ => "🎤",
        };
        let rating_str = rating.map(|r| format!(" ⭐ {r:.1}/10")).unwrap_or_default();
        let artist_str = artist.map(|a| format!(" — _{a}_")).unwrap_or_default();
        let genre_str = raw_genre.map(|g| format!(" `{g}`")).unwrap_or_default();
        format!("{icon} Logged **{title}**{artist_str}{rating_str}{genre_str}")
    }

    fn cmd_stats(&self, filter: Option<&str>) -> String {
        let log = self.log.lock().unwrap();
        if log.is_empty() {
            return "📭 Nothing logged yet.\n\nTry: `add song \"Clair de Lune\" Debussy 10 classical`".to_string();
        }

        let entries: Vec<&MusicEntry> = match filter {
            Some(t) if ["song", "album", "artist"].contains(&t) => {
                log.iter().filter(|e| e.entry_type == t).collect()
            }
            Some(g) => {
                // treat as genre filter
                log.iter()
                    .filter(|e| {
                        e.genre
                            .as_deref()
                            .map(|eg| eg.eq_ignore_ascii_case(g))
                            .unwrap_or(false)
                    })
                    .collect()
            }
            None => log.iter().collect(),
        };

        if entries.is_empty() {
            return format!("📭 No entries for `{}`.", filter.unwrap_or(""));
        }

        let mut by_type: HashMap<&str, (usize, f64, u32)> = HashMap::new();
        for e in &entries {
            let rec = by_type.entry(e.entry_type.as_str()).or_insert((0, 0.0, 0));
            rec.0 += 1;
            if let Some(r) = e.rating {
                rec.1 += r;
                rec.2 += 1;
            }
        }

        let total = entries.len();
        let total_rated: u32 = by_type.values().map(|r| r.2).sum();
        let total_rating: f64 = by_type.values().map(|r| r.1).sum();
        let avg = if total_rated > 0 {
            total_rating / total_rated as f64
        } else {
            0.0
        };

        let mut rows = Vec::new();
        for t in &["song", "album", "artist"] {
            if let Some(&(count, rs, rc)) = by_type.get(*t) {
                let avg_t = if rc > 0 {
                    format!("avg ⭐ {:.1}", rs / rc as f64)
                } else {
                    "unrated".to_string()
                };
                let icon = match *t {
                    "song" => "🎵",
                    "album" => "💿",
                    _ => "🎤",
                };
                rows.push(format!(
                    "  {icon} **{}s**: {count} — {avg_t}",
                    Self::capitalize(t)
                ));
            }
        }

        let header = match filter {
            Some(f) => format!("**🎧 Music Stats — {}**", Self::capitalize(f)),
            None => "**🎧 Music Stats — All Time**".to_string(),
        };
        let avg_line = if total_rated > 0 {
            format!("\n\n**Overall avg**: ⭐ {avg:.1}/10 ({total_rated} rated)")
        } else {
            String::new()
        };

        format!(
            "{header}\n\n{}\n\n**Total**: {total} entries{avg_line}",
            rows.join("\n")
        )
    }

    fn cmd_top(&self, parts: &[&str]) -> String {
        let n: usize = parts
            .first()
            .and_then(|s| s.parse().ok())
            .unwrap_or(5)
            .min(20);
        let type_offset = if parts
            .first()
            .and_then(|s| s.parse::<usize>().ok())
            .is_some()
        {
            1
        } else {
            0
        };
        let filter = parts.get(type_offset).copied();

        let log = self.log.lock().unwrap();
        let mut rated: Vec<&MusicEntry> = log
            .iter()
            .filter(|e| e.rating.is_some())
            .filter(|e| filter.map(|t| e.entry_type == t).unwrap_or(true))
            .collect();

        if rated.is_empty() {
            return "📭 No rated entries yet.\n\nTry: `add album \"Kind of Blue\" \"Miles Davis\" 9.5 jazz`".to_string();
        }

        rated.sort_by(|a, b| {
            b.rating
                .unwrap_or(0.0)
                .partial_cmp(&a.rating.unwrap_or(0.0))
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        let type_label = filter
            .map(|t| format!(" — {}s", Self::capitalize(t)))
            .unwrap_or_default();
        let rows: Vec<String> = rated
            .iter()
            .take(n)
            .enumerate()
            .map(|(i, e)| {
                let icon = match e.entry_type.as_str() {
                    "song" => "🎵",
                    "album" => "💿",
                    _ => "🎤",
                };
                let artist = e
                    .artist
                    .as_deref()
                    .map(|a| format!(" — _{a}_"))
                    .unwrap_or_default();
                let genre = e
                    .genre
                    .as_deref()
                    .map(|g| format!(" `{g}`"))
                    .unwrap_or_default();
                format!(
                    "  {}. {icon} **{}**{artist} ⭐ {:.1}{genre}",
                    i + 1,
                    e.title,
                    e.rating.unwrap_or(0.0)
                )
            })
            .collect();

        format!("**🏆 Top {n}{type_label}**\n\n{}", rows.join("\n"))
    }

    // ── Music theory ────────────────────────────────────────────────────────────

    fn cmd_theory(&self, parts: &[&str]) -> String {
        match parts.first().copied().unwrap_or("") {
            "chord" => {
                if parts.len() < 2 {
                    return "Usage: `theory chord <root>[quality]`\n\n\
                             Examples:\n\
                             • `theory chord Am`    → minor triad\n\
                             • `theory chord Cmaj7` → major 7th\n\
                             • `theory chord F#dim` → diminished\n\n\
                             Qualities: (blank)=major  m=minor  dim  aug  7  maj7  m7  dim7  sus2  sus4  add9  6  m6".to_string();
                }

                // Parts 1..N form the chord token (may be space-separated like "C maj7")
                let chord_str = parts[1..].join("");
                let (root_idx, quality) = match parse_root(&chord_str) {
                    Some(r) => r,
                    None => return format!("❓ Could not parse chord: `{chord_str}`"),
                };

                match build_chord(root_idx, quality) {
                    None => format!(
                        "❓ Unknown quality `{quality}`.\n\n\
                         Valid qualities: (blank) m dim aug 7 maj7 m7 dim7 sus2 sus4 add9 6 m6"
                    ),
                    Some((name, notes)) => {
                        let root_name = NOTE_NAMES[root_idx];
                        let note_list: Vec<&str> = notes.iter().map(|&n| NOTE_NAMES[n]).collect();
                        format!(
                            "**🎹 {} {}**\n\nNotes: **{}**\nDegrees: {}",
                            root_name,
                            name,
                            note_list.join("  "),
                            note_list
                                .iter()
                                .enumerate()
                                .map(|(i, n)| format!("{}: {}", i + 1, n))
                                .collect::<Vec<_>>()
                                .join("  ·  "),
                        )
                    }
                }
            }

            "scale" => {
                if parts.len() < 2 {
                    return "Usage: `theory scale <root> <mode>`\n\n\
                             Examples:\n\
                             • `theory scale C major`\n\
                             • `theory scale A dorian`\n\
                             • `theory scale F# blues`\n\n\
                             Modes: major  minor  dorian  phrygian  lydian  mixolydian  locrian\n\
                                    harmonic minor  melodic minor  pentatonic major  pentatonic minor  blues".to_string();
                }

                let (root_idx, suffix) = match parse_root(parts[1]) {
                    Some(r) => r,
                    None => return format!("❓ Could not parse root note: `{}`", parts[1]),
                };

                // Mode = suffix of root token + remaining parts
                let mode_parts: Vec<&str> = {
                    let mut v = vec![suffix.trim()];
                    v.extend_from_slice(&parts[2..]);
                    v.retain(|s| !s.is_empty());
                    v
                };
                let mode = mode_parts.join(" ");

                match build_scale(root_idx, &mode) {
                    None => format!(
                        "❓ Unknown mode `{mode}`.\n\nValid modes: major · minor · dorian · phrygian · \
                         lydian · mixolydian · locrian · harmonic minor · melodic minor · \
                         pentatonic major · pentatonic minor · blues"
                    ),
                    Some((name, notes)) => {
                        let root_name = NOTE_NAMES[root_idx];
                        let note_list: Vec<&str> = notes.iter().map(|&n| NOTE_NAMES[n]).collect();
                        format!(
                            "**🎼 {} {}**\n\nNotes ({} tones): **{}**",
                            root_name,
                            name,
                            notes.len(),
                            note_list.join("  "),
                        )
                    }
                }
            }

            "bpm" => {
                if parts.len() < 2 {
                    return "Usage: `theory bpm <tempo>`\n\nExample: `theory bpm 128`".to_string();
                }
                let bpm: u32 = match parts[1].parse() {
                    Ok(v) if v > 0 => v,
                    _ => return format!("❓ Invalid BPM: `{}`", parts[1]),
                };
                let tempo_name = bpm_tempo_name(bpm);
                let beat_ms = 60_000.0 / bpm as f64;
                let half_ms = beat_ms * 2.0;
                let eighth_ms = beat_ms / 2.0;
                let sixteenth = beat_ms / 4.0;
                let bar_4_4 = beat_ms * 4.0;
                // Delay times useful for production
                let delay_8th = eighth_ms;
                let delay_dot8 = eighth_ms * 1.5;
                let delay_16 = sixteenth;

                format!(
                    "**🥁 BPM: {bpm}** — {tempo_name}\n\n\
                     **Beat grid (4/4)**\n\
                     Whole note  : {:.1} ms\n\
                     Half note   : {:.1} ms\n\
                     Quarter (♩) : **{:.1} ms**\n\
                     8th note    : {:.1} ms\n\
                     16th note   : {:.1} ms\n\n\
                     **Delay times**\n\
                     1/8  : {:.1} ms\n\
                     Dot 1/8 : {:.1} ms\n\
                     1/16 : {:.1} ms",
                    bar_4_4 * 2.0,
                    half_ms,
                    beat_ms,
                    eighth_ms,
                    sixteenth,
                    delay_8th,
                    delay_dot8,
                    delay_16,
                )
            }

            "interval" => {
                if parts.len() < 3 {
                    return "Usage: `theory interval <note1> <note2>`\n\nExample: `theory interval C G`".to_string();
                }
                let n1 = match note_index(parts[1]) {
                    Some(n) => n,
                    None => return format!("❓ Unknown note: `{}`", parts[1]),
                };
                let n2 = match note_index(parts[2]) {
                    Some(n) => n,
                    None => return format!("❓ Unknown note: `{}`", parts[2]),
                };
                let semitones = (n2 + 12 - n1) % 12;
                let name = INTERVAL_NAMES[semitones.min(12)];
                let desc = match semitones {
                    0 => "Same pitch — root to root.",
                    5 | 7 => "A **perfect** interval — very stable, consonant.",
                    4 | 3 => "A **third** — the building block of chords.",
                    2 | 9 => "A **second/sixth** — melodic colour.",
                    6 => "The **tritone** — maximally tense, unstable.",
                    10 | 11 => "A **seventh** — dominant tension, wants to resolve.",
                    1 | 8 => "A **half-step/minor sixth** — chromatic tension.",
                    _ => "",
                };
                format!(
                    "**🎵 {} → {}**\n\n{} ({} semitones)\n\n_{}_",
                    parts[1].to_uppercase(),
                    parts[2].to_uppercase(),
                    name,
                    semitones,
                    desc,
                )
            }

            "" => "**theory** subcommands:\n\n\
                 ```\n\
                 theory chord <root>[quality]     chord tones  (Am, Cmaj7, F#dim)\n\
                 theory scale <root> <mode>       scale notes  (C major, A dorian)\n\
                 theory bpm <tempo>               beat grid + delay times\n\
                 theory interval <note1> <note2>  interval name (C G → P5)\n\
                 ```"
            .to_string(),

            sub => format!(
                "Unknown theory sub-command `{sub}`. Use: `chord`, `scale`, `bpm`, `interval`."
            ),
        }
    }

    fn cmd_tips(&self, topic: &str) -> String {
        match topic {
            "listening" | "listen" => {
                "**🎧 Listening Tips**\n\n\
                 1. **Dedicated sessions** — close other tabs; active listening ≠ background music\n\
                 2. **FLAC/lossless** — audible difference on good headphones above 256 kbps\n\
                 3. **Headphone break-in** — new drivers need ~50 h to loosen and open up\n\
                 4. **Equal-loudness** — human hearing is non-linear; 75-85 dB SPL is the sweet spot\n\
                 5. **Log what you hear** — use `add song/album` right after; memory fades fast\n\n\
                 _Try: `theory interval C G` to train your ear while listening._".to_string()
            }
            "mixing" | "mix" => {
                "**🎚 Mixing Tips**\n\n\
                 1. **Gain staging first** — keep channel peaks at -18 dBFS before touching EQ\n\
                 2. **Cut before you boost** — subtractive EQ sounds more natural than additive\n\
                 3. **Low-cut everything** — highpass filters below 80-100 Hz clean up mud fast\n\
                 4. **Reference tracks** — A/B with a commercial mix in the same genre every 20 min\n\
                 5. **Rest your ears** — mix for 45 min, break for 15; fatigue kills judgement\n\n\
                 _Use `theory bpm` to calculate delay sync times for your project tempo._".to_string()
            }
            "mastering" | "master" => {
                "**💿 Mastering Tips**\n\n\
                 1. **Leave headroom** — deliver mixes peaking at -6 dBFS for mastering\n\
                 2. **Loudness target** — streaming targets: -14 LUFS (Spotify), -16 LUFS (Apple)\n\
                 3. **True peak ceiling** — set limiter ceiling to -1.0 dBTP to avoid inter-sample clips\n\
                 4. **Compare on multiple systems** — headphones, car, laptop speaker, phone\n\
                 5. **A/B with reference** — match loudness before comparing; louder always sounds better\n\n\
                 _Industry standard: 24-bit / 44.1 kHz for streaming; 96 kHz for archiving._".to_string()
            }
            "practice" | "instrument" => {
                "**🎸 Practice Tips**\n\n\
                 1. **Slow it down** — at 60% speed with a metronome; precision before speed\n\
                 2. **Short daily sessions** — 30 min/day beats 3.5 h on weekends (neurologically)\n\
                 3. **Deliberate practice** — work the hard part, not the parts you already know\n\
                 4. **Record yourself** — playback reveals errors your playing brain masks\n\
                 5. **Learn the theory** — use `theory scale` and `theory chord` to understand what you play\n\n\
                 _Try: `theory chord Am` then `theory scale A minor` to see how they relate._".to_string()
            }
            "gear" | "equipment" => {
                "**🎛 Gear Tips**\n\n\
                 1. **Ears > gear** — a trained ear in a treated room beats expensive gear in a reflective room\n\
                 2. **Acoustic treatment first** — bass traps + broadband panels before any monitor upgrade\n\
                 3. **Interface quality** — the pre-amp in your interface shapes your sound more than plugins\n\
                 4. **Headphones for detail** — open-back for mixing reference, closed-back for tracking\n\
                 5. **Buy used, sell new** — professional gear holds value; buy secondhand and save 40–60%\n\n\
                 _The best gear is the gear you know deeply._".to_string()
            }
            _ => {
                "**WIS Sound Tips** — pick a topic:\n\n\
                 ```\n\
                 tips listening    — critical listening habits\n\
                 tips mixing       — mix technique and workflow\n\
                 tips mastering    — loudness, headroom, targets\n\
                 tips practice     — instrument and skill development\n\
                 tips gear         — equipment and acoustics\n\
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
        let arg = text.strip_prefix("@wis-agent").unwrap_or(text).trim();

        let parts: Vec<&str> = arg.split_whitespace().collect();
        let cmd = parts.first().copied().unwrap_or("help");

        match cmd {
            "add" => self.cmd_add(&parts[1..]),
            "stats" => self.cmd_stats(parts.get(1).copied()),
            "top" => self.cmd_top(&parts[1..]),
            "theory" => self.cmd_theory(&parts[1..]),
            "tips" => self.cmd_tips(parts.get(1).copied().unwrap_or("")),
            "help" | "" => "**WIS — Sound Expert** 🎧\n\
                 _Waldiez Intelligence Sound_\n\n\
                 ```\n\
                 add song|album|artist \"<title>\" …   log music\n\
                 stats [song|album|artist|genre]     consumption stats\n\
                 top [n] [song|album|artist]         highest rated\n\
                 theory chord <root>[quality]        chord tones\n\
                 theory scale <root> <mode>          scale notes\n\
                 theory bpm <tempo>                  beat grid + delay times\n\
                 theory interval <note1> <note2>     interval name\n\
                 tips [listening|mixing|mastering|   audio advice\n\
                       practice|gear]\n\
                 help                                this message\n\
                 ```"
            .to_string(),
            _ => format!("Unknown command: `{cmd}`. Type `help` for the full command list."),
        }
    }
}

// ── Actor implementation ────────────────────────────────────────────────────────

#[async_trait]
impl Actor for WisAgent {
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
                    "agentId":     self.config.id,
                    "agentName":   self.config.name,
                    "agentType":   "sound",
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

        let reply = self.dispatch(&content);
        self.reply(&reply);
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
            .ok_or_else(|| anyhow::anyhow!("WisAgent already running"))?;

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
