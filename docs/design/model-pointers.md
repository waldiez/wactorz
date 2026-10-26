# The two pointers: `active` and `shadow`

A decision record, not a guide: what the pointers on the model store are, where they
live, what moves them, and what every later piece builds on. Nothing here is built yet;
the "What follows" section says which step builds each part. The guide for what is
shipped is the "Models kept on main" section of [deployment.md](../deployment.md).

## The problem

The store (`wactorz.core.artifacts`) keeps every version of a model and resolves a bare
name to the newest. That is the wrong default once models are retrained: a version just
stored is the one least known to be good. Two things are missing, and both are sticky
notes on the shelf rather than new shelves:

- which version agents serve, so that storing a new one changes nothing until someone
  decides it should, and going back is moving a note, not re-adding a file;
- which version is being tried beside it, seeing the same data, acted on by nobody.

## The decision

### Exactly two pointers, per name

Each stored name may carry an `active` pointer and a `shadow` pointer. Each names one
version of that name, or is unset. There is no third pointer and no user-named one: a
pointer is a role a version plays, and there are two roles. Anything else someone wants
to say about a version goes in its metadata, which is already free-form.

- **`active`** is the version agents serve. It moves when a person or a program decides.
  Rollback is moving it back; promotion is moving it to where `shadow` points.
- **`shadow`** is a version under trial: it sees the same input as `active`, its answers
  are recorded and compared, and nobody acts on them. At most one per name. It is cleared
  by a promotion and by an explicit clear.

A version cannot be both: setting `shadow` to the version `active` names is refused,
since a trial of a model against itself measures nothing; moving `active` onto the
version `shadow` names is a promotion and clears `shadow`.

### They live in the store's index, beside the versions

The pointers are part of the open store, in the same `index.json`, under a `pointers`
key beside `artifacts`, changed under the same lock. Any installed package reads and
moves them through `ArtifactStore`; the paid add-on moves them the same way and owns
nothing.

```json
{
  "format": 2,
  "artifacts": {
    "imu-detector": [{"version": 1, "...": "..."}, {"version": 2, "...": "..."}, {"version": 3, "...": "..."}]
  },
  "pointers": {
    "imu-detector": {
      "active": {
        "version": 2,
        "sha256": "…",
        "previous": 1,
        "moved_at": 1760000000.0,
        "by": "cli",
        "reason": "auc 0.95 on imu-2026"
      },
      "shadow": {
        "version": 3,
        "sha256": "…",
        "previous": null,
        "moved_at": 1760003600.0,
        "by": "agent:retrain-controller",
        "reason": "regime change at 14:02, retrained from there"
      }
    }
  }
}
```

A pointer records:

| Field | What it is |
| ----- | ---------- |
| `version` | The version it names. |
| `sha256` | The bytes that version held when the pointer was set. A version's bytes never change, but its number can be reused after a remove, so the pointer carries the hash the way a pinned spawn does, and a pointer whose version no longer holds those bytes is reported as dangling rather than followed. |
| `previous` | The version it named before this move, or null. Enough for a one-step rollback without any history; the history itself is kept in the provenance graph, below. |
| `moved_at` | When, as a Unix time, like `added_at` on a version. |
| `by` | Who moved it: `cli`, `chat`, `dashboard`, `agent:<name>`, `rule:<name>`, or an extension's name. Free text, like `metadata.source` on a version; recorded, never checked. Chat knows the channel a turn came from, not a person's name, so it records where, not who. |
| `reason` | Why, in the mover's words, or null. The train step writes its margin here; a person writes whatever they would say in a commit message. |

The index format becomes 2. A release that writes format 1 would read a format-2 index
and silently drop the pointers when it rewrote it, which is a model silently served
wrong; refusing the index outright, as the reader already does for a format it does not
know, is the better failure. The format-2 reader accepts a format-1 index as one with no
pointers and writes it back as format 2.

No history is kept in the index: it holds where each pointer is now and where it was.
Every move is one event on the feed, and one `action` node of the provenance graph in
main's database (`labels-and-provenance.md`): main writes it when its watcher sees the
change, whoever made it, and `GET /api/models/{name}/moves` reads the history back. That
is where "what happened to this model" is answered; the retained topics below say where
things stand.

### How a reference resolves

A reference is `NAME`, `NAME:latest`, `NAME:active`, `NAME:shadow` or `NAME:3`. One
rule, in `ArtifactStore.resolve`, used by every caller: spawn pinning, `load_model`,
the CLI, chat and the dashboard.

