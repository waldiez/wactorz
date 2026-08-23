.PHONY: help dev dev-full dev-ui dev-down dev-app dev-backend precommit-install precommit-run build build-frontend build-py check fmt lint format clean \
        up down logs shell \
        run run-py test test-py test-frontend coverage coverage-py coverage-frontend ci \
        e2e e2e-setup e2e-release e2e-rehearse e2e-demo e2e-clean \
        install install-py install-docs install-dev install-frontend docs-serve docs-build publish

# ── Windows shell setup ──────────────────────────────────────────────────────
# Recipes below use POSIX shell syntax (grep/awk/mkdir -p/rm -rf/source/trap/
# &&/||/subshells). GNU Make only picks a POSIX shell automatically when
# sh.exe is already on PATH (true inside Git Bash, false from a plain
# PowerShell or cmd prompt) — otherwise it silently falls back to cmd.exe and
# every recipe below breaks. Point SHELL at Git for Windows' bash.exe
# explicitly so `make` behaves the same from any Windows shell. The `*` in
# each pattern below matches the literal space in "Program Files" — it's a
# workaround for $(wildcard) treating spaces as pattern separators, not a
# real glob. Skip the WSL bash.exe shim in System32: it runs inside a WSL
# distro, not against this checkout.
ifeq ($(OS),Windows_NT)
  # $(firstword) splits on whitespace, which mangles a path containing a
  # literal space (e.g. "Program Files") — so candidates are assigned as
  # plain text once $(wildcard) confirms they exist, never extracted from
  # a wildcard/firstword result.
  ifneq ($(wildcard C:/Program*Files/Git/bin/bash.exe),)
    GIT_BASH := C:/Program Files/Git/bin/bash.exe
  else ifneq ($(wildcard C:/Program*Files*(x86)/Git/bin/bash.exe),)
    GIT_BASH := C:/Program Files (x86)/Git/bin/bash.exe
  else
    WHERE_BASH := $(filter-out %/System32/bash.exe,$(subst \,/,$(shell where bash 2>NUL)))
    ifneq ($(WHERE_BASH),)
      GIT_BASH := $(firstword $(WHERE_BASH))
    endif
  endif
  ifneq ($(GIT_BASH),)
    SHELL := $(GIT_BASH)
    .SHELLFLAGS := -c
    # For recipe lines with no shell metacharacters, Make skips SHELL
    # entirely and launches the command directly via CreateProcess against
    # the native Windows PATH — which coreutils like rm/mkdir/grep/awk never
    # sit on. Prepend Git's bin dirs there too so both paths find them.
    GIT_ROOT := $(patsubst %/bin/bash.exe,%,$(GIT_BASH))
    export PATH := $(GIT_ROOT)/usr/bin;$(GIT_ROOT)/bin;$(PATH)
  endif
endif

# ── Python / virtualenv detection ────────────────────────────────────────────
# Prefer a local .venv over the system interpreter. Windows venvs put the
# interpreter under Scripts/, POSIX ones under bin/; Windows also has no
# python3.exe by default, so fall back to plain `python` there.
ifeq ($(OS),Windows_NT)
  VENV_PYTHON   := .venv/Scripts/python.exe
  SYSTEM_PYTHON := python
else
  VENV_PYTHON   := .venv/bin/python
  SYSTEM_PYTHON := python3
endif
PYTHON := $(if $(wildcard $(VENV_PYTHON)),$(VENV_PYTHON),$(SYSTEM_PYTHON))

COMPOSE      := docker compose
COMPOSE_DEV  := $(COMPOSE) -f compose.dev.yaml
FRONTEND_DIR := frontend
PKG_MGR      := $(shell command -v bun >/dev/null 2>&1 && echo bun || (command -v pnpm >/dev/null 2>&1 && echo pnpm || echo npm))

help: ## Show this help
	@# The character class includes digits, or targets like `e2e` are absent from
	@# their own help output.
	@grep -E '^[a-zA-Z0-9_-]+:.*##' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*## "}{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}' | sort

# ── Runtime ─────────────────────────────────────────────────────────────────

run: ## Start the Python backend via run.sh
	./run.sh

run-py: ## Explicitly start the Python backend
	./run.sh

dev-backend: ## Start the backend in dev mode (Python REST on :8080)
	WACTORZ_DEV_MODE=1 ./run.sh

# ── Development ─────────────────────────────────────────────────────────────

dev: ## Start the MQTT broker only (mosquitto on 1883)
	$(COMPOSE_DEV) up

