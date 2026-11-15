# The `train` step

A decision record, not a guide: how a model version is made inside the loop,
where the training runs, what is stored with the version, and when the step
moves a pointer. It builds on the pointers (`model-pointers.md`), the data
layout and loader (`data-storage.md`), labels and the graph
(`labels-and-provenance.md`) and event time (`event-time.md`). The guide for
what is shipped is the "Training a version" section of
[library.md](../library.md).

## The problem

A version reaches the store by a script a person runs: the IMU example's
`train.py` fits a model, stores it and sets a pointer, by hand. Nothing in the
loop can ask for a retrain, so a rule that sees drift, or a controller that
reads a label, has nothing to call. Nothing records what a version was trained
on, so the retention sweep protects nothing, a shadow trial's outcome has no
`model` node to be about, and a review after an override cannot find the
versions that followed from the answer that changed.

And training cannot run where the agents run. A fit that takes a minute in
main's process holds every actor for that minute, broker keepalive included;
one that grows past the memory of a Pi takes main down with it; one that never
ends cannot be stopped without stopping main. The user's training code is the
least reviewed code in the system, and the step must survive it.

## The decision

### A recipe is two functions

A recipe is a Python module the user writes, named by its importable name
(`my_models.recipe`) or by its file (`examples/imu_anomaly/recipe.py`), which
may import the modules beside it. It defines:

- `train(frame, settings) -> model`: fit on `frame`, an Arrow table from the
  loader, with `settings`, the free-form mapping the run was given;
- `score(model, frame) -> metrics`: a mapping of numbers, each named, over
  rows the model was not fitted on;
- optionally `save(model, directory) -> path`: write the model as a file the
  store takes, in a format `load_model` reads without running code, and return
  it. Without one, a mapping of numpy arrays is written as `.npz`, which the
  IMU example's agent reads; anything else needs a `save`.

Nothing else is asked of it: no base class, no registration, no decorator. The
frame carries a `label` column when the model is judged (`judged_on` on the
version the step inherits from, or `--labels` asked for): the effective label
of the source at each row's event time, null where none holds, as
`with_labels` gives it. A recipe that wants to train only on normal stretches
filters the column; one that scores itself against the labels reads it.

### A run is a subprocess with a timeout and a memory limit

Each run is one child process, `python -m wactorz.train.worker <run-dir>`,
started by whoever runs the step (the agent on main, or `wactorz-train` from a
terminal). The parent writes the run's specification into the run directory
under the state directory, `<state>/train-runs/<run id>/spec.json`, and reads
`result.json` back; the child's output goes to `log.txt` beside them, so a
failure's reasons are kept where a person finds them.

The child reads the window through `wactorz.data`, cuts the split, fits,
scores, saves the model into the run directory and writes the result. It
seeds `random`, numpy and torch (where installed) from the run's seed before
the recipe loads, and the seed is recorded. Before it imports anything heavy
it limits its own address space to the run's memory limit, where the platform
offers `resource` (Linux, macOS, the add-on); where it does not, the result
says the limit was not applied. The parent waits for the run's timeout, then
kills the child, and the run is recorded as failed for that reason. The
defaults are generous: an hour, and no memory limit, since a tight limit
trades one failure for another; a deployment sets both on the agent.

The child is a fresh interpreter, not a fork: it inherits no broker
connection, no event loop and none of main's memory, and it is killable. It
runs under the same user as main, with the same files, which is what phase 1
needs: a recipe is the operator's own code, deployed on purpose. A container
per run comes when generated code trains (below).

### What the stored version carries

The run stores the file `save` wrote with `ArtifactStore.add`, as the next
version of the model's name, and the metadata records everything a person or
a program needs to reproduce the run, judge it or keep its data:

| Key | What it holds |
| --- | --- |
| `trained_by` | `wactorz-train`, so a version made by hand is told from one the step made |
| `recipe`, `code_sha256` | The recipe's name and the hash of its source as it ran |
| `seed`, `settings` | What the run was given |
| `window` | `{source, since, until}` in event time: what was read |
| `data` | The windows the version learned from, `[{source, since, until}]`, which the retention sweep keeps (`data-storage.md`) |
| `split` | The split's kind and parameters, and each fold's manifest as the loader made it, so the split can be replayed |
| `rows` | How many rows were trained on and scored on |
| `metrics` | The new version's figures on the held-out rows, the parent's on the same rows, the metric compared, the margin asked for and the difference |
| `parent` | The version compared against, `name:V`, or null |
| `run_id`, `train_seconds` | The run, and how long the fit took |

`judged_on` is not metadata and is not the step's to declare: the version
inherits the previous one's, as every stored version does, and
`wactorz-artifacts judge` changes it.

