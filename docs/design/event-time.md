# Event time in the model loop

A decision record, not a guide: which clock the model loop reads, where that
time comes from, and what has to change for it. Labels and the provenance graph
are kept in event time (`wactorz/core/labels.py`); the rest is not built yet,
and the "What follows" section says which step builds each part.

## The problem

Every part of the model loop asks "when": the window a model is trained on, the
split between training and validation, the period a label covers, the moment a
shift began, the day a shadow trial ran. Today the answer is the clock of
whoever handles the message:

- the time-series collector stores each reading with the time it arrived
  (`time.time()` when the message is routed) and drops a `ts` or `timestamp`
  field the payload carried;
- a `StreamWindow` stamps each entry with the wall clock when it is pushed, and
  trims by the wall clock.

That is right for a live dashboard and wrong for everything that follows.
Messages arrive late: an edge node reconnects and flushes an hour of readings,
a bridge batches, a broker queues. Stamped on arrival, an hour of data lands in
one second, a shift appears where the link came back, and a label that says
"since 14:02" points at readings that were stored at 15:10.

And a replay cannot work at all. The simulator streams recorded data faster
than it happened; stamped on arrival, thirty days of a plant become a few
hours, and every window, split and label is computed in the wrong time.
The only alternative would be a virtual clock threaded through every actor.

## The decision

### The model loop reads event time

Event time is when the thing measured happened, as the payload says. Every
time the model loop stores, compares or reasons about is event time:

- readings and features, as stored for training and drift;
- training windows and the train, validation and test splits;
- the window a label covers;
- shift events, changepoints and drift windows;
- shadow pairs, scores and the comparison between versions;
- every node of the provenance graph (see `labels-and-provenance.md`).

Arrival time is still recorded beside it where it is useful (to see lag, to
spot a stalled link), but nothing in the loop decides by it.

### Where it comes from

In order, the first that is present:

1. a numeric `ts` field in the payload: Unix seconds, fractional allowed;
2. a `timestamp` field: Unix seconds, or an ISO 8601 string with an offset;
3. the arrival time, marked as such (`time_source: "arrival"`), so a consumer
   can tell a reading whose time was measured from one whose time was guessed.

Batched frames (one message carrying many samples, as high-rate sensors send)
carry a start time and a sampling interval, or a time per sample; each sample
gets its own event time.

### Clock trouble is visible, not silent

A payload time far in the future of its arrival (beyond a tolerance, by default
a few minutes) is a device clock that is wrong. It is stored with its arrival
time instead and marked `time_source: "arrival"`, and the source is reported
once on the feed. A payload time older than the store's retention is dropped
and counted. Neither stops the stream.

### "Now" is per source

Where the loop needs "now" (to close a window, to decide a trial has run long
enough, to time out a cooldown), it uses the newest event time seen on that
source, held back by an allowed lateness so that data arriving a little late
still lands in its window. During a live run that is the wall clock less a
small delay; during a replay it is the replay's own time. The same code serves
both, and the simulator needs no clock of its own inside the actors.

Timeouts that concern people, such as how long to wait for an operator's
answer, stay in wall-clock time: a person answers in real minutes, replayed or
not. The simulator speeds those up through its operator persona, not through
the clock.

## What was decided against

- **A virtual clock injected into every actor.** It would make replays work,
  but every agent, catalogue program and line of generated code would have to
  ask it instead of `time.time()`, and one that forgot would be wrong silently.
  Event time carried in the data needs nothing from the code that only passes
  it along.
- **Arrival time everywhere, with a lag correction.** Lag is not constant: a
  reconnecting node delivers an hour at once. No single correction recovers
  when each reading was taken.
- **Trusting payload times without a check.** A device with a reset clock would
  place its readings in 1970 or next year, and a training window would silently
  lose them.

## What follows

- **The collector** stores the event time in `ts` and the arrival time in a new
  column, with `time_source`; a schema migration adds the column. Readings
  without a payload time behave as today.
- **`StreamWindow`** gains an option to window by event time; the default stays
  the wall clock, so existing agents are unchanged.
- **Labels and the provenance graph** (built) are stored in event time
  (`labels-and-provenance.md`).
- **The train step** takes its window and splits in event time, and records
  them with the version it stores.
- **The simulator** replays recorded data with its original times in the
  payloads, at any speed, through the same collector and agents.
- **High-rate frames** are defined with the high-rate recorder, when the first
  design partner's rates need it.
