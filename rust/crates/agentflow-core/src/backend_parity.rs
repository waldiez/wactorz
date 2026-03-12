use std::collections::BTreeMap;
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use anyhow::Result;
use serde::{Deserialize, Serialize};
use tokio::sync::Mutex;
use tokio::sync::mpsc;

use crate::actor::{Actor, ActorConfig, ActorState};
use crate::message::{ActorCommand, Message, MessageType};
use crate::metrics::ActorMetrics;
use crate::registry::{ActorFactory, ActorSystem, Supervisor, SupervisorStrategy};

#[derive(Debug, Deserialize)]
pub struct Fixture {
    pub contract: String,
    pub scenarios: Vec<ScenarioFixture>,
}

#[derive(Debug, Deserialize)]
pub struct ScenarioFixture {
    pub name: String,
    pub strategy: SupervisorStrategy,
    pub actors: Vec<ActorFixture>,
    pub expected: ExpectedScenario,
}

#[derive(Debug, Deserialize)]
pub struct ActorFixture {
    pub name: String,
    pub kind: String,
    #[serde(default)]
    pub crash_count: u64,
}

#[derive(Debug, Deserialize)]
pub struct ExpectedScenario {
    pub start_counts: BTreeMap<String, u64>,
    pub restart_counts: BTreeMap<String, u64>,
    pub final_states: BTreeMap<String, String>,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct ParityReport {
    pub contract: String,
    pub results: Vec<ScenarioReport>,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct ScenarioReport {
    pub scenario: String,
    pub actors: BTreeMap<String, ActorReport>,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct ActorReport {
    pub starts: u64,
    pub restart_count: u64,
    pub final_state: String,
}

#[derive(Debug)]
struct ProbeActor {
    config: ActorConfig,
    state: ActorState,
    metrics: Arc<ActorMetrics>,
    mailbox_tx: mpsc::Sender<Message>,
    mailbox_rx: Option<mpsc::Receiver<Message>>,
    tracker: Arc<ActorTracker>,
}

#[derive(Debug)]
struct ActorTracker {
    starts: AtomicU64,
    crash_remaining: AtomicU64,
}

impl ActorTracker {
    fn new(crash_count: u64) -> Self {
        Self {
            starts: AtomicU64::new(0),
            crash_remaining: AtomicU64::new(crash_count),
        }
    }
}

impl ProbeActor {
    fn new(name: impl Into<String>, tracker: Arc<ActorTracker>) -> Self {
        let config = ActorConfig::new(name);
        let (mailbox_tx, mailbox_rx) = mpsc::channel(config.mailbox_capacity);
        Self {
            config,
            state: ActorState::Initializing,
            metrics: Arc::new(ActorMetrics::new()),
            mailbox_tx,
            mailbox_rx: Some(mailbox_rx),
            tracker,
        }
    }
}

#[async_trait::async_trait]
impl Actor for ProbeActor {
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

    async fn handle_message(&mut self, _message: Message) -> Result<()> {
        Ok(())
    }

    async fn run(&mut self) -> Result<()> {
        self.tracker.starts.fetch_add(1, Ordering::Relaxed);
        let remaining = self.tracker.crash_remaining.load(Ordering::Relaxed);
        if remaining > 0 {
            self.tracker.crash_remaining.fetch_sub(1, Ordering::Relaxed);
            self.state = ActorState::Failed("intentional crash".to_string());
            anyhow::bail!("intentional crash");
        }

        self.state = ActorState::Running;
        let mut rx = self
            .mailbox_rx
            .take()
            .ok_or_else(|| anyhow::anyhow!("mailbox already taken"))?;

        loop {
            match rx.recv().await {
                None => break,
                Some(message) => {
                    self.metrics.record_received();
                    match &message.payload {
                        MessageType::Command {
                            command: ActorCommand::Stop,
                        } => {
                            self.state = ActorState::Stopped;
                            break;
                        }
                        _ => {
                            self.handle_message(message).await?;
                            self.metrics.record_processed();
                        }
                    }
                }
            }
        }
        Ok(())
    }
}

pub fn expected_report(fixture: &Fixture) -> ParityReport {
    ParityReport {
        contract: fixture.contract.clone(),
        results: fixture
            .scenarios
            .iter()
            .map(|scenario| ScenarioReport {
                scenario: scenario.name.clone(),
                actors: scenario
                    .expected
                    .start_counts
                    .iter()
                    .map(|(name, starts)| {
                        (
                            name.clone(),
                            ActorReport {
                                starts: *starts,
                                restart_count: *scenario
                                    .expected
                                    .restart_counts
                                    .get(name)
                                    .unwrap_or(&0),
                                final_state: scenario
                                    .expected
                                    .final_states
                                    .get(name)
                                    .cloned()
                                    .unwrap_or_else(|| "unknown".to_string()),
                            },
                        )
                    })
                    .collect(),
            })
            .collect(),
    }
}

pub fn load_fixture(path: &Path) -> Result<Fixture> {
    let raw = std::fs::read_to_string(path)?;
    Ok(serde_json::from_str(&raw)?)
}

pub async fn run_fixture(path: &Path) -> Result<ParityReport> {
    let fixture = load_fixture(path)?;
    let mut results = Vec::new();
    for scenario in &fixture.scenarios {
        results.push(run_scenario(scenario).await?);
    }
    Ok(ParityReport {
        contract: fixture.contract,
        results,
    })
}

async fn run_scenario(scenario: &ScenarioFixture) -> Result<ScenarioReport> {
    let system = ActorSystem::new();
    let mut supervisor = Supervisor::new(system.clone());
    supervisor.with_poll_interval_secs(0.05);

    let trackers = Arc::new(Mutex::new(BTreeMap::<String, Arc<ActorTracker>>::new()));

    for actor in &scenario.actors {
        let tracker = Arc::new(ActorTracker::new(if actor.kind == "crasher" {
            actor.crash_count
        } else {
            0
        }));
        trackers
            .lock()
            .await
            .insert(actor.name.clone(), Arc::clone(&tracker));

        let name = actor.name.clone();
        let tracker_for_factory = Arc::clone(&tracker);
        let factory: ActorFactory = Arc::new(move || {
            Box::new(ProbeActor::new(name.clone(), Arc::clone(&tracker_for_factory)))
        });

        supervisor.supervise(
            actor.name.clone(),
            factory,
            scenario.strategy.clone(),
            3,
            60.0,
            0.0,
        );
    }

    supervisor.start().await?;
    wait_for_expected_actors(&system, scenario).await?;

    let status_rows = supervisor.status();
    let registry_rows = system.registry.list().await;
    let tracker_rows = trackers.lock().await;

    let mut actors = BTreeMap::new();
    for actor in &scenario.actors {
        let status = status_rows
            .iter()
            .find(|row| row["name"] == actor.name)
            .ok_or_else(|| anyhow::anyhow!("missing status row for {}", actor.name))?;
        let entry = registry_rows
            .iter()
            .find(|row| row.name == actor.name)
            .ok_or_else(|| anyhow::anyhow!("missing registry row for {}", actor.name))?;
        let tracker = tracker_rows
            .get(&actor.name)
            .ok_or_else(|| anyhow::anyhow!("missing tracker for {}", actor.name))?;

        actors.insert(
            actor.name.clone(),
            ActorReport {
                starts: tracker.starts.load(Ordering::Relaxed),
                restart_count: status["restarts_used"].as_u64().unwrap_or(0),
                final_state: match &entry.state {
                    ActorState::Initializing => "initializing".to_string(),
                    ActorState::Running => "running".to_string(),
                    ActorState::Paused => "paused".to_string(),
                    ActorState::Stopped => "stopped".to_string(),
                    ActorState::Failed(_) => "failed".to_string(),
                },
            },
        );
    }

    supervisor.stop().await;

    Ok(ScenarioReport {
        scenario: scenario.name.clone(),
        actors,
    })
}

async fn wait_for_expected_actors(system: &ActorSystem, scenario: &ScenarioFixture) -> Result<()> {
    let deadline = tokio::time::Instant::now() + Duration::from_secs(3);
    loop {
        let rows = system.registry.list().await;
        let all_present = scenario.actors.iter().all(|actor| {
            rows.iter()
                .any(|row| row.name == actor.name && matches!(row.state, ActorState::Running))
        });
        if all_present {
            return Ok(());
        }
        if tokio::time::Instant::now() >= deadline {
            anyhow::bail!("timed out waiting for running actors in scenario {}", scenario.name);
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}
