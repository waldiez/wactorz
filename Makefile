.PHONY: help dev dev-full dev-ui dev-down dev-app dev-backend precommit-install precommit-run build build-frontend build-py \
		check fmt fmt-py lint lint-py lint-ci tool-image image image-smoke image-scan lock audit test-py-versions test-broker soak format clean \
        up down logs shell mqtt-certs \
        run run-py test test-py test-py-tracked typecheck-tracked python-path test-frontend coverage coverage-py coverage-frontend ci \
        e2e e2e-setup e2e-clean \
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

# uv, when it is on PATH, installs the dev environment from uv.lock into .venv;
# without it the install targets run pip, as they always have. Every other
# target runs $(PYTHON), so nothing else cares which one made the environment.
# USE_UV=0 uses pip even where uv is installed.
UV := $(if $(filter 0,$(USE_UV)),,$(shell command -v uv 2>/dev/null))

# The targets that only make sense with uv say so, instead of failing on a
# command that is not there.
define require-uv
	@command -v uv > /dev/null 2>&1 || { echo "This needs uv: https://docs.astral.sh/uv/getting-started/installation/"; exit 1; }
endef

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
	$(PYTHON) -m ruff format wactorz tests scripts e2e examples
	$(PYTHON) -m ruff check wactorz tests scripts e2e examples --fix

lint: ## Full frontend lint (typecheck + prettier + eslint)
	cd $(FRONTEND_DIR) && $(PKG_MGR) run lint

lint-py: ## Lint Python — gated ruff + basedpyright (fail) + advisory ruff families (report only)
	$(PYTHON) -m ruff check wactorz tests scripts e2e examples
	$(PYTHON) -m ruff format --check wactorz tests scripts e2e examples
	@echo "── advisory (non-blocking): not-yet-gated families ──"
	-$(PYTHON) -m ruff check wactorz --extend-select TRY,C90,PTH,T20 --ignore PTH123 --statistics
	@echo "── gated: basedpyright (basic) ──"
	$(PYTHON) -m basedpyright

# The pinned image of a CI tool, read from its FROM line in .github/tools/Dockerfile.
# A function rather than a nested `$(MAKE) tool-image`: a recipe line naming
# $(MAKE) runs even under `make -n`, so a dry run would start the containers.
tool-image = $(shell sed -n 's/^FROM \(.*\) AS $(1)$$/\1/p' .github/tools/Dockerfile)

# The shell scripts shellcheck reads. The add-ons' run.sh start with bashio's
# shebang, which shellcheck cannot place, so they are named as bash.
SHELL_SCRIPTS := docker-entrypoint.sh run.sh infra/prometheus/render-config.sh infra/alertmanager/render-config.sh scripts/image-smoke.sh scripts/test-broker.sh e2e/stack/node/start.sh
ADDON_SCRIPTS := ha-addon/wactorz/run.sh ha-addon/wactorz-ultra/run.sh

# The docker calls below name paths inside containers (`-w /src`, the docker
# socket). Git Bash on Windows would rewrite them into Windows paths first;
# this keeps them as written, and does nothing anywhere else.
lint-ci image-smoke image-scan: export MSYS_NO_PATHCONV := 1
lint-ci image-smoke image-scan: export MSYS2_ARG_CONV_EXCL := *

