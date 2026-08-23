# End-to-end scenarios

The release checklist, executed: a real broker, a real backend process, a second
process standing in for a remote node, and a browser.

A scenario is a file of steps and assertions. A *profile* decides how those
steps are executed — pacing, capture, and which model answers:

```
test      headless · condition waits · no capture · fake model
rehearse  headed   · minimum dwell   · video      · fake model
demo      headed   · minimum dwell   · video      · real model
```

Assertions stay on in every profile, the demo included. A profile changes
pacing, capture and the provider — never what is checked.

## Layout

```
e2e/
├── conftest.py           preconditions and session fixtures
├── pytest.ini            configuration for this suite
├── profiles.py           test | rehearse | demo
├── harness/              everything a scenario may call
│   ├── broker.py         reach, wait for, stop and restart mosquitto
│   ├── backend.py        run the app as a subprocess under a given environment
│   ├── node.py           run remote_runner as a subprocess
│   ├── waiting.py        condition waits
│   ├── probe.py          REST, WebSocket and MQTT clients
│   ├── logs.py           read the app log and assert on what it must not contain
│   └── browser.py        Playwright fixtures and page objects
├── scripts/              fake-provider scripts as data: prompt substring → reply
├── scenarios/            the regression core — must always pass
│   ├── conftest.py       the agents these scenarios work with
│   ├── test_a01_broker_and_backend.py
│   ├── test_a02_remote_node.py
│   ├── test_a03_the_pages.py
│   ├── test_a04_spawn_an_agent.py
│   ├── test_a05_talk_to_it.py
│   ├── test_a06_lifecycle.py
│   ├── test_a07_counters.py
│   ├── test_a08_broker_down.py
│   ├── test_a09_shutdown.py
│   ├── test_a10_reset.py
│   ├── test_chat_target.py
│   ├── release/          scenarios for what is new in the release being cut
│   └── demo/             demonstration stories, written to be recorded
└── out/                  gitignored — everything a run leaves behind
    ├── state/            per-run state directory (kept on failure, cleaned on green)
    ├── videos/           recordings from the headed profiles
    ├── traces/           Playwright traces for failures
    └── logs/             backend and node stdout, kept on failure
```

## Running

```bash
make e2e-setup        # one-time: install the Playwright browser
make dev              # mosquitto on 1883
make e2e              # regression core + demo scenarios, headless, fake model
```

Prerequisites are enforced at the start of a run, and a missing one is an
error, not a skip: a run without them checks nothing, and reporting that as
success is worse than reporting nothing.

- **A broker** must be reachable.
- **A Playwright browser** must be installed — `a03`, `a05`, `a07`, `chat_target`
  and every demo scenario drive one, headless included. `make e2e-setup` is the
  one command; the error names it.
- **No `WACTORZ_STATE_DIR` in the environment.** The suite mints a fresh state
  directory per run under `e2e/out/state/`; an inherited one is the leak this
  rule exists to catch, so it is refused, not used.

```bash
make e2e-release     # everything: core + release/ + demo/  — before a tag
make e2e-rehearse    # headed, fake model — for iterating on pacing
make e2e-demo        # headed, real model — for the take you keep
```

Recordings are 720p by default. `--resolution 1080p` (or `E2E_RESOLUTION=1080p`)
records at 1920x1080 — the page renders at that size too, so what is recorded is
what was tested. A window that large does not fit on a 1080p screen and the
desktop clamps it, which affects only how much of the run you can watch live:

```bash
make e2e-demo E2E_RESOLUTION=1080p
```

To watch a browser scenario without changing anything else about the run, pass
`--headed` (or set `E2E_HEADED=1`). It is orthogonal to the profile — `rehearse`
also changes pacing, capture and which script is loaded, and often what you want
is just to see what the `test` profile is doing:

```bash
WACTORZ_STATE_DIR= pytest e2e --headed
WACTORZ_STATE_DIR= pytest e2e/scenarios/test_a05_talk_to_it.py --headed
```

## The regression core

The core covers the seams a unit test structurally cannot reach: browser↔WS
frames, agent↔broker, main↔node, container lifecycle. If a scenario here could
have been written as a unit test, it should have been.