Which rows the stored model was fitted on follows from the split. A time split
stores the model fitted before the cut and scored after it, which is the
version a regime change asks for: "retrain from 14:02", with the rows after it
held out. Several folds (rolling origin, leave-k-out) each fit and score a
model of their own, whose figures are averaged, and the stored version is then
fitted on every row of the window, since the folds have said what such a model
is worth. The manifest says which was done.

### The margin decides the move, and only the move

The step is told the pointer it is for -- `shadow` unless said, `active` when
asked, or none -- a metric, a margin, and which way is better. It scores the
parent version on the same held-out rows with the same `score`, and makes the
move only when the new version is better by at least the margin:

- the parent is the version `active` names, else the newest before this run;
- with no parent there is nothing to beat, the move is made, and the reason
  says so;
- the move's `reason` carries the figures, `f1 0.91 vs 0.88 (margin 0.02 met)`,
  so the feed and the history say why;
- a version that falls short is stored and not moved, and the result says by
  how much. Storing it is not a mistake: a version that lost is part of the
  record, and the next run's parent is still the one being served.

This is an offline check on held-out rows, and it decides only whether the new
version is worth trying. Whether a version on trial becomes `active` is the
promotion policy's decision (`promotion-policy.md`), made on what the trial
showed, not on what the recipe scored.

### What the run writes in the graph

Main writes the graph, as `labels-and-provenance.md` says, and the step runs
on main: the agent in main's process, or the CLI beside it on the same
machine, writing through the same database. Each run is:

- an `action` node, written `running` when the child starts and set to `done`
  or `failed` when it ends, with the recipe, window, split, settings, seed and
  run id in its payload, and the error when it failed;
- a `model` node for the version stored, named by the model's name, version
  and hash, with the run's metrics, joined `produced` from the action;
- a `trained` edge from every label whose window touches the training window
  of the source, so a label superseded later leads to the versions that
  learned from it;
- and, from then on, the trial's `outcome` is joined `evaluated_by` from the
  `model` node, since there is one to reach.

A run that failed is an action that failed, with no model: an action's status
gains `failed` for it, beside `cancelled`, which means something else.

### The step is an agent, and a command

The step is a library agent the package brings, `trainer`, spawned from the
catalogue on main with its recipe, model name, source, metric, margin, pointer,
settings and limits, like the recorder. A task to it runs one training:
`{"since": ..., "until": ..., "split": {...}, "settings": {...},
"reason": "..."}`, from chat, from a rule's `task` action, or from the
controller, each saying why; what it does not say takes the agent's defaults,
and the default window is the last week with its last fifth held out. The
agent runs one training at a time and refuses a second while one runs, since
two fits on one machine are each slower than one. `wactorz-train run` does the
same from a terminal, and `wactorz-train check` says what a recipe defines
before anything is fitted.

## What was decided against

- **Training in main's process, on a thread.** Rejected: the GIL makes a fit
  hold the loop more often than not, a thread cannot be killed, and a fit that
  exhausts memory takes main with it.
- **A forked worker.** Rejected: a fork inherits the broker connection and the
  event loop in a state neither can be used in, and on macOS it is unsafe
  under the libraries training uses.
- **A container per run.** Not now: a Pi and an add-on have no container
  runtime a run can lean on, and the recipe is the operator's own code. It
  comes with the designer, whose generated code is nobody's.
- **A base class the recipe subclasses.** Rejected: two functions are what a
  notebook already has, and a class would be copied from an example rather
  than written.
- **The recipe writing to the store itself.** Rejected: the child must not
  take the store's lock, move pointers or write the graph; it fits and scores,
  and the parent, which knows the pointer and the margin, does the rest.
- **Scoring the parent from its stored metrics.** Rejected: figures from
  another window are not comparable; the parent is scored again, on the same
  held-out rows, by the same function.
- **A move on a run that fell short, to a third pointer.** Rejected: two
  pointers are what the loop has (`model-pointers.md`); the version is stored,
  and the result says how it did.
- **Letting the step declare `judged_on`.** Rejected: what a model is judged
  on is about the model, declared once and inherited; a step that redeclared
  it per run would drift from what the trial reads.

## What follows

- **1.6, this step**: `wactorz/train/` (the specification, the recipe loader,
  the splits, the worker, the run and the graph record), the `trainer` agent,
  `wactorz-train`, `failed` on an action, and the `evaluated_by` edge from the
  model node to a trial's outcome.
- **1.6b, the promotion policy**, reads the trial and the policy and moves
  `active`; the step's margin is not its concern.
- **1.7, drift**, fires the step through a rule's `task` action: the baseline
  the controller is compared against.
- **1.8, the memory check**, runs `admit` before a training is placed on a
  node, once the step runs there.
- **1.9, the controller**, calls the step with a window and a split and
  writes the `decision` that led to it.
- **1.10, the designer**, runs k recipes or settings through the step and
  sends the winner to `shadow`, in a container once its code is generated.