lint-ci: ## Lint the GitHub workflows (zizmor), shell scripts (shellcheck) and Dockerfiles (hadolint), and scan for secrets (gitleaks), with the pinned tool images
	@# Online when GH_TOKEN is set, as in CI: the online audits check that a
	@# pinned sha belongs to its action and that no pinned version has an advisory.
	docker run --rm -v "$(CURDIR):/src:ro" -w /src $(if $(GH_TOKEN),-e GH_TOKEN,) \
		$(call tool-image,zizmor) $(if $(GH_TOKEN),,--offline) .
	docker run --rm -v "$(CURDIR):/mnt:ro" -w /mnt $(call tool-image,shellcheck) $(SHELL_SCRIPTS)
	docker run --rm -v "$(CURDIR):/mnt:ro" -w /mnt $(call tool-image,shellcheck) --shell=bash $(ADDON_SCRIPTS)
	@for f in Dockerfile ha-addon/*/Dockerfile e2e/stack/node/Dockerfile; do \
		echo "hadolint $$f"; docker run --rm -i $(call tool-image,hadolint) < "$$f" || exit 1; \
	done
	@# The committed tree, handed over as an archive: what is in the commit and
	@# nothing else. Scanning the folder would read a local .env and the state
	@# directory, and scanning the history takes many minutes. The commit hook
	@# scans each commit as it is made; this is for one made without the hook.
	git archive HEAD | docker run --rm -i --entrypoint sh $(call tool-image,gitleaks) -c \
		'mkdir /tmp/src && tar -x -C /tmp/src && cd /tmp/src && gitleaks dir . --no-banner --redact'

# The app image the checks below look at. `make image` builds it under this name;
# CI and the release workflows pass their own.
IMAGE ?= wactorz:local

# Which of the two app images `make image` builds: `default`, or `ultra` with
# PyTorch, Ultralytics, OpenCV and the system libraries they and the Reachy Mini
# SDK need (see the Dockerfile). `image-smoke` asks the image which it is.
FLAVOUR ?= default

# Extra Trivy arguments for `image-scan`: the release workflows pass `--platform`
# for each architecture, and the add-ons skip a binary of Home Assistant's own.
SCAN_ARGS ?=

# Trivy's vulnerability database lives in this docker volume, so it is fetched
# once and reused rather than downloaded by every scan. It is fetched on its own,
# with retries, because the download is what fails transiently -- a mirror
# answering 404 for a moment -- and a scan should not go red for that.
TRIVY_CACHE := wactorz-trivy-cache
TRIVY_DB_REPOSITORIES := mirror.gcr.io/aquasec/trivy-db:2,ghcr.io/aquasecurity/trivy-db:2

# Refuses early, naming the image and how to get it, instead of letting docker or
# Trivy fail on a reference that is not there. A registry digest (`…@sha256:…`,
# what the release workflows check) is fetched instead, so it is let through.
define require-image
	@case "$(IMAGE)" in *@sha256:*) ;; *) docker image inspect "$(IMAGE)" > /dev/null 2>&1 \
		|| { echo "No image $(IMAGE): build it with 'make image', or pass IMAGE=<an image you have>."; exit 1; } ;; esac
endef

image: ## Build the app image as CI does (the Debian upgrade stage never cached), tagged IMAGE (default wactorz:local); FLAVOUR=ultra builds the larger one
	docker build --build-arg FLAVOUR=$(FLAVOUR) --no-cache-filter runtime -t "$(IMAGE)" .

image-smoke: ## Smoke-test IMAGE beside a broker: /health and /ready on both servers, no root, no set-id; an ultra image also imports what it adds
	$(require-image)
	scripts/image-smoke.sh "$(IMAGE)" "$(call tool-image,mosquitto)"

# Fixable CRITICAL and HIGH findings fail. One that cannot be fixed here yet, such
# as a new Debian fix the pinned base has not picked up, is accepted in
# .trivyignore.yaml with a statement and an expiry date, never left to fail every push.
image-scan: ## Scan IMAGE for fixable CRITICAL/HIGH vulnerabilities (accepted ones: .trivyignore.yaml)
	$(require-image)
	@for attempt in 1 2 3; do \
		docker run --rm -v $(TRIVY_CACHE):/root/.cache $(call tool-image,trivy) image --quiet \
			--download-db-only --db-repository $(TRIVY_DB_REPOSITORIES) && exit 0; \
		echo "Fetching Trivy's database failed ($$attempt of 3)."; sleep 10; \
	done; exit 1
	@# TRIVY_USERNAME/TRIVY_PASSWORD, when set, reach a registry that needs them.
	docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v $(TRIVY_CACHE):/root/.cache \
		$(if $(TRIVY_PASSWORD),-e TRIVY_USERNAME -e TRIVY_PASSWORD,) \
		-v "$(CURDIR)/.trivyignore.yaml:/trivyignore.yaml:ro" $(call tool-image,trivy) \
		image --quiet --scanners vuln --severity CRITICAL,HIGH --ignore-unfixed \
		--skip-db-update --ignorefile /trivyignore.yaml --table-mode detailed --show-suppressed --exit-code 1 \
		$(SCAN_ARGS) "$(IMAGE)"

tool-image: ## Print the pinned image of a CI tool, NAME=zizmor|shellcheck|trivy|hadolint|mosquitto (.github/tools/Dockerfile)
	@echo "$(call tool-image,$(NAME))"

# ── Docker stack ────────────────────────────────────────────────────────────

up: ## Start full stack (build if needed)
	$(COMPOSE) up --build -d

mqtt-certs: ## Issue the compose broker's TLS certificate from this host, then: docker compose restart mosquitto
	$(PYTHON) -m wactorz.broker_certificates --export infra/mosquitto/generated

down: ## Stop full stack
	$(COMPOSE) down

logs: ## Follow full stack logs
	$(COMPOSE) logs -f

logs-%: ## Follow logs for a specific service, e.g. make logs-wactorz-python
	$(COMPOSE) logs -f $*

shell: ## Open a shell in the wactorz container
	$(COMPOSE) exec wactorz-python sh

shell-%: ## Open a shell in a running container, e.g. make shell-wactorz-python
	$(COMPOSE) exec $* sh

# ── Misc ────────────────────────────────────────────────────────────────────

clean: ## Remove frontend dist
	rm -rf $(FRONTEND_DIR)/dist

install: install-py install-frontend ## Install everything (Python + frontend)

# With uv, each install puts what it names at the versions uv.lock pins, and
# removes nothing -- as the pip commands they stand in for do -- so extras or
# tools installed by hand (ml, flic, a notebook kernel) survive it. CI installs
# exactly what the lockfile says instead, with `uv sync --locked`.
# `vision` is in install-dev because the type checker reads the camera shim,
# which imports cv2; CI's lint job installs it for the same reason.
install-py: ## Install Python package in editable mode with all extras (uv from uv.lock if present, else pip)
ifneq ($(UV),)
	uv sync --locked --inexact --extra all
else
	$(PYTHON) -m pip install -e ".[all]"
endif

install-docs: ## Install docs dependencies (markdown + pygments + pdoc) (uv if present, else pip)
ifneq ($(UV),)
	uv sync --locked --inexact --extra docs
else
	$(PYTHON) -m pip install -e ".[docs]"
endif

install-dev: ## Install everything including dev/docs deps (uv from uv.lock if present, else pip)
ifneq ($(UV),)
	uv sync --locked --inexact --extra all --extra docs --extra dev --extra vision
else
	$(PYTHON) -m pip install -e ".[all,docs,dev,vision]"
endif

lock: ## Re-resolve uv.lock after changing dependencies in pyproject.toml (needs uv)
	$(require-uv)
	uv lock

audit: ## Known vulnerabilities in the locked dependencies (needs uv)
	$(require-uv)
	uv audit --locked --preview-features audit-command

install-frontend: ## Install frontend dependencies
	cd $(FRONTEND_DIR) && $(PKG_MGR) install

precommit-install: ## Install the git pre-commit hook (prek)
	$(PYTHON) -m prek install

precommit-run: ## Run all configured hooks across the repo (prek)
	$(PYTHON) -m prek run --all-files

test: test-py test-frontend ## Run all tests (Python + frontend)

python-path: ## Print the interpreter every Python target here runs (.venv's when there is one)
	@echo $(PYTHON)

# What the commit hook runs. The hook sets unstaged changes aside before it
# starts, but not files git has never been told about: a new test file for work
# that is not part of the commit would then run against code that has just been
# set aside, and fail a commit it has nothing to do with. Those files are left
# out; one that is staged is in the commit and runs.
test-py-tracked: ## Run the Python tests git knows about, leaving out untracked files (the commit hook)
	$(PYTHON) -m pytest tests -n auto \
		$$(git ls-files --others --exclude-standard -- 'tests/*.py' | sed 's/^/--ignore=/')

# The same reason, for the type checker: it reads the whole configured tree, so
# an untracked file is checked against code the hook has just set aside. Given
# the files by name it reads those and what they import, and reports on no other.
typecheck-tracked: ## Type-check the Python files git knows about, leaving out untracked files (the commit hook)
	$(PYTHON) -m basedpyright $$(git ls-files -- 'wactorz/*.py' 'tests/*.py' 'scripts/*.py')

test-py: ## Run Python tests (pytest)
	@# -n auto here and not in pyproject's addopts: parallel wins on the whole
	@# suite and loses on a single file, where worker start-up costs more than
	@# the tests. A focused run should stay serial without having to opt out.
	$(PYTHON) -m pytest tests -n auto

# The Python versions `test-py-versions` runs on: those pyproject.toml supports,
# as CI's matrix. Narrow it for a quicker look, e.g. PYTHONS="3.10 3.11", the two
# whose asyncio differs most. Opt-in only: nothing else runs it, since each
# version takes about as long as `make test-py`.
PYTHONS ?= 3.10 3.11 3.12 3.13 3.14

test-py-versions: ## Run the Python tests on each supported version (PYTHONS=...), in throwaway uv environments (needs uv)
	$(require-uv)
	@for v in $(PYTHONS); do \
		echo "── Python $$v"; \
		uv run --isolated --locked --python $$v --extra all --extra dev \
			python -m pytest tests -q -n auto -p no:cacheprovider || { echo "Failed on Python $$v."; exit 1; }; \
	done

# A real main and a real node, in one process, joined only by a real mosquitto
# started for the run: the contract between them, exercised rather than pinned.
# The ordinary suite refuses every broker connection, so these are skipped there.
test-broker: ## Run the main-and-node tests over a real mosquitto, started for the run (needs Docker)
	scripts/test-broker.sh "$(call tool-image,mosquitto)" "$(PYTHON)" -q

# The same main and node, kept busy for DURATION seconds: agents spawned, asked
# and deleted over and over, with what is left behind compared after every
# round. A leak is a number that should come back and does not. SOAK_REPORT
# names a file for the samples.
DURATION ?= 300
soak: ## Keep a main and a node busy over a real mosquitto for DURATION seconds and fail on anything that only grows (needs Docker)
	WACTORZ_SOAK_SECONDS=$(DURATION) WACTORZ_SOAK_REPORT=$(SOAK_REPORT) \
		scripts/test-broker.sh "$(call tool-image,mosquitto)" "$(PYTHON)" -q -s -k soak

test-frontend: ## Run frontend tests (vitest)
	cd $(FRONTEND_DIR) && $(PKG_MGR) run test

# ── End-to-end ──────────────────────────────────────────────────────────────
# A real broker, the application as a process, a node deployed over SSH, and a
# browser: what a person does with the product, done in order and read
# strictly. Not part of `test`: it needs Docker and a browser, and takes
# minutes. See e2e/README.md.
#
# Run with its own pytest.ini, so it shares no setting with the unit suite. It
# starts everything it uses, on ports and in a directory of its own, and reads
# nothing of a developer's `.env` or state.
e2e-setup: ## One-time: install Playwright and the browser the e2e suite drives
	@# The extra, not a version repeated here: pyproject pins it.
	$(PYTHON) -m pip install -e ".[e2e]"
	$(PYTHON) -m playwright install chromium

e2e: ## Run the end-to-end journeys (needs Docker; `make e2e-setup` once)
	$(PYTHON) -m pytest -c e2e/pytest.ini --rootdir e2e e2e/journeys

e2e-clean: ## Delete what failed e2e runs kept (logs, traces, state)
	@# A run that passes removes its own directory; one that fails keeps it.
	rm -rf e2e/out
	@echo "removed e2e/out"

coverage: coverage-py coverage-frontend ## Generate coverage (Python + frontend)

coverage-py: ## Generate Python coverage (XML + lcov) + terminal report
	@# pytest-cov rather than `coverage run -m pytest`: the latter measures only
	@# the parent process, so under -n auto it reports a fraction of the truth
	@# with every test still passing. pytest-cov collects from the workers.
	mkdir -p coverage
	$(PYTHON) -m pytest tests -n auto --cov --cov-report=xml:coverage/python-coverage.xml --cov-report=term
	@# lcov as well, because it is the one format both halves of this repo can
	@# speak: the frontend's vitest writes it too, so one service can add them
	@# up into a single number for the badge.
	$(PYTHON) -m coverage lcov -o coverage/python-coverage.lcov

coverage-frontend: ## Generate frontend coverage (gated vitest v8 — fails below the floor)
	cd $(FRONTEND_DIR) && $(PKG_MGR) run coverage

docs-serve: ## Build docs + serve locally on :8001
	$(PYTHON) -W ignore::UserWarning:pdoc scripts/build_docs.py --serve

docs-build: ## Build full docs site (markdown→HTML + typedoc) into static/docs/
	$(PYTHON) -W ignore::UserWarning:pdoc scripts/build_docs.py --full

publish: ## Build wheel + sdist and upload to PyPI (requires twine + API token)
	$(PYTHON) scripts/build.py --upload

ci: lint test coverage ## Run the local CI-equivalent checks (lint + tests + coverage)
