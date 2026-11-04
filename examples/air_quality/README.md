# Room air quality

A model of what each room is usually like at each hour, an agent that notices
when a room is not, and a person asked what it was. Their answers become
labels, which is what a new version of the model is then judged against.

A simulated office stands in for real sensors: three rooms (a meeting room, an
open office and a kitchen), each with CO₂, temperature, humidity and PM2.5, a
reading every five minutes, people in from nine to five. On the Tuesday it
plays, three things go wrong:

| When (UTC) | Where | What |
| --- | --- | --- |
| 10:15–11:00 | Meeting room | a window left open during the meeting |
| 13:00–16:00 | Open office | the ventilation stops |
| 15:00–18:00 | Kitchen | the humidity sensor sticks at 99 % |

No language model and no Home Assistant are needed, and nothing heavier than numpy.

## What is in it

| File | What it does |
| --- | --- |
| `air_quality/plant.py` | the simulated building: the same readings on every run |
| `air_quality/model.py` | what is usual: per room and hour, the mean and spread of each quantity |
| `air_quality/train.py` | fits a healthy week and stores the model, with its threshold in the version's metadata |
| `air_quality/publish.py` | plays the Tuesday onto MQTT |
| `air_quality/agents.py` | `air-detect` scores each reading; `air-watch` asks about each episode |
| `air_quality/tab.py`, `panel.js` | the dashboard's Air tab |
| `pyproject.toml` | the entry points that bring the agents and the tab |

The score of a reading is how many spreads its furthest quantity is from that
hour's usual. That is the idea the example is about: 1200 ppm of CO₂ is
ordinary in a full meeting room at three in the afternoon, and far from normal
at three in the morning, so "normal" is learned per hour, not as one number.

## Run it

From the repository, in the environment Wactorz is installed in:

```bash
pip install -e examples/air_quality

# The model: v1, alarms past 5 spreads, made active.
python -m air_quality.train

# Wactorz, with this package's agents and tab, and no language model.
WACTORZ_EXTENSIONS=wactorz-example-air-quality wactorz --llm none
```

Open the dashboard (`http://localhost:8888`), and the **Air** tab. Then, in a
second terminal, from the same folder:

```bash
python -m air_quality.publish
```

It plays 07:00 to 19:00 at one five-minute step a second, about two and a half
minutes. Run `train` and `wactorz` from the same folder: the model is stored in
the state directory Wactorz runs with (`WACTORZ_STATE_DIR`, else `./state`).

## What to look at

![The Air tab after the Tuesday: a card per room, each with its latest readings and its score over the day, the threshold dashed across it and each fault shaded](air-tab.png)

1. **The Air tab.** A card per room: its latest readings, and its score over
   the day with the threshold across it. A room past the threshold for two
   readings in a row turns to *alert*; when it has settled for three, the
   stretch is shaded and the agent asks what it was.
2. **The Inbox.** One question per episode, such as *Meeting room, 10:15–11:00
   UTC: temperature (°C) was far from usual for the hour. What was it?*, with
   Window open, Ventilation fault, Sensor fault and Normal. Answer them; each
   answer is marked on the room's chart.
3. **The labels.** Each answer is now a label on its room over exactly that
   window: `GET /api/labels?source=kitchen`, or the room's history. "Normal" is
   a label too, so a false alarm can be counted as one.

## A stricter version, on trial

The labels are what a second version is judged against. Store one that alarms
earlier, at 4 spreads, put it on trial beside v1, and say which labels count as
a fault:

```bash
python -m air_quality.train --k-sigma 4 --set shadow
wactorz-artifacts judge air-quality:2 --positive window_open --positive ventilation_fault --positive sensor_fault
```

Play the day again and answer the questions. The **Models** tab
(`#/models/air-quality`) shows both versions on the same readings: the faults
each caught and how soon, and the false alarms. The threshold is in each
version's metadata (`k_sigma`), which the agent reads with
`me.artifacts["model"]["metadata"]`, so a version and the threshold it was
meant for travel together.

## Where to go from here

- Real sensors: publish `{"device": <room>, "ts": <seconds>, "co2": ..., "temperature": ...,
  "humidity": ..., "pm25": ...}` on `sensors/air/<room>`, and fit the model on a week of your own.
- Home Assistant sensors: a rule or a small agent republishing their states on those topics.