| scenario | claim |
| --- | --- |
| `a01_broker_and_backend` | the backend reaches ready, prints its address after startup rather than before it, and writes no credentials to the log |
| `a02_remote_node` | a node comes online and is listed, and `remote_runner` imports nothing from the `wactorz` package |
| `a03_the_pages` | every page renders and its data arrives |
| `a04_spawn_an_agent` | a catalogue agent spawns and reaches `running` |
| `a05_talk_to_it` | a message reaches the agent and its reply reaches the page |
| `a06_lifecycle` | pause, resume, stop, start, delete, and `main` refusing to be paused |
| `a07_counters` | after two stop/start cycles, message count and cost are unchanged |
| `a08_broker_down` | a local command takes effect with the broker stopped |
| `a09_shutdown` | one interrupt, exits in well under a second |
| `a10_reset` | `wactorz-reset` leaves a clean state directory and the system restarts empty |
| `chat_target` | the chosen agent survives a reload; a stopped one is refused rather than silently switched |

`a08` is the one core scenario that can skip, and only for a reason that is not
about the product: it has to take the broker away, and the suite may only stop
the development container this repository starts. A broker somebody else runs —
a system mosquitto, one belonging to a house — is left alone, and the scenario
says so rather than unplugging it.

Two assert a property rather than liveness:

- **`a02`'s import check is about packaging.** The runner is a single
  self-contained script, which is what lets it be copied to a single-board
  machine on its own. An `ImportError` means that property is broken, and the
  failure message says so rather than reading as a slow start. Every node the
  suite starts runs from a copy outside the repository with `wactorz` made
  unimportable — on a developer machine the package is installed and importable
  from anywhere, so the copy alone would prove nothing.
- **`a06` is one test per lifecycle row.** Refusing a message to a paused agent,
  refusing one to a stopped agent, and staying supervised a minute after a
  restart are each a case where the system can report success while having done
  nothing, so a failure names which claim broke.

(The numbers are zero-padded because pytest collects files in string order:
`a10` sorts before `a1`, and the reset scenario must not run first.)

## Release candidates

The regression core answers "did we break what worked". `scenarios/release/`
answers "does the thing we just built work".

The habit: read `[Unreleased]` in the changelog before tagging, and every
user-facing entry gets a scenario here — or already has one, if it was written
with the feature, which is the better time. One `make e2e-release` run replaces
the manual pre-tag walkthrough.

This directory is a revolving door, not a museum. When a scenario proves stable
and the feature becomes core, promote it into `scenarios/`; when a release's
checks are superseded, delete them.

Scenarios that need a live Home Assistant belong here too. They skip — loudly,
never red — when `HA_URL`/`HA_TOKEN` are not set.

### Real hardware, when it is available

A second process on localhost exercises the runner but not the road to it:
SSH provisioning, the runner as a deployed artifact on another architecture,
and a broker hop across a real LAN. Scenarios that need that are marked
`@pytest.mark.requires_node`.

No new configuration is invented for this: the target is one of the deploy
targets already configured for `/deploy` (`DEPLOY_TARGETS` and the
`DEPLOY_<NAME>_*` family), so the scenario exercises the same coordinates a
user deploys with. What a deploy target cannot express is *consent* — the
suite may reset and redeploy this machine — and that is the one thing to opt
into, either as `E2E_REAL_NODE=<name>` or a pytest flag naming the target.
Without it, the scenario skips — loudly, never red.

The rules are the HA rules, extended:

- **Skips loudly, never red**, when no target is named — and a target
  that cannot be reached at setup is a skip too; hardware is allowed to be off.
  A failure *mid-scenario* is red: that is the point of having it.
- **A dedicated device only.** The harness resets the node between runs, so a
  scenario may never deploy to a machine that does other work — which is why
  the target is named per run rather than read wholesale from the config.
- **Nothing in the regression core requires it.** Real-node scenarios live in
  `scenarios/release/` and run before a tag on a bench machine, not on every
  laptop run and not in CI.

The Home Assistant *add-on* as an add-on — Supervisor, install cycle, ingress —
stays with the manual checklist for now; the suite takes the HA-dependent
scenarios against a plain HA instance first.

