# Contributing to the Wactorz Rust workspace

## Before you start

- [ ] `cargo check` passes on `main`
- [ ] `cargo fmt --check` shows no diff
- [ ] `cargo clippy -- -D warnings` is clean
- [ ] You have read the [crate map](README.md#crate-map) and chosen the right crate for your change

## PR checklist

### Every PR

- [ ] `cargo fmt` — no diff after running
- [ ] `cargo clippy -- -D warnings` — zero warnings
- [ ] `cargo test` — all tests pass
- [ ] `cargo build --release` — release build succeeds
- [ ] `make parity` — Python ↔ Rust supervisor semantics still match

### New feature / behaviour change

- [ ] New CLI flag added to `Args` in `wactorz-server/src/main.rs` (if user-configurable)
- [ ] Corresponding field added to `RuntimeConfig` in `wactorz-interfaces/src/rest.rs` (if exposed to frontend)
- [ ] `/api/config` response updated to include the new field
- [ ] Python `monitor_server.py` `config_handler` updated with the same field
- [ ] Behaviour documented in a doc-comment (`///`) on the public item

### Adding a new agent

1. Create `crates/wactorz-agents/src/{name}_agent.rs`

   ```rust
   use crate::prelude::*;

   pub struct MyAgent {
       system: Option<ActorSystemHandle>,
       publisher: Option<EventPublisher>,
       // ...config fields...
   }

   impl MyAgent {
       pub fn new() -> Self { ... }

       // Builder for optional config
       pub fn with_publisher(mut self, p: EventPublisher) -> Self {
           self.publisher = Some(p); self
       }
   }

   #[async_trait]
   impl Actor for MyAgent {
       fn name(&self) -> &str { "my-agent" }
       fn is_protected(&self) -> bool { false }
       async fn run(&mut self) -> Result<()> { loop { tokio::time::sleep(...).await; } }
   }
   ```

2. Export from `crates/wactorz-agents/src/lib.rs`
   ```rust
   pub mod my_agent;
   pub use my_agent::MyAgent;
   ```

3. Add a supervisor block in `crates/wactorz-server/src/main.rs`
   ```rust
   let my = MyAgent::new().with_publisher(publisher.clone());
   system.supervisor.add("my-agent", my, SupervisorStrategy::OneForOne).await;
   ```

4. Add the NATO-alphabet entry to the supervisor table in [README.md](README.md#supervisor-tree-nato-alphabet)

- [ ] `name()` returns the canonical kebab-case agent name
- [ ] `is_protected()` returns `true` only for core infrastructure agents
- [ ] `run()` loop handles cancellation — exits cleanly on `tokio::select!` with a shutdown signal
- [ ] Agent publishes a `heartbeat` event periodically via `EventPublisher`
- [ ] Any new config accepted via a builder method (`with_*`), not a constructor argument
- [ ] Unit test in the same file (`#[cfg(test)] mod tests { ... }`)

### Adding a new REST endpoint

- [ ] Handler lives in `wactorz-interfaces/src/rest.rs`
- [ ] Route registered in `RestServer::router()`
- [ ] axum 0.8 path param syntax: `{id}` not `:id`
- [ ] Returns `impl IntoResponse` (use `Json(...)` or explicit status codes)
- [ ] Equivalent endpoint added to Python `monitor_server.py` (keep parity)
- [ ] Documented in [README.md](README.md#endpoints) endpoint table

### Touching the WebSocket bridge (`ws.rs`)

- [ ] New message type added to the `WsMessage` enum
- [ ] Handled in both the inbound `match` and, if broadcast, in `MonitorState`
- [ ] Slash command (`/help` output) updated if user-visible
- [ ] `Utf8Bytes` type used as `.as_str().into()` when constructing `TMsg::Text`

## Style guide

**Naming**
- Agent structs: `PascalCase` (e.g. `WeatherAgent`)
- Agent `name()` return: `kebab-case` (e.g. `"weather-agent"`)
- MQTT topic constants: `SCREAMING_SNAKE_CASE` in `wactorz-mqtt/src/topics.rs`

**Error handling**
- Use `anyhow::Result` in agent `run()` methods
- Use `thiserror` for library-facing error types in `wactorz-core`
- Log errors with `tracing::warn!` / `tracing::error!` before returning

**Async**
- Prefer `tokio::select!` with a shutdown channel over bare `loop`
- Never block the async runtime — use `tokio::task::spawn_blocking` for CPU work
- Keep `run()` loops cancel-safe

**Clippy**
- `#[allow(...)]` only as a last resort — add a comment explaining why
- `unwrap()` / `expect()` only in tests or truly infallible paths

## Running checks locally

```bash
cargo fmt --check
cargo clippy -- -D warnings
cargo test
make parity
```

All four must be green before pushing.
