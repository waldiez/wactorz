//! Rust counterpart to `tests/backend_parity_harness.py`.
//!
//! Runs the same supervisor scenarios defined in
//! `tests/parity_fixtures/backend_supervisor_parity.json` and emits a JSON
//! result that must match the Python harness output byte-for-byte (after
//! parsing).
//!
//! Usage:
//!   cargo run -q -p wactorz-core --bin backend_parity -- \
//!       --fixture <path> [--assert-expected]

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use serde::{Deserialize, Serialize};
use tokio::sync::mpsc;

use wactorz_core::actor::{Actor, ActorConfig, ActorState};
use wactorz_core::message::{ActorCommand, Message, MessageType};
use wactorz_core::metrics::ActorMetrics;
use wactorz_core::registry::{ActorFactory, ActorSystem, Supervisor, SupervisorStrategy};

// ── ProbeActor ────────────────────────────────────────────────────────────────

struct ProbeActor {
    config: ActorConfig,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    metrics: Arc<ActorMetrics>,
    starts: Arc<AtomicU64>,
    crash_remaining: Arc<AtomicU64>,
}

impl ProbeActor {
    fn new(
        name: &str,
        starts: Arc<AtomicU64>,
        crash_remaining: Arc<AtomicU64>,
    ) -> Self {
        let config = ActorConfig::new(name);
        let (tx, rx) = mpsc::channel(16);
        Self {
            config,
            mailbox_tx: tx,
            mailbox_rx: Some(rx),
            metrics: Arc::new(ActorMetrics::new()),
            starts,
            crash_remaining,
        }
    }
}

#[async_trait::async_trait]
impl Actor for ProbeActor {
    fn id(&self) -> String { self.config.id.clone() }
    fn name(&self) -> &str { &self.config.name }
    fn state(&self) -> ActorState { ActorState::Running }
    fn metrics(&self) -> Arc<ActorMetrics> { self.metrics.clone() }
    fn mailbox(&self) -> mpsc::Sender<Message> { self.mailbox_tx.clone() }

    async fn handle_message(&mut self, _msg: Message) -> Result<()> { Ok(()) }

    async fn run(&mut self) -> Result<()> {
        self.starts.fetch_add(1, Ordering::Relaxed);
        if self.crash_remaining.load(Ordering::Relaxed) > 0 {
            self.crash_remaining.fetch_sub(1, Ordering::Relaxed);
            anyhow::bail!("simulated crash");
        }
        let mut rx = self
            .mailbox_rx
            .take()
            .ok_or_else(|| anyhow::anyhow!("mailbox already consumed"))?;
        loop {
            match rx.recv().await {
                None => break,
                Some(m) => {
                    if let MessageType::Command { command: ActorCommand::Stop } = &m.payload {
                        break;
                    }
                }
            }
        }
        Ok(())
    }
}

// ── Fixture / output types ────────────────────────────────────────────────────

#[derive(Deserialize)]
struct ActorCfg {
    name: String,
    #[serde(default)]
    crash_count: u64,
}

#[derive(Deserialize)]
struct Expected {
    start_counts: HashMap<String, u64>,
    restart_counts: HashMap<String, u64>,
    final_states: HashMap<String, String>,
}

#[derive(Deserialize)]
struct Scenario {
    name: String,
    strategy: SupervisorStrategy,
    actors: Vec<ActorCfg>,
    expected: Expected,
}

#[derive(Deserialize)]
struct Fixture {
    contract: String,
    scenarios: Vec<Scenario>,
}

#[derive(Serialize)]
struct ActorResult {
    final_state: String,
    restart_count: u64,
    starts: u64,
}

#[derive(Serialize)]
struct ScenarioResult {
    actors: HashMap<String, ActorResult>,
    scenario: String,
}

#[derive(Serialize)]
struct Output {
    contract: String,
    results: Vec<ScenarioResult>,
}

// ── Scenario runner ───────────────────────────────────────────────────────────