## Demonstrations

A demo scenario is a story a home-automation user recognizes, told end to end:
"when the nursery temperature passes 26°, turn on the fan", or "when a person
is on the porch camera after dark, turn on the light and tell me" — set up in
chat, watched happening on the dashboard, with the agent card, the feed and the
reply all visible.

Because the same file runs as a test under the `test` profile, a demo cannot
quietly go stale: when the product moves, the scenario goes red before anyone
re-records it. A video of a passing test is a video of something that works.

One honest limit to that: a demo marked `requires_llm("real")` is skipped under
`test`, so it is only exercised when someone runs the demo profile — it *can*
go stale silently. Keep those few, and re-run them before sharing what they
produced.

- **Assert like a test, pace like a film.** The only timing a scenario may
  express is a `dwell="readable"` step option, which the test profile ignores
  and the demo profile honours.
- **An assertion must hold under both providers** — the fake and the real
  model. One that depends on what the model *said* belongs to a scenario marked
  `@pytest.mark.requires_llm("real")`, which `test` skips rather than fails.
- **Replies for the camera live in `e2e/scripts/`** — JSON mapping prompt
  substrings to well-phrased canned replies, loaded by the profile. Tests use
  the plain default; no scenario mentions which script is in use.
- **Iterate with `rehearse`.** A take against a real model costs money and
  varies run to run. Getting pacing and framing right against the fake, then
  doing one real take, is the difference between a recording costing cents and
  costing a morning.

Recordings land in `e2e/out/videos/`. Share the file, not the directory.

## Writing a scenario

Scenarios are Python test files; requirements and knobs use the mechanisms
pytest and the harness already have:

- **Availability is a marker.** `@pytest.mark.requires_ha`,
  `@pytest.mark.requires_node`, `@pytest.mark.requires_llm("real")` — the
  profile or conftest decides what skips, and a skip is loud, never red.
- **Speak harness only.** Every other verb lives in `harness/` — no Playwright,
  asyncio or broker imports in a scenario file. That is what makes "no timing
  in scenarios" hold by construction.
- **Wait on conditions, never on the clock** (`harness/waiting.py`). Two shapes
  that look like sleeps and are not: `holds_for` (a state persists across a
  window) and `becomes_and_stays` (settles, then stays settled).
- **`dwell` is a step option, not a sleep** — `harness.chat(..., dwell="readable")`.
  The test profile ignores it; the demo profile honours it.
- **Assert the user-visible invariant, not the change's intent.** "The cost
  total moves while an agent answers" catches a regression; "totals are right
  after connect" describes one behaviour and passes against another.
- **Prefer properties over golden text.** Counters that do not inflate, a reply
  that streams, a card that appears — not exact wording.
- **Compare against what you captured**, not hardcoded numbers:
  `harness.capture("message_count", "cost_usd")` before a lifecycle cycle, then
  assert the values are unchanged after it.
- **Seed the state directory, never reuse a real one.** Fresh by default; a
  fixture copy when the point is migrations.

## Rules

**State is isolated.** The suite mints a fresh `e2e/out/state/<run-id>/` per
run — kept for debugging on failure, cleaned on green. The `make e2e*` targets
run with `WACTORZ_STATE_DIR` unset so a value exported for normal work never
leaks in; a direct `pytest e2e/` invocation with it set is refused, and the
error says to unset it, because that is the same leak arriving by the other
door.

**No sleeps.** Waiting is on conditions. The only timing a scenario may express
is a `dwell="readable"` step option, and only the demo profile reads it.

**Artefacts are trimmed, not hoarded.** A green run cleans its own state
directory; a failing one keeps it. Logs, videos and traces survive on purpose —
they are what explains a failure after the fact — but only for the last few runs,
because the recording profiles write a video per browser scenario and nothing
else would ever remove them. `make e2e-clean` removes the lot.

**Not a required check.** Real processes, a real broker and a browser give this
more ways to be non-deterministic than the unit suite has. It runs on demand
and before a tag, and earns gating only after it has been boringly green for a
while — a flaky required check gets ignored, and an ignored check is worse than
none.
