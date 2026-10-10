# Sandbox Executor — Phase 1

Minimal execution platform for untrusted Python code.

## Architecture

```text
client
  |
  v
FastAPI  ---> Redis queue ---> Worker ---> Docker Engine ---> one-shot container
  |                              |
  +---- GET /jobs/{id} <---------+---- result/status
```

Only the worker talks to Docker. FastAPI only validates requests and manages the job queue/state.

## Security controls in Phase 1

- `network_mode=none`
- read-only container root filesystem
- `/workspace` and `/tmp` are separate ephemeral tmpfs mounts
- memory + swap limit
- CPU quota via `nano_cpus`
- PID cgroup limit
- `SIGKILL` after the execution deadline
- non-root user
- all Linux capabilities dropped
- `no-new-privileges`
- default Docker seccomp/AppArmor policy remains enabled
- one fresh container per job, forced removal in `finally`
- server-side ceilings prevent callers from requesting unlimited resources
- bounded source and output sizes
- live stdout/stderr snapshots while a job is running

## Important Phase 1 limitation

This is a learning/MVP boundary, not a production sandbox boundary. The worker is trusted and controls Docker. A compromise of the worker host or Docker daemon is outside this phase. Phase 2 replaces the container runtime with gVisor; Phase 3 adds Firecracker microVMs.

Also, `/etc/passwd` inside the sandbox is the image's own file. The meaningful host-escape test is that the sandbox cannot access host-only paths or host filesystem content.

## Run locally

### 1. Start Redis

```bash
docker compose up -d redis
```

### 2. Build execution image

```bash
docker build -f Dockerfile.executor -t sandbox-executor:py311 .
```

### 3. Install Python dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 4. Start API

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### 5. Start worker in a second terminal

```bash
source .venv/bin/activate
python3 -m app.worker
```

### 6. Execute code

```bash
curl -s http://127.0.0.1:8000/execute \
  -H 'content-type: application/json' \
  -d '{
    "language": "python",
    "source": "print(2 + 40)",
    "timeout_ms": 2000,
    "memory_mb": 128,
    "cpu_cores": 1,
    "pids_limit": 64
  }'
```

Then poll:

```bash
curl -s http://127.0.0.1:8000/jobs/<JOB_ID>
```

While a job is `running`, polling this endpoint returns the stdout/stderr collected so far. Final output, exit code, timeout/OOM flags, duration, and sampled peak memory are persisted when execution finishes.

## Phase 1 acceptance checks

1. `print(2 + 40)` => `succeeded`, stdout `42`.
2. `while True: pass` => `timed_out`.
3. 100 GB allocation loop => `oom_killed` or non-zero exit under the memory cgroup.
4. Outbound HTTP request => fails because networking is disabled.
5. Fork loop => hits the PID cgroup limit.
6. No host bind mounts are configured; host-only paths are not visible.
7. Root filesystem is read-only while `/workspace` and `/tmp` are writable tmpfs mounts.
8. Combined stdout/stderr is capped at 1 MiB.
9. Every job gets a clean container, and the container is removed afterwards.
10. Multiple workers execute concurrent jobs in distinct containers.

Run the complete Docker-backed acceptance suite with:

```bash
make test-blackbox
```

The suite starts Redis, builds the executor image, launches the API and two workers, executes every check above through HTTP, and tears down its application processes. Docker must be running. Fast unit tests remain available through `make test-unit`; `make smoke` checks the happy path against an already running API and worker.

## Failure modes

What the client sees, what the service does, and what an operator sees. Rows marked *known gap* are not handled in Phase 1.

| Failure | Client sees | Service behavior | Operator sees |
|---|---|---|---|
| Redis down, request path (`POST /execute`, `GET /jobs/{id}`, `/healthz`) | `503`, `Retry-After: 1` | Request fails fast, nothing is queued | HTTP access log with 503 |
| Redis down, worker | Nothing directly; jobs wait | Worker does not exit. Retries with backoff 0.5 s, 1 s, 2 s, ... capped at 30 s | `WARNING redis unavailable; retrying` |
| Redis drops after a job is popped but before it is read | Job stays `queued` forever | *Known gap:* `BLPOP` removes the id with no acknowledgement | Nothing; add reliable queue (`BLMOVE`) later |
| Worker killed mid-job | Job stays `running` forever | *Known gap:* no lease or requeue | Container keeps running until removed manually: `docker rm -f $(docker ps -aq --filter label=sandbox.executor=true)` |
| Docker unavailable while a job runs | `failed`, `stderr` starts with `worker error:` | Worker keeps serving the queue; each job fails until Docker returns | Worker log line `failed in worker` |
| Docker unavailable at worker start | Nothing | Worker process exits (`docker.from_env()` fails) | Process exit; restart when Docker is back |
| Container removal fails | Job result is returned normally | Result is kept, container is left behind | `ERROR failed to remove container <id>` |
| Log stream breaks mid-job | Final output is complete | Output is re-read from the container after exit | None |
| Stats sampling fails | Job runs; `peak_memory_bytes` may be low | Sampling errors are skipped | None (metric accuracy is a known limitation) |
| `kill` fails with an error other than "already exited" | `failed`, `worker error:`, not `timed_out` | Job is marked failed, container is removed | Worker log line `failed in worker` |
| Request body above the limit | `413` | Rejected before JSON parsing | None |

Not implemented in Phase 1: retries for Docker calls, circuit breaking, admission control when dependencies are down, and metrics for these failures (Phase 4).

## Production questions intentionally left for later phases

- reliable queue delivery / retry semantics
- cancellation API
- push-based HTTP streaming (SSE/WebSocket); Phase 1 exposes live snapshots through polling
- image provenance and digest pinning
- multi-language runner images
- per-tenant quotas and admission control
- Prometheus metrics and tracing
- gVisor / Firecracker isolation boundary
- worker pools and warm sandboxes
- cloud IAM / node isolation / autoscaling