dev-down: ## Stop the dev compose stack (all profiles)
	$(COMPOSE_DEV) --profile app --profile full down

dev-app: ## Run the backend + metrics in containers (compose 'app' profile, UI on :8888)
	$(COMPOSE_DEV) --profile app up

dev-full: ## Dev loop: mosquitto (docker) + backend (host, :8888) + Vite (:3000)
	$(COMPOSE_DEV) up -d
	@WACTORZ_DEV_MODE=1 ./run.sh & \
	backend_pid=$$!; \
	trap 'printf "\n[dev-full] stopping backend %s\n" "$$backend_pid"; kill $$backend_pid 2>/dev/null' EXIT INT TERM; \
	printf '[dev-full] waiting for monitor server on :8888'; \
	for _ in $$(seq 1 60); do \
		curl -sf -o /dev/null http://127.0.0.1:8888/api/config && break; \
		printf '.'; sleep 0.5; \
	done; echo; \
	cd $(FRONTEND_DIR) && $(PKG_MGR) run dev
	@# compose (mosquitto) stays up on exit — stop it with `make dev-down`; only
	@# the host backend started above is cleaned up by the trap.

dev-ui: ## Start Vite only (needs a backend on :8888 — e.g. `make dev-full` or `dev-app`)
	cd $(FRONTEND_DIR) && $(PKG_MGR) run dev

# ── Build ───────────────────────────────────────────────────────────────────

build: build-frontend build-py ## Build everything

build-py: ## Build Python wheel and check with twine
	$(PYTHON) -m pip wheel . --no-deps -w dist/ -q
	$(PYTHON) -m pip install --quiet twine
	$(PYTHON) -m twine check dist/*.whl

build-frontend: ## Build Vite frontend and sync to installed package
	cd $(FRONTEND_DIR) && $(PKG_MGR) run build
	@INST=$$($(PYTHON) -m pip show wactorz 2>/dev/null | awk '/^Location:/{print $$2}'); \
	INST="$$INST/wactorz/static/app"; \
	if [ -d "$$INST" ] && [ "$$INST" != "$(CURDIR)/static/app" ]; then \
	  echo "Syncing static/app → $$INST"; \
	  cp -r static/app/ "$$INST/"; \
	fi

check: ## Typecheck the frontend (fast)
	cd $(FRONTEND_DIR) && $(PKG_MGR) run typecheck

fmt: ## Format TypeScript
	cd $(FRONTEND_DIR) && $(PKG_MGR) run fmt 2>/dev/null || $(PKG_MGR) x prettier --write "src/**/*.ts"

format: fmt ## Format TypeScript

fmt-py: ## Format Python (ruff format + safe autofixes) — run this to pass the gate
	$(PYTHON) -m ruff format wactorz tests scripts e2e
	$(PYTHON) -m ruff check wactorz tests scripts e2e --fix

lint: ## Full frontend lint (typecheck + prettier + eslint)
	cd $(FRONTEND_DIR) && $(PKG_MGR) run lint

lint-py: ## Lint Python — gated ruff (fails) + advisory docstrings/typing (reports only)
	$(PYTHON) -m ruff check wactorz tests scripts e2e
	$(PYTHON) -m ruff format --check wactorz tests scripts e2e
	@echo "── advisory (non-blocking): not-yet-gated families ──"
	-$(PYTHON) -m ruff check wactorz --extend-select G,LOG,TRY,C90,PTH,S,T20,DTZ --statistics
	@echo "── advisory (non-blocking): basedpyright (basic) ──"
	@if command -v basedpyright >/dev/null 2>&1; then \
		basedpyright wactorz || true; \
	else \
		echo "(basedpyright not installed — run 'make install-dev')"; \
	fi

# ── Docker stack ────────────────────────────────────────────────────────────

up: ## Start full stack (build if needed)
	$(COMPOSE) up --build -d

down: ## Stop full stack
	$(COMPOSE) down

logs: ## Follow full stack logs
	$(COMPOSE) logs -f

logs-%: ## Follow logs for a specific service, e.g. make logs-wactorz
	$(COMPOSE) logs -f $*

shell: ## Open a shell in the wactorz container
	$(COMPOSE) exec wactorz sh

shell-%: ## Open a shell in a running container, e.g. make shell-wactorz
	$(COMPOSE) exec $* sh

# ── Misc ────────────────────────────────────────────────────────────────────

clean: ## Remove frontend dist
	rm -rf $(FRONTEND_DIR)/dist

install: install-py install-frontend ## Install everything (Python + frontend)

