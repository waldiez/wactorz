.PHONY: help dev dev-full dev-backend dev-backend-rust precommit-install precommit-run build build-rust build-frontend check fmt lint clean \
        up down logs shell release release-full release-native release-source \
        run run-py test test-py test-rust parity coverage coverage-py coverage-rust ci

COMPOSE      := docker compose
COMPOSE_DEV  := $(COMPOSE) -f compose.dev.yaml
FRONTEND_DIR := frontend
RUST_DIR     := rust

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

dev-ui: ## Start Vite dev server only (needs mosquitto running)
	cd $(FRONTEND_DIR) && npm run dev

# ── Build ───────────────────────────────────────────────────────────────────

build: build-rust build-frontend ## Build everything

build-rust: ## Build Rust workspace (release)
	cd $(RUST_DIR) && cargo build --release

build-frontend: ## Build Vite frontend
	cd $(FRONTEND_DIR) && npm run build

check: ## Cargo check (fast, no codegen)
	cd $(RUST_DIR) && cargo check

fmt: ## Format Rust + TypeScript
	cd $(RUST_DIR) && cargo fmt
	cd $(FRONTEND_DIR) && npx prettier --write "src/**/*.ts" index.html

lint: ## Clippy + TS typecheck
	cd $(RUST_DIR) && cargo clippy -- -D warnings
	cd $(FRONTEND_DIR) && npm run typecheck

# ── Docker stack ────────────────────────────────────────────────────────────

up: ## Start full stack (build if needed)
	$(COMPOSE) up --build -d

down: ## Stop full stack
	$(COMPOSE) down

logs: ## Follow full stack logs
	$(COMPOSE) logs -f

logs-%: ## Follow logs for a specific service, e.g. make logs-agentflow
	$(COMPOSE) logs -f $*

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

install-frontend: ## Install frontend npm dependencies
	cd $(FRONTEND_DIR) && npm install

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

ci: test coverage ## Run the local CI-equivalent checks
