# Labels and the provenance graph

A decision record, not a guide: what a label is, how a later answer overrides
an earlier one, how the model loop records what it did and why, and how that
record is used when an answer changes. The graph's two tables, with its label
and question nodes, are built (`wactorz/core/labels.py`, `wactorz/core/provenance.py`);
the "What follows" section says which step builds each of the rest. Times are
event times, as `event-time.md` decides.

## The problem

`ask_operator` gets a person's answer to an agent, and the agent acts on it.
The answer itself is then lost as data: it sits in the agent's question book,
which keeps the newest answered questions and drops the rest, and nothing says
which readings it was about. A model cannot be trained on it, a controller
cannot learn from it, and a simulator cannot score a run against it.

Answers also change. An operator says "maintenance" at 14:31 and, an hour
later, "no, the new valve is faulty". By then the loop may have retrained a
model from 14:02, put it on trial as `shadow`, suppressed an alert and raised
a maintenance ticket. Some of those are now wrong, some are still right, and
nothing records which action followed from which answer.

And every shift the loop handles is an episode worth learning from: what was
seen, what the agent believed, what it asked, what it did, and how it turned
out. Kept as scattered log lines, none of that can be read back by a person
asking "why is v8 active?", let alone by an agent learning what to do next time.

## The decision

### One graph, two tables

The model loop records labels, decisions, actions and their results as nodes
of one graph, joined by edges from cause to effect. It lives in main's
database, in two tables beside the time-series tables:

```sql
CREATE TABLE prov_nodes (
    id        TEXT PRIMARY KEY,   -- uuid, minted by whoever writes the node
    kind      TEXT NOT NULL,      -- one of the kinds below
    ts        REAL NOT NULL,      -- event time the node is about, or its start
    ts_end    REAL,               -- where its window ends; NULL while open; ts for a moment
    recorded  REAL NOT NULL,      -- when main wrote it
    status    TEXT NOT NULL,      -- one of the statuses below
    source    TEXT DEFAULT '',    -- topic, entity or model it concerns
    actor     TEXT DEFAULT '',    -- operator, dashboard, rule:<name>, agent:<name>
    node      TEXT DEFAULT '',    -- the wactorz node that wrote it
    payload   TEXT NOT NULL       -- JSON: what the kind needs, below
);

CREATE TABLE prov_edges (
    src       TEXT NOT NULL,      -- the cause
    dst       TEXT NOT NULL,      -- the effect
    relation  TEXT NOT NULL,      -- one of the relations below
    recorded  REAL NOT NULL,
    PRIMARY KEY (src, dst, relation)
);
```

Walking from an answer to everything that followed from it is a recursive
query over `prov_edges`, which SQLite supports. No graph database is needed.
A window is kept in columns (`ts`, `ts_end`) rather than in the payload, so
"which nodes touch this stretch of time" is answered in SQL, whatever the
number of nodes. The table's own row order breaks a tie between two nodes
recorded at the same instant.
The shape maps onto the W3C PROV model (entities, activities, agents) should
the graph ever need to be exchanged in a standard form.

### Node kinds

| Kind | What it records | Payload holds |
| --- | --- | --- |
| `observation` | Something the loop noticed: a drift alarm, a changepoint, an anomaly above threshold | the detector, the statistic, the window |
| `question` | A question put to a person | the agent, the question id, its text and options |
| `label` | What a source was doing over a period, as someone said | window `start` and `end` (`end` open for "until further notice"), `value`, `channel`, `note`, the question id if it came from one |
| `decision` | What a decision-maker chose and why | the options weighed, the belief behind the choice where there is one |
| `action` | Something done: a retrain, a pointer move, a ticket, a notification, an alert raised or suppressed | what was done, to what, how to undo it if it can be undone |
| `model` | A version in the model store | name, version, sha256 |
| `outcome` | How something turned out | what was measured or said, and against what |

### Relations

Each edge points from cause to effect:

| Relation | From → to | Reads |
| --- | --- | --- |
| `led_to` | observation → question, label → decision, decision → action | the second happened because of the first |
| `answered_by` | question → label | the label came from the answer to the question |
| `trained` | label → model | the version was trained with this label |
| `produced` | action → model | the retrain stored this version |
| `superseded_by` | label → label | the later label replaces the earlier one, over the overlap |
| `evaluated_by` | action, decision or model → outcome | the outcome says how it turned out |

### Statuses

- A `label` is `valid`, `partly_superseded` or `superseded`.
- An `action` is `planned`, `running`, `done`, `cancelled` or `undone`.
- Any node derived from a superseded label is marked `invalidated` once a
  review (below) has decided it no longer holds; until then it keeps its
  status, and the graph shows that a review is pending.

Nodes are never deleted and never rewritten, apart from their status. What
changed is itself recorded as nodes and edges.

### A label covers a period of a source

A label says what a source was doing between two event times: "AHU-1 was
under maintenance from 14:02", "joint 3 was wearing from day 15". It is an
interval, not a point, because operators answer about periods. A label names a
concrete source (a topic or an entity), and may name the model it is meant
for; its `value` is one of the values that source's labelling allows.

### Answers become labels when the question says how

A question opts in by carrying a label spec: the source, the window it asks
about, and which option means which value. An option mapped to no value
("Not sure") makes no label. A sketch of the call, to be settled in its pull
request:

