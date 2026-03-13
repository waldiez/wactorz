.PHONY: help dev dev-full dev-backend dev-backend-rust precommit-install precommit-run build build-rust build-frontend check fmt lint clean \
        up down logs shell release release-full release-native release-source \
        run run-py test test-py test-rust parity coverage coverage-py coverage-rust ci \
        install install-py install-docs install-dev install-frontend docs-serve docs-build publish

COMPOSE      := docker compose
COMPOSE_DEV  := $(COMPOSE) -f compose.dev.yaml
FRONTEND_DIR := frontend
RUST_DIR     := rust
PKG_MGR      := $(shell command -v bun 2>/dev/null && echo bun || (command -v pnpm 2>/dev/null && echo pnpm) || echo npm)

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*## "}{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}' | sort

# ── Runtime ─────────────────────────────────────────────────────────────────

run: ## Start the backend via run.sh (respects AGENTFLOW_BACKEND)
	./run.sh

run-py: ## Explicitly start the Python backend
	AGENTFLOW_BACKEND=python ./run.sh

dev-backend: ## Start the backend in dev mode (defaults to Python REST on :8080)
	AGENTFLOW_DEV_MODE=1 ./run.sh

dev-backend-rust: ## Start the Rust backend while keeping dev-mode defaults elsewhere
	AGENTFLOW_DEV_MODE=1 AGENTFLOW_BACKEND=rust ./run.sh

# ── Development ─────────────────────────────────────────────────────────────

dev: ## Start mock stack (mosquitto + mock-agents only)
	$(COMPOSE_DEV) up

dev-down: ## Stop mock stack
	$(COMPOSE_DEV) down

dev-full: ## Start full stack in dev mode (Python + mock agents + Vite)
	$(COMPOSE_DEV) up -d && AGENTFLOW_DEV_MODE=1 ./run.sh &
	cd $(FRONTEND_DIR) && $(PKG_MGR) run dev

dev-ui: ## Start Vite dev server only (needs mosquitto running)
	cd $(FRONTEND_DIR) && $(PKG_MGR) run dev

# ── Build ───────────────────────────────────────────────────────────────────

build: build-rust build-frontend ## Build everything

build-rust: ## Build Rust workspace (release)
	cd $(RUST_DIR) && cargo build --release

build-frontend: ## Build Vite frontend
	cd $(FRONTEND_DIR) && $(PKG_MGR) run build

check: ## Cargo check (fast, no codegen)
	cd $(RUST_DIR) && cargo check

fmt: ## Format Rust + TypeScript
	cd $(RUST_DIR) && cargo fmt
	cd $(FRONTEND_DIR) && $(PKG_MGR) run fmt 2>/dev/null || $(PKG_MGR) x prettier --write "src/**/*.ts"

lint: ## Clippy + TS typecheck
	cd $(RUST_DIR) && cargo clippy -- -D warnings
	cd $(FRONTEND_DIR) && $(PKG_MGR) run typecheck

# ── Docker stack ────────────────────────────────────────────────────────────

up: ## Start full stack (build if needed)
	$(COMPOSE) up --build -d

down: ## Stop full stack
	$(COMPOSE) down

logs: ## Follow full stack logs
	$(COMPOSE) logs -f

logs-%: ## Follow logs for a specific service, e.g. make logs-agentflow
	$(COMPOSE) logs -f $*

shell: ## Open a shell in the agentflow container
	$(COMPOSE) exec agentflow sh

shell-%: ## Open a shell in a running container, e.g. make shell-agentflow
	$(COMPOSE) exec $* sh

# ── Release packaging ───────────────────────────────────────────────────────

release: ## Package pre-built Docker image release (.tar.gz)
	bash scripts/package-release.sh

release-full: ## Package full release zip (source + image)
	bash scripts/package-full-release.sh

release-native: ## Package native binary release (.tar.gz)
	bash scripts/package-native.sh

release-source: ## Package source-only tarball
	bash scripts/package-source.sh

# ── Misc ────────────────────────────────────────────────────────────────────

clean: ## Remove Rust build artifacts and frontend dist
	cd $(RUST_DIR) && cargo clean
	rm -rf $(FRONTEND_DIR)/dist

install: install-py install-frontend ## Install everything (Python + frontend)

install-py: ## Install Python package in editable mode with all extras
	pip install -e ".[all]"

install-docs: ## Install docs dependencies (MkDocs Material + mkdocstrings + mike)
	pip install -e ".[docs]"

install-dev: ## Install everything including dev/docs deps
	pip install -e ".[all,docs,dev]"

install-frontend: ## Install frontend dependencies
	cd $(FRONTEND_DIR) && $(PKG_MGR) install

precommit-install: ## Install the git pre-commit hook
	pre-commit install

precommit-run: ## Run all configured pre-commit hooks across the repo
	pre-commit run --all-files

test: test-py test-rust parity ## Run Python, Rust, and cross-backend parity tests

test-py: ## Run Python tests
	python3 -m unittest discover -s tests -p 'test_*.py'

test-rust: ## Run Rust tests
	cd $(RUST_DIR) && cargo test

parity: ## Prove Python and Rust core supervisor semantics match
	python3 scripts/check_backend_parity.py

coverage: coverage-py coverage-rust ## Generate Python and Rust coverage reports

coverage-py: ## Generate Python coverage XML + terminal report
	mkdir -p coverage
	python3 -m coverage run -m unittest discover -s tests -p 'test_*.py'
	python3 -m coverage xml -o coverage/python-coverage.xml
	python3 -m coverage report

coverage-rust: ## Generate Rust coverage with cargo-llvm-cov
	mkdir -p coverage
	cd $(RUST_DIR) && cargo llvm-cov --workspace --lcov --output-path ../coverage/rust.lcov

docs-serve: ## Serve MkDocs guide locally with live reload (http://localhost:8000)
	mkdocs serve

docs-build: ## Build full docs site (MkDocs + rustdoc) into site/
	mkdocs build
	cp docs/index.html site/index.html
	mkdir -p site/api/rust
	cd $(RUST_DIR) && cargo doc --no-deps --workspace && \
	  cp -r target/doc/. ../site/api/rust/ 2>/dev/null || true

publish: ## Build wheel + sdist and upload to PyPI (requires twine + API token)
	python scripts/build.py --upload

ci: test coverage ## Run the local CI-equivalent checks
