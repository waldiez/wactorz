# Architecture

## Overview

Wactorz is an async, multi-agent orchestration system built on the **Actor Model** with **MQTT** as the communication backbone.  Every agent is an independent actor with its own message inbox; no shared mutable state exists between actors.

```
┌────────────────────────────────────────────────────────────────────┐
│  Browser                                                           │
│  Babylon.js SPA  ←──WebSocket──►  nginx  ←─── /ws  ──►           │
│                  ←──REST──────►          ←─── /api/ ──►           │
│                  ←──MQTT/WS───►          ←─── /mqtt ──►           │
└──────────────────────────────────────────────────────────────────┘
                                    │
                             nginx  │  (single public entry point)
                              /:80  │
                                    │
          ┌─────────────────────────┼──────────────────────────────┐
          │                         │                              │
          ▼                         ▼                              ▼
  wactorz (Python)        Mosquitto (MQTT)              Fuseki (RDF)
  :8080 REST                :1883 TCP / :9001 WS          :3030 SPARQL
  :8081 WS bridge
  ┆ Rust in-sync ┆
          │
          │  pub/sub via MQTT
          ├── MainActor        (LLM orchestrator)
          ├── MonitorAgent     (health watchdog)
          ├── IOAgent          (UI gateway)
          ├── QAAgent          (safety observer)
          ├── NautilusAgent    (SSH / rsync bridge)
          ├── UDXAgent         (built-in knowledge base)
          └── DynamicAgent*   (LLM-generated scripts, spawned at runtime)
```

---

## Components

### wactorz backend

The backend is **Python-first**: the Python implementation is the primary runtime.  A Rust implementation mirrors the same actor model and API contract and may run in sync alongside Python, but it is not required.

#### Python backend

`run.sh` starts the Python backend by default (`WACTORZ_BACKEND=python`).

#### Rust backend (optional, in-sync)

The single `wactorz` Rust binary exposes the same REST + WebSocket interface.

| Crate | Role |
|---|---|
| `wactorz-core` | `Actor` trait, `ActorRegistry`, message types, `EventPublisher` |
| `wactorz-agents` | All concrete agent implementations |
| `wactorz-mqtt` | MQTT client wrapper + topic constants |
| `wactorz-interfaces` | REST API (axum), WebSocket bridge, interactive CLI |
| `wactorz-server` | Binary entry point — wires everything together |

### Mosquitto

The MQTT broker.  All inter-actor and actor-to-frontend communication passes through it.  In the full Docker stack, Mosquitto is not exposed directly — all traffic is proxied through nginx.

### nginx

The single public HTTP entry point.

| Path | Proxied to |
|---|---|
| `/` | `static/app/` (static SPA) |
| `/api/` | `wactorz:8080` (REST) |
| `/ws` | `wactorz:8081` (WebSocket bridge) |
| `/mqtt` | `mosquitto:9001` (MQTT over WebSocket) |
| `/fuseki/` | `fuseki:3030` (SPARQL, path-stripped) |

### Fuseki (optional)

Apache Jena Fuseki for RDF/SPARQL storage.  Not required for basic operation.

### Babylon.js SPA

A Vite + TypeScript single-page application.  Connects to the backend via:

- **MQTT over WebSocket** (`/mqtt`) — receives every MQTT message in real time
- **REST** (`/api/`) — agent lifecycle control (pause, resume, stop, delete)
- **WebSocket** (`/ws`) — an alternative MQTT re-broadcast endpoint

---

## Actor Model

Each agent implements the `Actor` protocol.  The Python implementation is the primary definition; the Rust trait mirrors it.

### Rust mirror (in-sync)

```rust
#[async_trait]
pub trait Actor: Send {
    fn id(&self)       -> String;
    fn name(&self)     -> &str;
    fn state(&self)    -> ActorState;
    fn mailbox(&self)  -> mpsc::Sender<Message>;

    async fn on_start(&mut self)                   -> Result<()>;
    async fn handle_message(&mut self, msg: Message) -> Result<()>;
    async fn on_heartbeat(&mut self)               -> Result<()>;
    async fn on_stop(&mut self)                    -> Result<()> { Ok(()) }
    async fn run(&mut self)                        -> Result<()>;
}
```

`ActorSystem::spawn_actor(box)` registers the actor in the `ActorRegistry` and calls `tokio::spawn(actor.run())`.  The `run()` loop is a `tokio::select!` over the mailbox channel and a heartbeat interval.

---

## Message Flow

### User → Agent

```
Browser IO bar
  │  publishes  io/chat  { from: "user", content: "@agent-name text" }
  ▼
Mosquitto
  │  subscribed by wactorz-server
  ▼
MQTT event loop  →  IOAgent mailbox
  │
  ▼
IOAgent.handle_message()
  │  parses @mention, looks up registry
  ▼
target actor mailbox  →  handle_message()  →  LLM call
  │
  ▼
actor publishes  agents/{id}/chat  { from: actor, to: "user", content: … }
  │
  ▼
Mosquitto  →  WebSocket bridge  →  Browser
```

### Agent Lifecycle Events

Every agent publishes to its own MQTT topics on lifecycle events:

| Event | Topic | Triggered by |
|---|---|---|
| Spawn | `agents/{id}/spawn` | `on_start()` |
| Heartbeat | `agents/{id}/heartbeat` | periodic tick |
| State change | `agents/{id}/status` | state mutation |
| Alert | `agents/{id}/alert` | error condition |
| Chat reply | `agents/{id}/chat` | `handle_message()` |

---

## IDs

All actor IDs use **HLC-WIDs** (Hybrid Logical Clock Wide IDs) from the [`waldiez-wid`](https://github.com/waldiez/wid) crate.  They are time-ordered, globally unique, and human-readable.

Message IDs use simpler **WIDs**.

---

## Deployment Modes

| Mode | Docker containers | Binary |
|---|---|---|
| **Full Docker** (`compose.yaml`) | wactorz + nginx + mosquitto + fuseki + HA | inside container |
| **Native binary** (`compose.native.yaml`) | nginx + mosquitto only | runs on host OS |

See [deployment.md](deployment.md) for full instructions.
