# Wactorz

**Actor-model multi-agent AI framework — Python MQTT**

Spawn, coordinate and monitor AI agents running 24/7 with Erlang/OTP-style supervision,
and real-time MQTT telemetry.

## Quick install

```bash
pip install wactorz[all]
```

> **Install directly from GitHub (latest dev build):**
>
> ```bash
> pip install "wactorz[all] @ git+https://github.com/waldiez/wactorz.git"
> ```

## Navigation

- **[Guide](development.md)** — Installation, configuration, deployment
- **[Architecture](architecture.md)** — Actor model, supervision trees, message flow
- **[Agents](agents.md)** — Built-in and custom agent reference
- **[Auto-Wiring](mqtt_auto_wiring.md)** — TopicBus, contracts, and schema-aware planning
- **[Interfaces](interfaces.md)** — CLI, REST, MCP, chat platforms, and dashboard
- **[Prometheus Monitoring](prometheus.md)** — Python metrics, Prometheus, and optional dependency probes
- **[Pipelines](pipelines.md)** — Reactive rules, canonical patterns, planner workflow
- **[MQTT Topics](mqtt_topics.md)** — Full topic reference with payload schemas
- **[Remote Nodes](remote-nodes.md)** — Edge deployment via `remote_runner.py`
- **[Python API](python-api.md)** — Core classes, supervision, persistence
- **[Home Assistant Addon](../ha-addon/DOCS.md)** — Install and configure the HA Supervisor addon (requires HAOS or Supervised)
<!-- - **[Rust Docs](https://waldiez.github.io/wactorz/api/rust/)** — Rustdoc for wactorz-core and wactorz-interfaces
- **[JS/TS Docs](https://waldiez.github.io/wactorz/api/js/)** — TypeDoc for the Babylon.js frontend -->

## Links

[GitHub](https://github.com/waldiez/wactorz) ·
[PyPI](https://pypi.org/project/wactorz/) ·
[Issues](https://github.com/waldiez/wactorz/issues) ·
[Apache-2.0](https://github.com/waldiez/wactorz/blob/main/LICENSE)
