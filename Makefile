.PHONY: help dev dev-full dev-ui dev-down dev-backend precommit-install precommit-run build build-frontend build-py check fmt lint format clean \
        up down logs shell \
        run run-py test test-py coverage coverage-py ci \
        install install-py install-docs install-dev install-frontend docs-serve docs-build publish

COMPOSE      := docker compose
COMPOSE_DEV  := $(COMPOSE) -f compose.dev.yaml
FRONTEND_DIR := frontend
PKG_MGR      := $(shell command -v bun >/dev/null 2>&1 && echo bun || (command -v pnpm >/dev/null 2>&1 && echo pnpm || echo npm))
PYTHON       := python3

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*## "}{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}' | sort
	@echo $(PKG_MGR)

# ── Runtime ─────────────────────────────────────────────────────────────────

run: ## Start the Python backend via run.sh
	./run.sh

run-py: ## Explicitly start the Python backend
	./run.sh

dev-backend: ## Start the backend in dev mode (Python REST on :8080)
	WACTORZ_DEV_MODE=1 ./run.sh

# ── Development ─────────────────────────────────────────────────────────────

dev: ## Start mock stack (mosquitto + mock-agents only)
	$(COMPOSE_DEV) up

dev-down: ## Stop mock stack
	$(COMPOSE_DEV) down

dev-full: ## Start full stack in dev mode (Python + mock agents + Vite)
	$(COMPOSE_DEV) up -d && WACTORZ_DEV_MODE=1 ./run.sh &
	cd $(FRONTEND_DIR) && $(PKG_MGR) run dev

dev-ui: ## Start Vite dev server only (needs mosquitto running)
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
	$(PYTHON) -m ruff format wactorz tests
	$(PYTHON) -m ruff check wactorz tests --fix

lint: ## Full frontend lint (typecheck + prettier + eslint)
	cd $(FRONTEND_DIR) && $(PKG_MGR) run lint

lint-py: ## Lint Python — gated ruff (fails) + advisory docstrings/typing (reports only)
	$(PYTHON) -m ruff check wactorz tests
	$(PYTHON) -m ruff format --check wactorz tests
	@echo "── advisory (non-blocking): docstrings + good-practice rules ──"
	-$(PYTHON) -m ruff check wactorz --extend-select D,SIM,RET,PIE --statistics
	@echo "── advisory (non-blocking): basedpyright (basic) ──"
	@command -v basedpyright >/dev/null 2>&1 && basedpyright wactorz || echo "(basedpyright not installed — run 'make install-dev')"

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

test: test-py ## Run Python tests

test-py: ## Run Python tests
	$(PYTHON) -m pytest tests

coverage: coverage-py ## Generate Python coverage report

coverage-py: ## Generate Python coverage XML + terminal report
	mkdir -p coverage
	$(PYTHON) -m coverage run -m pytest tests
	$(PYTHON) -m coverage xml -o coverage/python-coverage.xml
	$(PYTHON) -m coverage report

docs-serve: ## Build docs + serve locally on :8001
	$(PYTHON) -W ignore::UserWarning:pdoc scripts/build_docs.py --serve

docs-build: ## Build full docs site (markdown→HTML + typedoc) into static/docs/
	$(PYTHON) -W ignore::UserWarning:pdoc scripts/build_docs.py --full

publish: ## Build wheel + sdist and upload to PyPI (requires twine + API token)
	$(PYTHON) scripts/build.py --upload

ci: test coverage ## Run the local CI-equivalent checks
