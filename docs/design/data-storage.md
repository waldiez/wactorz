# Data storage: where readings, pairs and serving logs are kept

A decision record, not a guide. It covers where the model loop's data lives,
what writes it, how it is read back, and what the database keeps. Nothing here
is built yet. The "What follows" section says which work package builds each
part. It replaces the open question the plan called decision 4, where shadow
pairs go, and settles the collector and `StreamWindow` follow-ups of
`event-time.md`.

## The problem

Every reading an agent sees today goes into main's SQLite database, one row per
field: `sensor_readings(ts, topic, entity_id, field, value, value_str, unit,
agent, node)`, written by the `timeseries-collector` agent in batches and
pruned by age. That is right for what it was built for, the dashboard's recent
history and an agent asking what a sensor did in the last hour. It is wrong for
what the model loop needs:

- **Rate.** An IMU at a hundred readings a second is six rows a reading, and a
  plant has many such sources. SQLite has one writer, holds one lock on one
  file, and stores rows, so every column of every row is read to get one
  column of a window.
- **Shape.** Training and drift want a frame: one row per reading, one column
  per field, in event time, for one source over a window. The long table has
  to be pivoted on every read.
- **Place.** Agents on nodes see readings main never does, and shipping each
  one over the broker to be stored on main costs the broker what the data
  costs, on a Pi.
- **Reproducibility.** A model is only as good as its record of what it was
  trained on. A row store pruned by age cannot say later what a training
  window held.

The shadow trial has the same problem in miniature: every input the serving
agent sees produces a pair of answers, and the comparison at the end of a trial
reads them all.

## The decision

### Readings from streams are recorded as Parquet files

A recorder takes the readings of a stream and writes them as Parquet files
under the state directory:

```text
<state>/data/<source>/<yyyy>/<mm>/<dd>/<hh>.parquet
```

`source` is what `event-time.md` calls a source, the thing whose readings are
one series: a topic, or a device named inside the payload, with `/` kept as
directory levels. One file per source per hour; a file is written whole when
it closes, never appended to, so a reader never sees a half-written file. The
recorder batches readings in memory and flushes a file when the hour turns or
the batch reaches a size; a flush that fails keeps the batch for the next.

A file holds one row per reading, one column per field the payloads carry, in
the order they arrived, with three columns the recorder adds:

| Column | Type | What |
| --- | --- | --- |
| `ts` | float64 | event time, Unix seconds, as `event-time.md` reads it from the payload |
| `arrived` | float64 | when the recorder received it, wall clock |
| `time_source` | string | `payload` when `ts` came from the payload, `arrival` when it is the arrival time |

Payload fields keep their names and their types as they came: a number is a
`float64`, a string a `string`, a boolean a `bool`, a nested object or a list
is a JSON string. A field that appears later in the stream appears in later
files only; the loader unifies the files' schemas with nulls, so a frame over a
window always has every column the window ever had.

The recorder is a catalogue agent, `recorder`, spawned on main or on a node
with the topics it records and, per topic, where the source is named in the
payload. It takes the place of the collector for the model loop; the collector
keeps its job, below.

### A node records where it is, and ships closed files to main

An agent on a node sees its readings there, so the recorder runs there too,
writing under the node's own state directory in the same layout. When a file
closes, the node sends it to main over the blob path that already carries
migration state and artifacts (`nodes/<node>/blob_return/<sha256>`), with a
small notice naming the source and the hour; main checks the hash and places
the file in its own layout. A file that did not arrive is sent again on the
next close, and the node keeps its copies until main has acknowledged them and
a retention has passed, so a node away for a day loses nothing. Readings cross
the broker once an hour, as one file, instead of once each.

Main's copy is the record. A frame is read from main; a program on a node that
wants its own recent data reads its node's files through the same loader.

### Shadow pairs and serving logs use the same layout

A serving agent running a shadow trial records one pair per input it saw, and a
serving agent asked to log its answers records one row per input. Both are
streams and go where streams go, under their own prefixes:

```text
<state>/data/shadow/<model>/<yyyy>/<mm>/<dd>/<hh>.parquet
<state>/data/serving/<model>/<yyyy>/<mm>/<dd>/<hh>.parquet
```

A pair's columns: `ts`, `arrived`, `time_source`, `input_sha256`, `source`,
`active_version`, `shadow_version`, `active_output`, `shadow_output` (JSON
strings when not scalar), `active_ms`, `shadow_ms`. A serving log's: the same
without the shadow columns. The input itself is not stored twice: the hash
names it, and the readings file for `source` and that hour holds it. This is
the answer to decision 4: pairs at a few per second and at a few hundred per
second are written the same way, by the same writer, and nothing is built twice
when a design partner's rate turns out higher than the first.

### SQLite keeps what is small and transactional

Main's database keeps the things that are written one at a time, under a
transaction, and read by key: labels, the provenance graph, questions, the
pointer history, agent state, chat. It also keeps `sensor_readings` as the
dashboard's live window: the collector goes on writing it, with the retention
shortened to what the overview and the trend panel show, and the event-time
columns `event-time.md` asks for (`arrived`, `time_source`) added by a
migration. Nothing an existing install does changes: an install without a
recorder behaves as today, with a smaller table.

