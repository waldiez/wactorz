# Event time in the model loop

A decision record, not a guide: which clock the model loop reads, where that
time comes from, and what has to change for it. Labels and the provenance graph
are kept in event time (`wactorz/core/labels.py`), the recorder files readings
by it, the train step takes its window in it, and the loop's "now" is each
source's own (`wactorz/data/clock.py`); the "What follows" section says what
each step built and what is still open.

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

Built as `wactorz.data.now_of`: the newest `ts` in the source's newest hour
of Parquet, less a minute (`lateness_s`, an option of the agents that read
it), and the wall clock while nothing is recorded for the source, so a loop
without a recorder behaves as before. It is read where the loop asks "now":

- the `drift` agent ends its current window there, so a replayed day is
  checked at the replay's own moments;
- the `trainer` ends its default window there, so "the last week" is the
  last week the source saw;
- the promotion policy counts a trial's days as the event time its pairs
  cover (the trial summary's `span`), a replayed day being a day however fast
  it was played; a trial with no pairs yet counts the wall clock since the
  shadow was set;
- a rule's cooldown counts the event time of the triggers it fires on, so a
  cooldown of a day is a replayed day. A trigger without a time counts when
  it arrived.

The cadences stay on the wall clock: how often the drift agent checks and
main holds a trial to its policy is how often the machine looks, not how far
the data has come.

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

- **The recorder** (built) writes the event time in `ts`, the arrival in
  `arrived` and `time_source` to Parquet; where each store is used is decided
  in `data-storage.md`. The collector that serves the dashboard's charts still
  stores arrival time, with a payload time kept beside it, since its charts
  are of what was seen when; a schema migration adds the column when a chart
  needs event time.
- **`StreamWindow`** gains an option to window by event time; the default stays
  the wall clock, so existing agents are unchanged.
- **Labels and the provenance graph** (built) are stored in event time
  (`labels-and-provenance.md`).
- **The train step** (built) takes its window and splits in event time, and
  records them with the version it stores.
- **"Now" per source** (built, above): the drift agent's window, the
  trainer's default window, the policy's days and a rule's cooldown.
- **The simulator** replays recorded data with its original times in the
  payloads, at any speed, through the same recorder and agents, which the
  above makes possible: nothing in the loop reads the machine's clock to
  place the data.
- **High-rate frames** are defined with the high-rate recorder, when the first
  design partner's rates need it.
