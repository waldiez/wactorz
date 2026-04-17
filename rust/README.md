# Wactorz — Rust workspace

Async actor system and HTTP/WebSocket server, written in Rust.
Mirrors the Python backend semantics while offering a lower-footprint native binary.

## Crate map

```
rust/
└── crates/
    ├── wactorz-core          # Actor trait, message types, registry, supervisor
    ├── wactorz-mqtt          # Async MQTT client wrapper + topic constants
    ├── wactorz-agents        # 20+ concrete agent implementations
    ├── wactorz-interfaces    # REST API (axum), WebSocket bridge, CLI REPL
    └── wactorz-server        # Binary entry point — wires everything together
```

### Dependency graph

```
wactorz-server
    └── wactorz-interfaces
    └── wactorz-agents
            └── wactorz-mqtt
                    └── wactorz-core
```

## Quick start

```bash
# From repo root
make dev-rust          # mosquitto in Docker + Rust server natively (port 8080)
make dev-rust-full     # + Fuseki triplestore
make dev-rust-check    # smoke-test a running server
make dev-rust-down     # stop Docker services
```

Or run directly:

```bash
cd rust
cargo run -p wactorz-server -- \
  --llm-provider anthropic \
  --llm-api-key $ANTHROPIC_API_KEY \
  --mqtt-host localhost \
  --no-cli
```

## Key CLI flags

| Flag | Default | Description |
|------|---------|-------------|
| `--mqtt-host` | `localhost` | MQTT broker host |
| `--mqtt-port` | `1883` | MQTT broker port |
| `--api-addr` | `127.0.0.1:8080` | REST + WS listen address |
| `--llm-provider` | `anthropic` | `anthropic` / `openai` / `ollama` / `nim` |
| `--llm-model` | _(provider default)_ | Model name |
| `--llm-api-key` | env `LLM_API_KEY` | API key |
| `--ha-url` | — | Home Assistant base URL |
| `--ha-token` | env `HA_TOKEN` | Long-lived HA access token |
| `--fuseki-url` | `http://localhost:3030` | Apache Jena Fuseki URL |
| `--fuseki-dataset` | `wactorz` | Fuseki dataset name |
| `--static-dir` | `static/app` | Serve SPA from this directory |
| `--no-cli` | off | Disable interactive REPL (use in containers) |

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness probe |
| `GET` | `/api/actors` | List all registered actors |
| `GET` | `/api/actors/{id}` | Single actor detail |
| `POST` | `/api/actors/{id}/pause` | Pause an actor |
| `POST` | `/api/actors/{id}/resume` | Resume an actor |
| `POST` | `/api/actors/{id}/stop` | Stop an actor |
| `GET` | `/api/config` | Non-secret runtime config (seeds frontend localStorage) |
| `POST` | `/api/fuseki/{dataset}/sparql` | SPARQL SELECT/ASK proxy |
| `POST` | `/api/fuseki/{dataset}/update` | SPARQL INSERT/DELETE proxy |
| `GET` | `/ws` | WebSocket bridge (MQTT ↔ browser) |
| `GET` | `/*` | SPA fallback (ServeDir) |

## Build

```bash
# Debug (fast compile)
cargo build -p wactorz-server

# Release (optimised — LTO + strip)
cargo build --release

# Or via Makefile
make build-rust
```

## Test

```bash
cargo test                    # all crates
cargo test -p wactorz-core    # single crate
make test-rust
make parity                   # Python ↔ Rust supervisor parity check
```

## Lint / format

```bash
cargo fmt                     # format
cargo clippy -- -D warnings   # lint (warnings are errors)
make fmt
make lint
```

## Adding a new agent

See [CONTRIBUTING.md](CONTRIBUTING.md#adding-a-new-agent).

## Supervisor tree (NATO alphabet)

| Name | Agent | Protected |
|------|-------|-----------|
| alpha | main | yes |
| bravo | monitor | yes |
| charlie | io-agent | yes |
| delta | installer | yes |
| echo | code | no |
| foxtrot | manual | no |
| golf | home-assistant | no |
| hotel | weather | no |
| india | fuseki | no |
| juliet | catalog | yes |
| kilo | ha-actuator | no |
| lima | ha-state-bridge | no |
