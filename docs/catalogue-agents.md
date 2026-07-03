# Catalogue agents

The **catalogue** is Wactorz's library of ready-made agents. Instead of writing code,
you spawn a pre-built recipe by name and it starts running under supervision. The
`CatalogAgent` loads every recipe at startup and tells the main agent what's available,
so a plain-language request can auto-spawn the right one — or you can spawn it yourself.

Each catalogue agent has its own page in this section (see the sidebar).

## Spawning

```text
@catalog list                    # show every available recipe
@catalog info gmail-agent        # show one recipe's schema
@catalog spawn gmail-agent       # start it
```

Or just ask the main agent in natural language ("check my unread email", "what's the
weather in Athens?") — it discovers the catalogue by capability and spawns as needed.
A spawned agent is saved to the spawn registry and restored automatically on restart.

## Available agents

| Agent | What it does | Setup |
|-------|--------------|-------|
| [Google Calendar](catalogue-google-calendar.html) | List, create, and delete calendar events | OAuth |
| [Gmail](catalogue-gmail.html) | Search/read mail, labels, drafts (never sends) | OAuth |
| [Weather](catalogue-weather.html) | Current conditions, forecast, history (Open-Meteo) | none |
| [Smart Energy](catalogue-smart-energy.html) | Smart-plug monitoring, kWh/cost, guarded auto-off | Home Assistant |
| [Anomaly Detector](catalogue-anomaly-detector.html) | Learns normal patterns, flags anomalies in real time | MQTT data |
| [Device Manuals](catalogue-manual.html) | Finds a device manual and answers questions about it | none |
| [Doc → PPTX](catalogue-doc-to-pptx.html) | Converts a PDF/TXT into a PowerPoint deck | none |
| [Time-Series Collector](catalogue-timeseries.html) | Records MQTT data to SQLite for history/ML | MQTT data |

**Native** agents (Weather, Calendar, Gmail) are Python `Actor` subclasses spawned
directly. **Dynamic** agents ship as recipe code that's spawned as a `DynamicAgent`; any
declared dependencies are `pip`-installed on first spawn.

## Adding a recipe

Create `wactorz/catalogue_agents/my_agent.py` exporting `AGENT_CODE = r'''…'''`, then add
an entry to `_build_catalog()` in `wactorz/agents/catalog_agent.py`. It becomes available
on the next restart. See the [Agents reference](../guide/agents.html) for the DynamicAgent
API and code-safety rules.
