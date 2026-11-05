# Drift

A decision record, not a guide: which tests say that a model's input, or its
output, no longer looks like what the model was made for, over which windows;
how an alarm is debounced so one shift is one alarm; what is recorded and
published; and what the baseline rule does with an alarm. It builds on the
data layout and loader (`data-storage.md`), event time (`event-time.md`), the
graph (`labels-and-provenance.md`), the `train` step (`train-step.md`) and the
promotion policy (`promotion-policy.md`). The guide for what is shipped is the
"Watching for drift" section of [library.md](../library.md).

## The problem

A version is fitted on a window of readings and judged on another. Once it is
served, nothing watches whether the readings still look like those. A sensor
that is moved, a valve that is replaced, a season that turns, all change what
arrives without a single reading being wrong, and a detector tuned to the old
readings alarms on everything or on nothing. The labels say so eventually,
when a person has answered enough questions; by then the model has been wrong
for days.

The loop has the step that makes a new version and the policy that decides
whether it is served. What it lacks is the thing that notices, and fires the
step, without a person: the `observation` the graph has a kind for and nothing
writes.

## The decision

### Three tests, two windows

Drift is measured by an agent on main, `drift`, spawned from the catalogue for
one model, on a cadence (`WACTORZ_DRIFT_CHECK_S`, ten minutes unless set).
Each check reads two windows of the model's recorded source through the loader,
in event time:

- the **current** window: the last `window_s` of event time (a day unless
  said), ending now;
- the **reference** window: what the served version learned from, read from
  the version's `window` metadata when the `train` step made it; else the
  window the agent was spawned with; else the window of the same length
  before the current one. The reference is pinned to the version served, so a
  promotion moves it and the figures start again from the new version's own
  readings.

Over them it runs three tests, each chosen because it needs no assumption
about how the readings are spread and no dependency beyond the standard
library:

| Test | On | Says | Alarms when |
| --- | --- | --- | --- |
| PSI, the population stability index | each numeric feature column, binned by the reference's deciles | how far the current distribution moved from the reference, in the units credit scoring has used for decades: under 0.1 nothing, 0.1 to 0.2 a moderate shift, over 0.2 a shift that matters | `psi >= 0.2` |
| KS, the two-sample Kolmogorov–Smirnov test | each numeric feature column | the largest distance between the two empirical distributions, `D`, with the asymptotic `p` | `D >= 0.1` and `p < 0.05` |
| ADWIN, adaptive windowing | the model's scores (or residuals), read from a recorded source and column the agent names, over the current window in event time | the moment the mean of the sequence changed, if it did, with the means on either side | a cut was found inside the window |

The feature tests ask whether the inputs changed; ADWIN asks whether the
outputs did, and when. The score source is also held against the reference
with PSI and KS, so a shift in the scores that began before the current window
is still seen. Each test runs only when both windows hold at least `min_rows`
rows (100 unless said), since a decile of ten readings says nothing. The
thresholds are options of the agent, with these defaults.

The windows are event time, as `event-time.md` requires, so a replay through
the simulator drifts at the same moments a plant did.

### An alarm is an observation, a line, a topic and a gauge

A test that crosses its threshold on a column raises an **alarm**, and an
alarm is four things at once:

- an `observation` node in the graph, about the model, with the test, the
  column, the statistic, the threshold, the `p` where there is one, both
  windows, the version served and, for ADWIN, the moment of the cut;
- one line on the feed, under the agent's name, saying the same;
- one message on `models/<name>/drift`, not retained, carrying the
  observation's id, so a rule can act on it (below);
- the gauges `wactorz_model_drift{model,test,column}` (the statistic) and
  `wactorz_model_drift_alarm{model,test,column}` (`1` while raised), which
  every check sets whether or not anything alarms.

### Debounce: one shift is one alarm

A shift that lasts a week would cross the threshold at every check for a
week. So an alarm is an **episode**, keyed by model, test and column: it is
raised once, at the first check that crosses, and stays raised, with the
gauge following the statistic, until a check finds the statistic back under
the threshold, when it is said to have settled, on the feed only. The next
crossing of the same key is a new episode and a new observation. A promotion
changes the reference, so every episode of that model settles or is raised
again against the new version, which is what the record should show: the
drift was from the old version.

Across a restart the agent reads its episodes back from the graph: an
observation of the version served, whose window reaches into the current one,
is an episode still raised, so main coming up does not alarm again on a shift
it already recorded.

