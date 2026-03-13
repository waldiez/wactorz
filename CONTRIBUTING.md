# Contributing to AgentFlow

First off — thank you. AgentFlow is built in the open and every contribution matters.

## Ways to Contribute

- **Bug reports** — open a [GitHub issue](https://github.com/waldiez/agentflow/issues/new?template=bug_report.yml)
- **Feature requests** — open a [feature issue](https://github.com/waldiez/agentflow/issues/new?template=feature_request.yml)
- **Code** — fork → branch → PR
- **Docs** — the `docs/` directory is MkDocs Markdown; PRs welcome
- **Testing** — add test cases in `tests/`

## Development Setup

```bash
git clone https://github.com/waldiez/agentflow
cd agentflow

# Python (editable install with all extras)
pip install -e ".[all]"
pip install mkdocs-material mkdocstrings[python]

# Frontend
cd frontend && bun install && bun run build && cd ..

# Rust (optional)
cd rust && cargo build && cd ..
```

Run the tests:

```bash
make test-py        # Python unit tests
make test-rust      # Rust tests (requires Rust toolchain)
```

## Pull Request Process

1. Fork the repo and create a branch: `git checkout -b feat/my-feature`
2. Make your changes — keep commits focused and atomic
3. Run tests: `make test-py`
4. Update docs if your change affects public API or behaviour
5. Open a PR against `main` — fill in the PR template

**PR title format:** `type: short description`
Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`

## Code Style

- **Python**: `ruff` for linting, `black` for formatting (via pre-commit hooks)
- **Rust**: `cargo fmt` + `cargo clippy`
- **TypeScript**: `biome` (via `bun run lint`)

Install pre-commit hooks: `pre-commit install`

## Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(agents): add WizAgent coin economy tracking
fix(mqtt): handle connack timeout with TCP bridge fallback
docs: update MQTT topics reference for 0.2.0
```

## Project Layout

```
agentflow/          Python package source
├── agents/         Built-in agent implementations
├── core/           Actor base, registry, supervisor
└── interfaces/     CLI, REST, Discord, WhatsApp interfaces

frontend/           Babylon.js web dashboard (TypeScript + Vite)
rust/               Rust WS bridge and server crates
docs/               Documentation (MkDocs + custom landing page)
tests/              Python test suite
```

## Adding a New Agent

1. Create `agentflow/agents/my_agent.py` — extend `Actor` or `LLMAgent`
2. Register it in `agentflow/cli.py` via `system.supervisor.supervise(...)`
3. Add docs in `docs/agents.md`
4. Add a test if the agent has non-trivial logic

## Questions?

Open a [Discussion](https://github.com/waldiez/agentflow/discussions) or reach us at development@waldiez.io.
