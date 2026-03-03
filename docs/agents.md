# Agents

All agents are Rust structs implementing the `Actor` trait.  They communicate exclusively via MQTT — no direct function calls between agents.

---

## Core (protected) agents

These agents are marked `protected: true` and cannot be stopped or deleted from the dashboard.

### MainActor

| | |
|---|---|
| **Name** | `main-actor` |
| **Type** | `orchestrator` |
| **Topic** | `agents/{id}/chat` |

The LLM brain of the system.  Receives user messages, calls the configured LLM (Anthropic / OpenAI / Ollama), and parses `<spawn>` directives in the response to dynamically create new agents.

**Spawn syntax** (in LLM response):
```xml
<spawn agent-type="dynamic" name="my-agent">
  // Rhai script body
  fn handle(msg) { ... }
</spawn>
```

**Configuration** (`.env`):
```
LLM_PROVIDER=anthropic        # anthropic | openai | ollama
LLM_MODEL=claude-sonnet-4-6   # any model ID
LLM_API_KEY=sk-ant-...
```

---

### MonitorAgent

| | |
|---|---|
| **Name** | `monitor-agent` |
| **Type** | `monitor` |
| **Publishes** | `system/health`, `agents/{id}/alert` |

Polls all registered actors every heartbeat cycle.  Raises a `severity: error` alert if any actor's last heartbeat is older than 60 seconds.  Publishes a `system/health` digest on every tick.

---

### QAAgent

| | |
|---|---|
| **Name** | `qa-agent` |
| **Type** | `qa` |
| **Listens** | all `*/chat` messages (via MQTT router) |

Passively inspects every chat message flowing through the broker.  Flags content that matches harmful patterns (prompt injection, PII, profanity).  Publishes a `system/alert` if a policy is violated.

---

## Standard agents

### IOAgent

| | |
|---|---|
| **Name** | `io-agent` |
| **Type** | `gateway` |
| **Listens** | `io/chat` (fixed topic — no ID discovery needed) |

Bridges the frontend IO bar to the actor system.

- `@agent-name text` → routes `text` to the named agent's mailbox
- No `@` prefix → routes to `main-actor`

---

### NautilusAgent

| | |
|---|---|
| **Name** | `nautilus-agent` |
| **Type** | `transfer` |

Named after the *nautilus* shell (SSH = **Secure Shell**) and Jules Verne's submarine.  Bridges remote filesystem operations into the chat interface.

**Commands:**

| Command | Description |
|---|---|
| `ping <user@host>` | Test SSH connectivity |
| `exec <user@host> <cmd [args…]>` | Run a command over SSH |
| `sync <[user@]host:src> <dst>` | rsync pull from remote |
| `push <src> <[user@]host:dst>` | rsync push to remote |
| `help` | List available commands |

**Examples** (from the IO bar):
```
@nautilus-agent ping deploy@myserver.com
@nautilus-agent exec deploy@myserver.com df -h
@nautilus-agent push ./frontend/dist/ deploy@myserver.com:/opt/agentflow/frontend/dist/
@nautilus-agent exec deploy@myserver.com sudo systemctl restart agentflow
```

**Security**: arguments are never passed through a shell — each token is a discrete `Command::arg()`, preventing injection attacks.

**Configuration** (`.env`):
```
NAUTILUS_SSH_KEY=~/.ssh/agentflow_deploy
NAUTILUS_STRICT_HOST_KEYS=0
NAUTILUS_CONNECT_TIMEOUT=10
NAUTILUS_EXEC_TIMEOUT=120
NAUTILUS_RSYNC_FLAGS=
```

---

### UDXAgent

| | |
|---|---|
| **Name** | `udx-agent` |
| **Type** | `expert` |

User and Developer Xpert.  Zero-LLM, always-available knowledge agent.  Answers questions about AgentFlow instantly from a built-in knowledge base — no API key needed.

**Commands:**

| Command | Description |
|---|---|
| `help [topic]` | Overview or topic-specific help |
| `docs <topic>` | In-depth documentation |
| `explain <concept>` | Explain a concept |
| `agents` | List all live agents (queries registry) |
| `status` | System health snapshot |
| `version` | Build info |

**Topics**: `architecture`, `agents`, `chat`, `dashboard`, `api`, `mqtt`, `deploy`

**Concepts**: `actor-model`, `mqtt`, `hlc-wid`, `rust`, `babylon`, `nautilus`, `io`, `qa`, `monitor`, `dynamic`, `main`, `udx`

**Example** (from the IO bar):
```
@udx-agent help
@udx-agent explain mqtt
@udx-agent docs deployment
@udx-agent status
```

---

### DynamicAgent

| | |
|---|---|
| **Name** | `dynamic-{uuid}` (generated) |
| **Type** | `dynamic` |

Spawned on-demand by `MainActor` when the LLM response contains a `<spawn>` directive.  Executes Rhai scripts generated at runtime.  Enables the LLM to extend the system with new capabilities without a server restart.

---

### MlAgent

| | |
|---|---|
| **Type** | `ml` |

Base struct for ML-inference agents.  ONNX and Candle backends are currently stubbed (`anyhow::bail!` placeholders) pending full implementation.

---

## Adding a new agent

1. Create `rust/crates/agentflow-agents/src/my_agent.rs` — implement `Actor` trait (copy `io_agent.rs` as a template)
2. Export in `lib.rs`:
   ```rust
   pub mod my_agent;
   pub use my_agent::MyAgent;
   ```
3. Spawn in `main.rs`:
   ```rust
   let cfg = ActorConfig::new("my-agent");
   let agent = Box::new(MyAgent::new(cfg).with_publisher(publisher.clone()));
   system.spawn_actor(agent).await?;
   ```
4. Add mock responses in `scripts/mock-agents.mjs` for dev-mode testing
5. Add cover gradient + bioline in `frontend/src/ui/SocialDashboard.ts` if desired