### The baseline rule

The retrain that the controller is later compared against is a rule the
operator writes, with no code: trigger on `models/<name>/drift`, condition on
the test or the column if wanted, action a `task` to the `trainer` with the
window and split the rule names and the observation's id. The trainer runs
the step as it would for a person, and joins the run `led_to` from the
observation, so the graph says the retrain happened because of the drift.
The rule's `cooldown_seconds` is the second debounce: however many columns
alarm in one check, one retrain is fired.

```json
{"type": "rule", "name": "imu-retrain",
 "triggers": ["models/imu-detector/drift"],
 "conditions": [{"field": "test", "op": "in", "value": ["psi", "adwin"]}],
 "actions": [{"type": "task", "agent": "trainer",
              "payload": {"split": {"kind": "time_split", "at": "{at}"},
                          "observation": "{observation}",
                          "reason": "drift: {test} on {column} ({statistic})"}}],
 "cooldown_seconds": 86400}
```

What the baseline does with an alarm is therefore fixed: retrain on the
trainer's default window (the last week), cut at the moment the alarm names,
set the version as shadow when the step's margin is met, and leave the
promotion to the policy. It asks nobody and reads no label. The controller's
claim is that it does better than this with the same alarms, the same step and
the same policy: fewer retrains, fewer questions, the same recall.

For that, a rule's payload fills a placeholder with the trigger's value as it
is, so `{at}` is a number the trainer takes rather than its text, and fills
placeholders inside nested objects, so the split above is written where the
trainer reads it.

### The metrics beside it

With the drift gauges, the model loop reports what Prometheus did not have:

| Metric | Set by | What it says |
| --- | --- | --- |
| `wactorz_model_score{model,side,metric}` | main's check of a trial | each side's figure on the main metric of the kind the trial is judged as, or on the policy's, from the labels so far |
| `wactorz_model_shadow_agreement{model}` | the same check | the share of the trial's pairs both sides answered alike |
| `wactorz_model_train_seconds{model}` | the trainer | how long the last run's fit took |
| `wactorz_model_train_runs_total{model,outcome}` | the trainer | runs by how they ended |

A trial's gauges are set at every check of every trial under way, with or
without a policy, and dropped when the trial ends, so a series that stops is a
trial that ended rather than a model that scores nothing.

## What was decided against

- **A streaming detector on the topics.** Rejected: the agent would need a
  subscription per model and per feature, would see nothing recorded before it
  started, and would hold its state in memory alone. Reading the loader's
  frames on a cadence sees the same readings the trainer will, in event time,
  across a restart.
- **A drift setting beside the pointers, like the policy.** Rejected: a policy
  is six numbers about a name; drift needs the source, the columns, the score
  source and five thresholds, which is a spawn's options. The agent is where
  the recorder and the trainer are.
- **Tests that need SciPy or River.** Rejected: both are right, and both are
  a dependency a Pi install does not want for three statistics the standard
  library computes. The asymptotic KS `p` is the Kolmogorov series; ADWIN is
  the published algorithm with its exponential histogram.
- **Alarming on `p` alone.** Rejected: on a day of readings every difference
  is significant. The statistic is the threshold, and `p` is a second
  condition for KS, not the first.
- **A retrain the agent fires itself.** Rejected: the agent observes; what
  follows from an observation is a rule's, or the controller's, so the two can
  be compared on the same observations.
- **Rewriting the observation as the episode goes on.** Rejected: a node's
  payload is never rewritten (`labels-and-provenance.md`); the gauge carries
  the statistic as it moves, and the node carries what was seen when the
  episode began.

## What follows

- **1.7, this**: `wactorz/drift/` (the three tests, the check over two
  windows, the observation and the alarms route), the `drift` library agent,
  `wactorz/monitoring/model_metrics.py`, `GET /api/models/{name}/drift`, the
  `observation` edge from the trainer, and the rule agent's typed placeholders.
- **1.3, the simulator**, scores the baseline rule above against a static
  model, by the benchmark protocol.
- **1.9, the controller**, reads the same observations through the loader
  and the graph, decides whether to retrain, ask or wait, and writes its
  `decision` between the observation and the action.
- **The paid drift**, in `wactorz-mlops`, adds what these three do not: drift
  per segment and per device, multivariate tests, and the choice of the
  window to retrain on from the observations themselves.