async fn run_scenario(scenario: &Scenario) -> Result<ScenarioResult> {
    let system = ActorSystem::new();
    let mut sup =
        Supervisor::with_poll_interval(system.clone(), Duration::from_millis(50));

    let mut starts_map: HashMap<String, Arc<AtomicU64>> = HashMap::new();

    for cfg in &scenario.actors {
        let starts = Arc::new(AtomicU64::new(0));
        let crash_rem = Arc::new(AtomicU64::new(cfg.crash_count));
        starts_map.insert(cfg.name.clone(), starts.clone());

        let name = cfg.name.clone();
        let starts_c = starts.clone();
        let crash_c = crash_rem.clone();
        let factory: ActorFactory = Arc::new(move || {
            Box::new(ProbeActor::new(&name, starts_c.clone(), crash_c.clone()))
        });

        sup.supervise(
            &cfg.name,
            factory,
            scenario.strategy.clone(),
            3,
            60.0,
            0.0,
        );
    }

    sup.start().await?;
    tokio::time::sleep(Duration::from_millis(350)).await;

    // Collect status before stopping
    let status = sup.status();
    let status_map: HashMap<String, u64> = status
        .iter()
        .filter_map(|v| {
            let name = v.get("name")?.as_str()?.to_string();
            let restarts = v.get("restarts_used")?.as_u64()?;
            Some((name, restarts))
        })
        .collect();

    let registry_list = system.registry.list().await;
    let registry_map: HashMap<String, ActorState> = registry_list
        .into_iter()
        .map(|e| (e.name, e.state))
        .collect();

    let mut actors: HashMap<String, ActorResult> = HashMap::new();
    for cfg in &scenario.actors {
        let starts = starts_map[&cfg.name].load(Ordering::Relaxed);
        let restart_count = status_map.get(&cfg.name).copied().unwrap_or(0);
        let final_state = registry_map
            .get(&cfg.name)
            .map(|s| match s {
                ActorState::Running => "running",
                ActorState::Stopped => "stopped",
                ActorState::Paused => "paused",
                ActorState::Initializing => "initializing",
                ActorState::Failed(_) => "failed",
            })
            .unwrap_or("running") // deregistered after stop command → treat as running
            .to_string();

        actors.insert(cfg.name.clone(), ActorResult { starts, restart_count, final_state });
    }

    sup.stop().await;

    Ok(ScenarioResult { scenario: scenario.name.clone(), actors })
}

// ── CLI ───────────────────────────────────────────────────────────────────────

fn parse_args() -> (PathBuf, bool) {
    let args: Vec<String> = std::env::args().collect();
    let mut fixture = PathBuf::from(
        std::env::current_dir()
            .unwrap_or_default()
            .join("tests/parity_fixtures/backend_supervisor_parity.json"),
    );
    let mut assert_expected = false;

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--fixture" => {
                i += 1;
                if i < args.len() {
                    fixture = PathBuf::from(&args[i]);
                }
            }
            "--assert-expected" => {
                assert_expected = true;
            }
            _ => {}
        }
        i += 1;
    }
    (fixture, assert_expected)
}

fn build_expected(fixture: &Fixture) -> Output {
    let results = fixture
        .scenarios
        .iter()
        .map(|s| {
            let actors = s
                .expected
                .start_counts
                .iter()
                .map(|(name, &starts)| {
                    let restart_count =
                        s.expected.restart_counts.get(name).copied().unwrap_or(0);
                    let final_state =
                        s.expected.final_states.get(name).cloned().unwrap_or_default();
                    (name.clone(), ActorResult { starts, restart_count, final_state })
                })
                .collect();
            ScenarioResult { scenario: s.name.clone(), actors }
        })
        .collect();
    Output { contract: fixture.contract.clone(), results }
}

// ── main ──────────────────────────────────────────────────────────────────────

#[tokio::main]
async fn main() -> Result<()> {
    let (fixture_path, assert_expected) = parse_args();

    let content = std::fs::read_to_string(&fixture_path)
        .map_err(|e| anyhow::anyhow!("cannot read fixture {}: {e}", fixture_path.display()))?;
    let fixture: Fixture = serde_json::from_str(&content)?;

    let mut results = Vec::new();
    for scenario in &fixture.scenarios {
        results.push(run_scenario(scenario).await?);
    }
    let actual = Output { contract: fixture.contract.clone(), results };

    if assert_expected {
        let expected = build_expected(&fixture);
        let actual_val = serde_json::to_value(&actual)?;
        let expected_val = serde_json::to_value(&expected)?;
        if actual_val != expected_val {
            let out = serde_json::json!({
                "actual":   actual_val,
                "expected": expected_val,
            });
            eprintln!("{}", serde_json::to_string_pretty(&out)?);
            std::process::exit(1);
        }
    }

    println!("{}", serde_json::to_string_pretty(&actual)?);
    Ok(())
}