| Reference | Resolves to |
| --------- | ----------- |
| `NAME` | `active` when it is set; otherwise the newest version. |
| `NAME:active` | `active`; an error when it is not set. |
| `NAME:shadow` | `shadow`; an error when it is not set. |
| `NAME:latest` | The newest version, whatever the pointers say. |
| `NAME:3` | The third version. |

So `models={"model": "imu-detector"}` means "the one being served", as the plan asks,
and a store with no pointers behaves exactly as today: nothing changes for anyone until
they set a pointer. An agent that wants the newest regardless says `latest`, and an
agent that wants one version forever names it.

### What a pin remembers

Main pins a reference to name, version and hash when a spawn goes out, and that stays.
A pinned entry gains one optional field, `follows`: `"active"` when the spawn wrote a
bare name or `NAME:active`, `"shadow"` for `NAME:shadow`, absent when it named a version
or `latest`. A node ignores it; main uses it for two things:

- an agent restarted from its record, whose pin follows a pointer, is re-pinned to where
  that pointer is now rather than to the version it ran before. Following was what the
  spawn asked for, and a restart that revived the version everyone else had moved off
  would be the one agent serving the old model. While the pointer is unset there is
  nothing to follow, and the pin keeps the version it ran, as every pin did before
  there were pointers: a store with no pointers behaves as it always has;
- the hot swap (below) knows which agents to reload when a pointer moves: those whose pin
  follows it, and no agent that named a version.

### Moving a pointer

The store gets three operations, each one locked index change:

- `move(which, reference, *, by, reason=None)` sets `active` or `shadow` to the version
  the reference resolves to. The reference names a version, `latest` included; a bare
  name is refused, since it would mean the pointer being moved. `NAME:shadow` as the
  reference of an `active` move is a promotion.
- `clear(which, name)` unsets it, and returns what it was. Clearing `active` is allowed
  and explicit: the name goes back to meaning the newest. It takes no `by` or `reason`:
  there is no pointer left to record them on, and the feed line is the caller's to write.
- `promote(name, *, by, reason=None)` moves `active` to where `shadow` points and clears
  `shadow`, as one change, so no reader sees a moment with both on the same version.

Each returns the move as a record (name, pointer, from, to, hash, when, by, reason),
which is what gets published and shown. The rules around them:

- **A move is a change.** Moving a pointer to the version it already names is refused,
  so every move recorded is one; the one it was refused for keeps its `by` and `reason`.
- **A move is its own act.** `add` never moves a pointer; a version just stored is the
  newest and nothing more. The train step stores and then moves, in two steps, so a
  store followed by a refusal to move is two lines on the feed, not a version that
  appeared and quietly took over.
- **A version a pointer names cannot be removed** one at a time: `remove NAME:3` is
  refused with which pointer names it, and moving the pointer first is the answer.
  `remove NAME --every-version` removes the pointers with the name.
- **Who moves them.** A person, from the CLI, chat or the dashboard; or a program a
  person deployed: a rule action, an agent, an extension. Main's own model is not given a
  tool for it, in line with the rule that it cannot add, remove or change a stored
  model. The paid add-on's controller is an agent and moves them as one, with `by`
  naming it.
- **Where they are moved from.** The store directly, under its lock, from any process
  on main's machine, as `add` and `remove` already are; and through main, for anything
  that is not on that machine. Main is the one publisher of the topics below. A move
  made beside it by the CLI reaches the topics at main's next look at the index, which
  it takes on a short interval and before every spawn; a move made through main is
  published at once.

### How a move is heard

Two retained topics per name, published by main at QoS 1:

- `models/{name}/active`
- `models/{name}/shadow`

The payload is the pointer as it stands, with its name and which pointer it is:

```json
{"name": "imu-detector", "pointer": "active", "version": 2, "sha256": "…",
 "previous": 1, "moved_at": 1760000000.0, "by": "cli", "reason": "auc 0.95 on imu-2026"}
```

A cleared pointer is an empty retained payload, so a subscriber that comes up late sees
the same as one that was there. Main publishes both topics for every name at start, from
the index, so the broker always agrees with the store. The topics are read-only for
everyone but main: a node, an agent or a package that wants to move a pointer does it
through the store or through main, never by publishing here.

### How they are shown

- `wactorz-artifacts list` marks the version each pointer names; `show NAME` includes
  the pointers; `activate NAME:V`, `shadow NAME:V`, `promote NAME`, and
  `clear active|shadow NAME` are the commands, each taking `--reason`.