```python
answer = await me.ask_operator(
    "Every reading on AHU-1 looks unusual since 14:02. Did something change?",
    options=["Maintenance", "Real problem", "Not sure"],
    label={
        "source": "sensors/ahu-1",
        "start": shift_ts,
        "values": {"Maintenance": "maintenance", "Real problem": "fault"},
    },
)
```

Labels can also be written directly: by an agent (`agent.label(...)`), by a
rule action `label`, by an import from a maintenance system's work orders, and
by the simulator's operator persona.

### A later answer wins

A later label on the same source supersedes an earlier valid one wherever
their windows overlap. Later is by `recorded`, the time main wrote the label
(then by write order), never by the event time it is about and never by a
time stamped elsewhere: an agent's `answered_at` is kept in the question's
payload but does not decide, because a node whose clock runs ahead, or that
stamps a future time, would otherwise outrank every correction that follows.
Several answers from one book are written in the order they were answered,
so their own order holds. The earlier
label is not edited: it gets a `superseded_by` edge and the status
`superseded`, or `partly_superseded` when the new window covers only part of
it. What a source was doing at a moment is the newest valid label covering
that moment, and one query, `labels.effective(source, since, until)`, returns
those resolved intervals. "Maintenance from 14:02" followed by "a real
problem from 15:00" reads as maintenance from 14:02 to 15:00 and a fault after.

### An override produces a plan for review, not silent undos

When a label is superseded, the loop walks the graph down from it to every
decision, action and model that followed. It changes none of them on its own
authority. It builds a review: each affected node with a proposed handling,
from the decision-maker where there is one (the controller, deciding again
with the corrected label), else from defaults by kind:

| Affected node | Still planned or running | Already done |
| --- | --- | --- |
| Retrain | Cancel | Mark the version as trained on a superseded label |
| `shadow` set to that version | – | Clear `shadow` |
| `active` moved to that version | – | Roll back, to the version the pointer records as `previous` |
| Ticket or message | Cancel where the receiving system allows | Send a correction |
| Alert suppressed because of the old answer | – | Raise it again |

Some of what followed is still right, and the review says so: an action whose
reason did not depend on the overridden part of the label stays as it is.
Reversible changes inside the stream's policy, such as clearing a shadow or
cancelling a retrain that has not started, are applied at once. A rollback of
what is served, and anything that leaves wactorz (tickets, messages), waits for
a person's yes on the dashboard. Each change applied is a new `action` node,
`led_to` from the new label, so the history reads forward.

### Outcomes close the loop

A graph that records actions but never whether they were right teaches
nothing. Outcome nodes are written when results come in: a shadow trial won or
lost, a fault confirmed by a work order, false alarms after a promotion, an
operator overriding a decision (every decision that used the superseded label
gets an `outcome` saying so). These edges from actions to outcomes are what the
self-improvement levels learn from: experiment memory distils lessons from
episodes, the harness library keeps what worked with its evidence, and the
controller tunes its beliefs by how often they turned out right.

### Who may write, and what an answer proves

Main writes the graph. Agents on main write through the database; agents on a
node send their nodes and edges to main over the broker, signed with the node
key like the node's other messages, and main takes a write only about agents
that node speaks for: those main placed there, or, for an agent main has no
record of, the node whose heartbeat lists it, as a manifest is taken. A book
naming one of main's own agents counts only from that agent's own topic.

An answer that arrives on an agent's answer topic is not authenticated
(`security.md`): any client with broker access could have published it. A
label records the channel it came from, and one taken from the broker rather
than from the dashboard's own API is marked unverified. A review may act on an
unverified label only within what the stream's policy allows without asking.

## What was decided against

- **Editing a label in place when the answer changes.** It would lose what was
  said first, which is exactly what a review and a learner need.
- **A labels table apart from the graph.** Labels are causes of decisions and
  inputs to models; held apart, every link to them would be rebuilt later.
- **A graph database or a full ontology.** Two tables and a recursive query
  answer every question the loop asks; a standard form can be exported if ever
  needed.
- **Undoing every descendant of an overridden label automatically.** Some
  actions remain right, and some cannot be undone without a person, such as a
  ticket already worked on.
- **Keeping the record in each agent's state.** An agent's state lives where the
  agent runs and goes with it; the graph spans agents and nodes and has to
  outlive them.

## What follows

- **Labels** (built): both tables (a migration), label nodes with
  `superseded_by` edges, the label spec on `ask_operator`, `agent.label`, the
  rule action `label`, `labels.effective`, and `GET /api/labels`, with question
  nodes and `answered_by` edges.
- **Pointer moves** (built): each move of a model's `active` or `shadow` pointer is an
  `action` node, written by main when it sees the change (`wactorz/core/pointer_moves.py`),
  read back by `GET /api/models/{name}/moves`.
- **Hot swaps** (built): each running agent reloaded because a pointer it follows moved is
  an `action` node, `led_to` from the move (`wactorz/core/model_swaps.py`).
- **The train step** writes an `action` for each run, a `model` node for the
  version it stores, `produced` and `trained` edges, and its window and splits
  in the action's payload.
- **Shadow serving** writes the trial's `outcome`.
- **The controller** writes its `observation`, `decision` and `action` nodes,
  and builds reviews when a label is superseded.
- **The simulator** writes the same graph, with the scenario's ground truth as
  outcomes, which is training data for the learning levels before any plant
  provides it.
- **The dashboard** reads it for the events strip, model lineage, the Inbox's
  "what this answer led to", and a "why" view that draws the path behind any
  model or action; `GET /api/provenance/{id}` walks it up or down.
