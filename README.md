<p align="center">
  <img src=".github/assets/logo.svg" width="120" alt="Wactorz" />
</p>
<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)"  srcset=".github/assets/title-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset=".github/assets/title-light.svg">
    <img src=".github/assets/title-dark.svg" width="320" alt="Wactorz" />
  </picture>
</p>

<p align="center"><strong>AI agents that don't stop when you close the tab.</strong></p>

<p align="center">
<a href="https://docs.waldiez.io/wactorz/">Docs</a> |
<a href="https://docs.waldiez.io/wactorz/guide/development.html">Installation</a> |
<a href="docs/architecture.md">Architecture</a> |
<a href="ha-addon/DOCS.md">Home Assistant Addon</a> |
<a href="https://github.com/waldiez/wactorz/issues">Issues</a>
</p>

<p align="center">
<a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License"/></a>
<a href="https://python.org"><img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python"/></a>
<a href="https://mosquitto.org"><img src="https://img.shields.io/badge/transport-MQTT-purple.svg" alt="MQTT"/></a>
<a href="ha-addon/DOCS.md"><img src="https://img.shields.io/badge/Home%20Assistant-addon-41BDF5.svg" alt="Home Assistant"/></a>
</p>

---

Wactorz runs LLM-driven agents as long-lived actors on the hardware you already have - a Raspberry Pi in the garage, an old laptop, a VM in your closet. You describe what you want in chat; the planner writes the Python, spawns it on a node, and supervises it. When an agent crashes, only that one restarts. State persists across restarts and you can move an agent to a different machine without losing it.

It runs on MQTT, so anything happening inside the system surfaces as a topic external code can subscribe to. Home Assistant talks to it the same way Discord and Telegram do - it's one channel among several, alongside a REST API and an MCP server. The LLM provider is configurable (Anthropic, OpenAI, Gemini, NIM) or fully local via Ollama for offline use.

---

## Quick Start

```bash
git clone https://github.com/waldiez/wactorz
cd wactorz
pip install -e ".[all]"

# Start the MQTT broker
docker compose up -d mosquitto

# Set your provider, model, and key (or put them in .env)
export LLM_PROVIDER=anthropic   # anthropic | openai | ollama | nim | gemini
export LLM_MODEL=claude-sonnet-4-6
export LLM_API_KEY=your-key-here

python -m wactorz
```

Dashboard: `http://localhost:8888`.

If you'd rather skip the clone, [pull the image from Docker Hub](docs/dockerhub.md). To run without an API key, use Ollama:

```bash
ollama pull llama3
python -m wactorz --llm ollama --ollama-model llama3
```

Windows setup is in [docs/windows.md](docs/windows.md); the full set of deployment options lives in [docs/deployment.md](docs/deployment.md).

---

## Example prompts

```text
when a person is detected in my pc camera, open the office light
when the door opens, make reachy wakeup
when the light has been on for too long, send me a discord notification
```

---

## Architecture

```mermaid
flowchart LR
    User["User<br/>CLI, REST, Discord, Telegram, HA"] --> Main["MainActor<br/>intent routing"]

    Main --> Actuate["OneOffActuatorAgent<br/>direct service calls"]
    Main --> Planner["PlannerAgent<br/>pipeline planning"]
    Main --> HA["HomeAssistantAgent<br/>REST + WebSocket"]
    Main --> Chat["LLM reply<br/>streaming response"]

    Planner --> Dynamic["DynamicAgents<br/>LLM-generated runtime code"]
    Actuate --> Bus["MQTT broker"]
    HA --> Bus
    Dynamic --> Bus

    Bus --> Dashboard["Live dashboard<br/>agents, logs, cost, heartbeats"]
    Bus --> Remote["Remote nodes"]
    Bus --> External["Sensors, services, and IoT systems"]
```

---

## Interfaces

| Interface | How to use it |
|---|---|
| CLI | `python -m wactorz` |
| Live dashboard | `http://localhost:8888` |
| REST API | `python -m wactorz --interface rest` |
| Discord | `python -m wactorz --interface discord` |
| Telegram | `python -m wactorz --interface telegram` |
| MCP server | `wactorz-mcp` |
| Flutter app | iOS/Android companion app for agents, chat, and activity feed |
| Home Assistant addon | One-click install inside the HA Supervisor |

---

## LLM Configuration

Set these three env vars in `.env` or export them in your shell:

```bash
# Options: anthropic | openai | ollama | nim | gemini | none
LLM_PROVIDER=anthropic

# Model ID — examples:
#   anthropic  →  claude-sonnet-4-6
#   openai     →  gpt-4o
#   ollama     →  llama3
#   nim        →  meta/llama-3.3-70b-instruct
#   gemini     →  gemini-2.5-flash
LLM_MODEL=claude-sonnet-4-6

# Generic key — used for anthropic / openai / nim / gemini
# For Ollama, set OLLAMA_URL instead (default: http://localhost:11434)
LLM_API_KEY=your-key-here
```