- `/artifacts activate|shadow|promote|clear …` in chat does the same, with `by` saying
  it came from chat; a social channel may list but not move.
- The summary main's model and the planner read says, per name, which version is
  active and which is shadow, so a request for "the detector" is wired to the one being
  served.
- The dashboard's model view shows both and lets a person move them; the feed shows
  every move with its `by` and `reason`.

### Hot swap: what a move does to a running agent

When a pointer moves, every running agent whose pin follows it is re-pinned to where the
pointer stands now and reloaded in place. The old version is served until the new one is
in memory; then the path and the loaded object change in one assignment. A version that
will not load leaves the agent serving what it served, and the feed says so with the
reason. Nothing restarts, and an agent that named a version is left alone. A pointer
cleared moves nobody: a pin keeps its version while there is nothing to follow. Both
pointers swap the same way, since a pin may follow `shadow` as well as `active`.

The program is told through one hook, `on_model(agent, key, model)`, called after the
swap has landed with the newly loaded object, or `None` for a model the program opens
itself through `artifact_path`. A program that reads `await agent.load_model(key)` on
each call needs no hook: the call returns the new version. One that kept what it loaded
in a local or in `agent.state` replaces it in the hook; without one it keeps serving the
old object, and the feed line says the host swapped while the program is what decides.
The IMU example's function reads through `load_model` and needs nothing.

On main, the pointer watcher hands each change to the swap directly. For an agent on a
node, main sends the node the new pin on a signed control topic
(`nodes/<node>/swap_model`) and the node answers on `nodes/<node>/swap_ack`, after
updating the spawn registry and the node's desired state, so the node may fetch the
version and a reboot runs it. A node that stayed up while main was away is sent the
version everyone else moved to on main's first look. A swap that landed is an `action`
node in the provenance graph, joined `led_to` from the move that caused it; one that
failed is a feed line and nothing in the graph.

Decided against:

- **Restarting the agent.** Rejected: the restart loses the agent's state and in-flight
  work, which is what the swap exists to keep.
- **Every read through `load_model`.** Rejected: slower for a program that would hold the
  object anyway, and still a change to the program; the hook is the change, made once.
- **A node following `models/<name>/<pointer>` itself.** Rejected: the topic carries the
  version and its hash, not the format or the size a node needs to fetch and check it,
  and anything may publish to a topic a node reads, while a command to a node is
  signed.
- **Recording a failed swap in the graph.** Rejected: nothing changed; the agent's
  record stays true, and the feed is where a failure is read.

## What was decided against

- **`latest` following `active`.** Rejected: `latest` keeps one meaning, the newest, so
  there is always a way to say that. The bare name is what changes meaning, and only
  once a pointer is set.
- **A history of moves in the index.** Rejected: the index would grow with every
  retrain, and an older release refuses an index format it does not know. The history
  is kept as actions in the provenance graph instead. `previous` covers the one case
  where the index itself needs to look back, a rollback.
- **More pointers, or named tags.** Rejected: two roles are what the loop needs, and a
  tag that means nothing to the system is metadata, which exists.
- **Pointers in a file of their own.** Rejected: one lock, one file, one export; and a
  pointer and the versions it names are changed together, under one lock, when a
  version is removed.
- **A move on `add`.** Rejected, above: the two acts stay two, so a train step can store
  what it made and still refuse to serve it.
- **Letting main's model move them.** Rejected: serving a model is an operator's
  decision or a deployed program's; a language model's judgement in a chat turn is
  neither.

## What follows

- **Step 4, item 5** builds the pointers and the topics as written here: the index
  format, `resolve`, the three operations, the refusals, `follows` on a pin, the CLI
  and chat commands, main publishing the topics. Built; the dashboard's model view is
  not part of it and waits for the dashboard work.
- **Item 7, hot swap**, re-pins and reloads the agents whose pin follows a moved pointer,
  on main and on nodes, as "Hot swap" above says. Built (`wactorz/agents/main/hot_swap.py`,
  `Actor.swap_model`, the runner's `swap_model` command, `wactorz/core/model_swaps.py`).
- **Item 8, shadow serving**, resolves `NAME:shadow` beside `active` and runs both; the
  pointer being unset is what turns the shadow path off.
- **Item 9, the train step**, stores a version, scores it, and calls `move` or not,
  writing its margin in `reason`.
- **Step 7, the paid add-on**, calls the same three operations as `agent:<name>` and
  reads the same topics.
