# Contributing to Wactorz

First off — thank you. Wactorz is built in the open and every contribution matters.

## Ways to Contribute

- **Bug reports** — open a [GitHub issue](https://github.com/waldiez/wactorz/issues/new?template=bug_report.yml)
- **Feature requests** — open a [feature issue](https://github.com/waldiez/wactorz/issues/new?template=feature_request.yml)
- **Code** — fork → branch → PR
- **Docs** — the `docs/` directory is Markdown built by `scripts/build_docs.py`; PRs welcome
- **Testing** — add test cases in `tests/`

## Development Setup

```bash
git clone https://github.com/waldiez/wactorz
cd wactorz

# Python (editable install with all extras and dev tooling)
pip install -e ".[all,docs,dev]"

# Frontend
cd frontend && bun install && bun run build && cd ..

# Docs (optional — only needed if you change docs/ source files)
# make docs-build    → regenerates static/docs/ (guide + reference pages)
# API reference docs (JS/Python) are not committed; they are built
# by CI and published to https://waldiez.github.io/wactorz/api/
```

Run the tests and linters:

```bash
make test           # Python + frontend tests
make lint-py        # Python format + lint gate
make lint           # frontend lint gate
```

## Pull Request Process

1. Fork the repo and create a branch: `git checkout -b feat/my-feature`
2. Make your changes — keep commits focused and atomic
3. Run tests: `make test`
4. Update docs if your change affects public API or behaviour
5. Open a PR against `dev` — fill in the PR template

**PR title format:** `type: short description`
Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`

## Code Style

- **Python**: `make lint-py` — ruff format + lint gate (advisory typing via basedpyright)
- **TypeScript**: `make lint` — Prettier, ESLint and `tsc` typecheck

Install pre-commit hooks to run these on commit: `pre-commit install`

## Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(agents): add WizAgent coin economy tracking
fix(mqtt): handle connack timeout with TCP bridge fallback
docs: update MQTT topics reference for 0.2.0
```

## Project Layout

```
wactorz/          Python package source
├── agents/         Built-in agent implementations
├── core/           Actor base, registry, supervisor
└── interfaces/     CLI, REST, Discord, WhatsApp, Telegram interfaces

frontend/           Web card dashboard (TypeScript + Vite)
docs/               Documentation (MkDocs + custom landing page)
tests/              Python test suite
```

## Adding a New Agent

1. Create `wactorz/agents/my_agent.py` — extend `Actor` or `LLMAgent`
2. Register it in `wactorz/cli.py` via `system.supervisor.supervise(...)`
3. Add docs in `docs/agents.md`
4. Add a test if the agent has non-trivial logic

## Questions?

Open a [Discussion](https://github.com/waldiez/wactorz/discussions) or reach us at development@waldiez.io.
