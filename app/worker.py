from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

import redis

from .config import Settings, get_settings
from .models import JobRecord, JobState
from .runner import ExecutionResult, SandboxRunner

logger = logging.getLogger(__name__)

RETRY_BASE_S = 0.5
RETRY_CAP_S = 30.0


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _retry_delay(failures: int) -> float:
    """Exponential backoff for Redis outages: 0.5 s, 1 s, 2 s, ... capped at 30 s."""
    return min(RETRY_BASE_S * 2 ** (failures - 1), RETRY_CAP_S)


def _final_state(result: ExecutionResult) -> JobState:
    if result.timed_out:
        return JobState.TIMED_OUT
    if result.oom_killed:
        return JobState.OOM_KILLED
    if result.exit_code == 0:
        return JobState.SUCCEEDED
    return JobState.FAILED


def _run_job(r: redis.Redis, runner: SandboxRunner, settings: Settings, key: str, record: JobRecord) -> None:
    result = runner.execute(
        source=record.source,
        timeout_ms=record.timeout_ms,
        memory_mb=record.memory_mb,
        cpu_cores=record.cpu_cores,
        pids_limit=record.pids_limit,
        job_id=record.job_id,
        output_callback=lambda stdout, stderr: r.hset(key, mapping={"stdout": stdout, "stderr": stderr}),
    )
    record.status = _final_state(result)
    record.finished_at = _now()
    record.exit_code = result.exit_code
    record.duration_ms = result.duration_ms
    record.peak_memory_bytes = result.peak_memory_bytes
    record.timed_out = result.timed_out
    record.oom_killed = result.oom_killed
    record.stdout = result.stdout
    record.stderr = result.stderr
    r.hset(key, mapping=record.to_redis())
    r.expire(key, settings.job_ttl_seconds)


def _process_next(r: redis.Redis, runner: SandboxRunner, settings: Settings) -> None:
    item = r.blpop(settings.queue_key, timeout=0)
    if not item:
        return

    _, raw_job_id = item
    job_id = raw_job_id.decode() if isinstance(raw_job_id, bytes) else raw_job_id
    key = f"{settings.job_key_prefix}{job_id}"
    data = r.hgetall(key)
    if not data:
        return

    record = JobRecord.from_redis(data)
    record.status = JobState.RUNNING
    record.started_at = _now()
    r.hset(key, mapping=record.to_redis())

    try:
        _run_job(r, runner, settings, key, record)
    except Exception as exc:
        # Partial update: do not overwrite stdout that was already published live.
        r.hset(
            key,
            mapping={
                "status": JobState.FAILED.value,
                "finished_at": _now(),
                "stderr": f"worker error: {type(exc).__name__}: {exc}",
            },
        )
        r.expire(key, settings.job_ttl_seconds)
        print(f"job {job_id} failed in worker: {exc!r}", flush=True)


def main() -> None:
    settings = get_settings()
    r = redis.Redis.from_url(settings.redis_url)
    runner = SandboxRunner(settings.executor_image, settings.max_output_bytes)

    print(f"worker started; queue={settings.queue_key} image={settings.executor_image}", flush=True)

    failures = 0
    while True:
        try:
            _process_next(r, runner, settings)
            failures = 0
        except redis.ConnectionError as exc:
            # Redis is down: wait and retry instead of exiting. Queued jobs stay in Redis.
            failures += 1
            delay = _retry_delay(failures)
            logger.warning("redis unavailable (%s); retrying in %.1fs", exc, delay)
            time.sleep(delay)


if __name__ == "__main__":
    main()
