.PHONY: install redis image api worker start up stop test test-unit test-blackbox smoke

# Use the project venv when it exists, so targets work without activating it.
PY := $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)

HOST := 127.0.0.1
PORT := 8000
API_URL := http://$(HOST):$(PORT)

# Pid files and logs written by `make up`, removed by `make stop`.
RUN_DIR := .run
WORKERS ?= 2

# --- setup -------------------------------------------------------------------

# Plain python3: the venv does not exist yet when this runs.
install:
	python3 -m pip install -e '.[test]'

redis:
	docker compose up -d redis

image:
	docker build -f Dockerfile.executor -t sandbox-executor:py311 .

# --- run ---------------------------------------------------------------------

api:
	$(PY) -m uvicorn app.main:app --reload --host $(HOST) --port $(PORT)

worker:
	$(PY) -m app.worker

# Foreground development: API with --reload and one worker. Ctrl+C stops both.
start: redis image
	@set -eu; \
	$(PY) -m uvicorn app.main:app --reload --host $(HOST) --port $(PORT) & \
	api_pid=$$!; \
	$(PY) -m app.worker & \
	worker_pid=$$!; \
	trap 'kill "$$api_pid" "$$worker_pid" 2>/dev/null || true' EXIT; \
	trap 'exit 130' INT; \
	trap 'exit 143' TERM; \
	wait

# Background stack plus smoke test. Idempotent: stops any previous run first.
# If the smoke test fails, the stack keeps running so logs in $(RUN_DIR)/ can be read.
up: stop redis image
	@set -eu; \
	mkdir -p $(RUN_DIR); \
	$(PY) -m uvicorn app.main:app --host $(HOST) --port $(PORT) > $(RUN_DIR)/api.log 2>&1 & echo $$! > $(RUN_DIR)/api.pid; \
	for n in $$(seq 1 $(WORKERS)); do \
		$(PY) -m app.worker > $(RUN_DIR)/worker-$$n.log 2>&1 & echo $$! > $(RUN_DIR)/worker-$$n.pid; \
	done; \
	healthy=0; \
	for _ in $$(seq 1 100); do \
		if curl -fsS $(API_URL)/healthz >/dev/null 2>&1; then healthy=1; break; fi; \
		sleep 0.2; \
	done; \
	if [ "$$healthy" -ne 1 ]; then \
		echo "API did not become healthy, see $(RUN_DIR)/api.log" >&2; \
		$(MAKE) stop; \
		exit 1; \
	fi; \
	echo "stack is up: API $(API_URL), $(WORKERS) worker(s), logs in $(RUN_DIR)/"; \
	./scripts/smoke.sh

# Stops processes started by `make up` and removes leftover executor containers.
stop:
	@for pid in $(RUN_DIR)/*.pid; do \
		[ -f "$$pid" ] && kill "$$(cat "$$pid")" 2>/dev/null || true; \
	done; \
	rm -f $(RUN_DIR)/*.pid; \
	ids=$$(docker ps -aq --filter label=sandbox.executor=true); \
	[ -z "$$ids" ] || docker rm -f $$ids >/dev/null; \
	echo "stack stopped"

# --- checks ------------------------------------------------------------------

smoke:
	./scripts/smoke.sh

test:
	$(PY) -m pytest

test-unit:
	$(PY) -m pytest tests --ignore=tests/blackbox

test-blackbox:
	RUN_PHASE1_BLACKBOX=1 $(PY) -m pytest -v tests/blackbox