install-py: ## Install Python package in editable mode with all extras
	$(PYTHON) -m pip install -e ".[all]"

install-docs: ## Install docs dependencies (MkDocs Material + mkdocstrings + mike)
	$(PYTHON) -m pip install -e ".[docs]"

install-dev: ## Install everything including dev/docs deps
	$(PYTHON) -m pip install -e ".[all,docs,dev]"

install-frontend: ## Install frontend dependencies
	cd $(FRONTEND_DIR) && $(PKG_MGR) install

precommit-install: ## Install the git pre-commit hook
	pre-commit install

precommit-run: ## Run all configured pre-commit hooks across the repo
	pre-commit run --all-files

test: test-py test-frontend ## Run all tests (Python + frontend)

test-py: ## Run Python tests (pytest) + the remote runner's own self-test
	@# -n auto here and not in pyproject's addopts: parallel wins on the whole
	@# suite and loses on a single file, where worker start-up costs more than
	@# the tests. A focused run should stay serial without having to opt out.
	$(PYTHON) -m pytest tests -n auto
	@# remote_runner.py ships to nodes without pytest or the wactorz package, so
	@# it carries its own tests. Nothing ran them and they had rotted silently.
	$(PYTHON) wactorz/remote_runner.py --test

test-frontend: ## Run frontend tests (vitest)
	cd $(FRONTEND_DIR) && $(PKG_MGR) run test

# ── End-to-end ──────────────────────────────────────────────────────────────
# Real processes, a real broker and a browser. Not part of `test`, and not a
# required check: it has more ways to be non-deterministic than the unit suite,
# so it runs on demand and before a tag. See e2e/README.md.
#
# Every target unsets WACTORZ_STATE_DIR. The suite mints a fresh state directory
# per run, and a value exported for ordinary work would otherwise decide where a
# run wrote — the leak the suite refuses at startup when it arrives by the other
# door (a direct `pytest e2e/`).
E2E := WACTORZ_STATE_DIR= $(PYTHON) -m pytest e2e

e2e-setup: ## One-time: install the Playwright browser the e2e suite drives
	@# The extra rather than a version repeated here — pyproject pins it, and a
	@# second copy of the number is a second thing to forget to bump.
	$(PYTHON) -m pip install -e ".[e2e]"
	$(PYTHON) -m playwright install chromium

e2e: ## Run the e2e regression core + demo scenarios (headless, fake model)
	@# release/ is excluded rather than listed the other way round: the core and
	@# the demos are what must always pass, and release scenarios are a revolving
	@# door that would otherwise make an ordinary run red for a feature in flight.
	$(E2E) --ignore=e2e/scenarios/release

e2e-release: ## Run everything before a tag: core + release/ + demo/
	$(E2E)

e2e-rehearse: ## Headed, paced, fake model — for iterating on demo pacing
	$(E2E) --profile rehearse

e2e-demo: ## Headed, paced, real model — for the take you keep
	$(E2E) --profile demo

e2e-clean: ## Delete every e2e artefact (state, logs, videos, traces)
	@# Everything under out/ is evidence about a run, and a run keeps only what
	@# it needs to explain a failure. A suite trims older runs itself; this is
	@# for reclaiming the lot, including recordings worth keeping — so it says
	@# what it removed rather than doing it silently.
	rm -rf e2e/out
	@echo "removed e2e/out"

coverage: coverage-py coverage-frontend ## Generate coverage (Python + frontend)

coverage-py: ## Generate Python coverage XML + terminal report
	@# pytest-cov rather than `coverage run -m pytest`: the latter measures only
	@# the parent process, so under -n auto it reports a fraction of the truth
	@# with every test still passing. pytest-cov collects from the workers.
	mkdir -p coverage
	$(PYTHON) -m pytest tests -n auto --cov --cov-report=xml:coverage/python-coverage.xml --cov-report=term

coverage-frontend: ## Generate frontend coverage (gated vitest v8 — fails below the floor)
	cd $(FRONTEND_DIR) && $(PKG_MGR) run coverage

docs-serve: ## Build docs + serve locally on :8001
	$(PYTHON) -W ignore::UserWarning:pdoc scripts/build_docs.py --serve

docs-build: ## Build full docs site (markdown→HTML + typedoc) into static/docs/
	$(PYTHON) -W ignore::UserWarning:pdoc scripts/build_docs.py --full

publish: ## Build wheel + sdist and upload to PyPI (requires twine + API token)
	$(PYTHON) scripts/build.py --upload

ci: lint test coverage ## Run the local CI-equivalent checks (lint + tests + coverage)