---

## Repository Map

| Path | What lives there |
|---|---|
| `wactorz/` | Python actor runtime, built-in agents, interfaces, monitoring, HA integration |
| `frontend/` | Vite + TypeScript + Babylon.js dashboard |
| `rust/` | Rust backend crates and MQTT/interface support |
| `mobile/` | Flutter companion app |
| `ha-addon/` | Home Assistant Supervisor addon |
| `docs/` | Markdown docs source |
| `infra/` | Mosquitto, Prometheus, OpenTelemetry, Fuseki, nginx, and HA configs |
| `tests/` | Python test suite and backend parity harness |

---

## Documentation

| Start here | For |
|---|---|
| [Quickstart](docs/quickstart.md) | First run and Windows setup |
| [Docker Hub](docs/dockerhub.md) | Run from Docker without cloning the repo |
| [Architecture](docs/architecture.md) | Actor system, supervision, MQTT flow |
| [Agents](docs/agents.md) | Built-in agents, recipes, and dynamic agents |
| [Pipelines](docs/pipelines.md) | Reactive automation patterns |
| [Remote nodes](docs/remote-nodes.md) | Edge deployment over SSH |
| [Interfaces](docs/interfaces.md) | CLI, REST, chat platforms, dashboard, MCP |
| [API reference](docs/api.md) | REST endpoints and payloads |
| [Deployment](docs/deployment.md) | Docker, native binary, systemd, staging, HA addon |
| [Prometheus](docs/prometheus.md) | Metrics and monitoring |
| [Technical reference](docs/reference.md) | Deeper internals |

---

## Contributors

<!-- ALL-CONTRIBUTORS-LIST:START - Do not remove or modify this section -->
<!-- prettier-ignore-start -->
<!-- markdownlint-disable -->
<table>
  <tbody>
    <tr>
      <td align="center" valign="top" width="14.28%">
        <a href="https://github.com/ounospanas">
          <img src="https://avatars.githubusercontent.com/u/29335277?v=4" width="100px;" alt="Panagiotis Kasnesis"/>
          <br /><sub><b>Panagiotis Kasnesis</b></sub>
        </a>
        <br />
        <a href="#projectManagement-ounospanas" title="Project Management">📆</a>
        <a href="https://github.com/waldiez/wactorz/commits?author=ounospanas" title="Code">💻</a>
      </td>
      <td align="center" valign="top" width="14.28%">
        <a href="https://github.com/lazToum">
          <img src="https://avatars.githubusercontent.com/u/4764837?v=4" width="100px;" alt="Lazaros Toumanidis"/>
          <br /><sub><b>Lazaros Toumanidis</b></sub>
        </a>
        <br />
        <a href="https://github.com/waldiez/wactorz/commits?author=lazToum" title="Code">💻</a>
        <a href="#design-lazToum" title="UI & Design">🎨</a>
      </td>
      <td align="center" valign="top" width="14.28%">
        <a href="https://github.com/hchris0">
          <img src="https://avatars.githubusercontent.com/u/23460824?v=4" width="100px;" alt="Chris"/>
          <br /><sub><b>Chris</b></sub>
        </a>
        <br />
        <a href="https://github.com/waldiez/wactorz/commits?author=hchris0" title="Code">💻</a>
        <a href="#userTesting-hchris0" title="User Testing">📓</a>
      </td>
      <td align="center" valign="top" width="14.28%">
        <a href="https://github.com/amaliacontiero">
          <img src="https://avatars.githubusercontent.com/u/29499343?v=4" width="100px;" alt="Amalia Contiero"/>
          <br /><sub><b>Amalia Contiero</b></sub>
        </a>
        <br />
        <a href="https://github.com/waldiez/wactorz/commits?author=amaliacontiero" title="Code">💻</a>
        <a href="#promotion-amaliacontiero" title="Promotion">📣</a>
      </td>
    </tr>
  </tbody>
</table>
<!-- markdownlint-restore -->
<!-- prettier-ignore-end -->
<!-- ALL-CONTRIBUTORS-LIST:END -->

Contributions of any kind are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) to get started.

---

## Contributing

| What | How |
|---|---|
| Found a bug | [Open an issue](https://github.com/waldiez/wactorz/issues/new?template=bug_report.yml) |
| Have an idea | [Start a discussion](https://github.com/waldiez/wactorz/discussions) |
| Want to code | Fork, branch, and open a PR against `main` |
| Docs, tests, UI | Same drill, open a PR |
| New agent recipe | Add it in `wactorz/catalogue_agents/` and open a PR |
| Home Assistant | HA integrations and addon config PRs are very welcome |

Read [CONTRIBUTING.md](CONTRIBUTING.md) for setup instructions, code style, and the PR process.

---

## License

[Apache 2.0](LICENSE). Free to use, modify, and distribute.