The two stores are not mirrored. A reading in the window is also in a file
only if a recorder records that stream; the loader's fallback to the window,
below, is for the minutes before the current hour's file closes, not for
history.

### One loader, `wactorz.data`

Every consumer reads through one module, so the layout is known in one place
and a change to it is made once: the train step, drift, the shadow comparison,
the profiler, the simulator's report, and a notebook.

- `frame(source, start, end, columns=None)`: the readings of `source` over
  `[start, end)` in event time, as an Arrow table, read from the files whose
  hours overlap the window and filtered by `ts`; the last minutes come from
  the SQLite window when the current hour's file is not closed yet. Pairs and
  serving logs are read the same way, with `shadow/<model>` or
  `serving/<model>` as the source.
- `segments(frame, by=...)`: the frame cut into pieces, which every split groups
  on: `by="gap"` where event time jumps more than a threshold, `by="label"`
  along the intervals `labels.effective` gives for the source, `by="column"`
  on a column's value such as a device or a run id, `by="length"` into fixed
  spans.
- Splits, each returning index ranges and a manifest that says how the split
  was made, which the train step stores with the version:
  `time_split(frame, at)`, before and after one moment;
  `rolling_origin(frame, k)`, k forward folds, each trained on what came before
  it and scored on what came next;
  `leave_k_out(segments, k)`, k segments held out, the rest trained on, over
  every choice or a seeded sample of them.
  No split shuffles rows: a reading's neighbours in time are the signal the
  model is supposed to learn, and a shuffled split scores a model on what it
  has already seen.
- `with_labels(frame, source)`: a `label` column holding the effective label at
  each row's event time, from `labels.effective`, null where none holds.
- `profile(frame)`: the dataset card: row count, span, rate, gaps, and per
  column the type, count, missing, min, max, mean and a few quantiles. Served
  as `GET /api/data/{source}/profile` and `wactorz-data profile <source>`.
- `export(frame, path)`: one Parquet file, for a notebook or a hand-off.

The loader returns Arrow tables and numpy arrays, not a dataframe library's
objects: a consumer that wants pandas or polars converts at its edge.

### Dependencies

pyarrow is the data package's one dependency, for writing and reading Parquet
and for the dataset reads with filters. It is declared as the `data` extra,
`wactorz[data]`; the recorder and the loader say so when it is missing, and
nothing in core imports them. numpy stays the only required dependency of the
model loop's core. Nothing else: SQLite and Parquet are the two stores, and
there is no third.

## What was decided against

- **Readings in SQLite.** Rejected, above: one writer, one lock, rows not
  columns, and a pivot on every read. It keeps the live window, which is what
  it is good at.
- **A time-series database** (TimescaleDB, InfluxDB, QuestDB). Rejected: another
  service to run on a Pi and to package into the add-on, another network path
  to secure, and a schema the loader would have to pivot anyway. Parquet files
  are a directory, back up with the state directory, and open in every tool.
- **DuckDB for reading the files.** Rejected for now: pyarrow's dataset reads
  with filters cover every read the loop makes, numpy covers the rest, and a
  Pi install stays small. The profiler may add it as an optional extra if a
  history outgrows memory; nothing in the layout would change.
- **CSV.** Rejected: no schema, no types, no column reads, and a file that is
  appended to is a file a reader can see half-written.
- **A file per day rather than per hour.** Rejected: an hour is the longest a
  reading waits before a node ships it and before it is readable on main, and
  a day of a fast source is a file too large to write in one go on a Pi.
- **Shipping readings to main one by one over the broker.** Rejected: it costs
  the broker what the data costs, and the broker carries control traffic that
  must not queue behind it.
- **Storing pairs in SQLite because they are fewer.** Rejected: they are fewer
  today, and the writer would be built twice.
- **Random splits.** Rejected, above: a time-series model scored on its own
  neighbours scores well and means nothing.
- **A dataframe library in the API.** Rejected: it would be a required
  dependency of everything downstream, and the choice between pandas and
  polars is the consumer's.

## What follows

- **1.6a, the data package**, builds the recorder on main and on a node, the
  shipping of closed files, the loader with its segments, splits, labels join,
  profile and export, the profile route and CLI, the `data` extra, the
  collector's event-time columns and shortened retention, and the
  `StreamWindow` event-time option.
- **1.5, shadow serving**, writes pairs under `shadow/<model>` through the same
  writer, and reads them back through `frame` for the comparison. It may build
  the writer first if it lands before 1.6a.
- **1.6, the train step**, reads its window through `frame`, takes a split from
  the loader, and stores the split's manifest and the files' hashes with the
  version, which is the data manifest `model-pointers.md` asks for.
- **1.7, drift**, reads windows through `frame` in event time.
- **1.3, the simulator**, replays through the recorder, so a scenario's run is
  stored as a plant's would be and its report reads through the loader.
- **1.10, the profiler**, grows the dataset card and adds sampling over large
  histories, with DuckDB as an optional extra if it is needed by then.
