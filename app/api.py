from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from .config import Settings
from .models import ExecuteAccepted, ExecuteRequest, JobRecord, JobResponse, JobState


def _job_key(settings: Settings, job_id: str) -> str:
    return f"{settings.job_key_prefix}{job_id}"


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RedisConnectionError)
    @app.exception_handler(RedisTimeoutError)
    async def redis_unavailable(_request: Request, _exc: Exception) -> JSONResponse:
        # The job store is down, not the request. Say so, and say it is safe to retry.
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "job store is unavailable, retry later"},
            headers={"Retry-After": "1"},
        )


def _check_service_limits(request: ExecuteRequest, settings: Settings) -> None:
    exceeded = [
        (request.timeout_ms > settings.max_timeout_ms, "timeout exceeds service limit"),
        (request.memory_mb > settings.max_memory_mb, "memory exceeds service limit"),
        (request.cpu_cores > settings.max_cpu_cores, "CPU exceeds service limit"),
        (request.pids_limit > settings.max_pids_limit, "PID limit exceeds service limit"),
    ]
    for is_exceeded, detail in exceeded:
        if is_exceeded:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)


def build_router(redis: Redis, settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        await redis.ping()
        return {"status": "ok"}

    @router.post("/execute", response_model=ExecuteAccepted, status_code=status.HTTP_202_ACCEPTED)
    async def execute(request: ExecuteRequest) -> ExecuteAccepted:
        if len(request.source.encode("utf-8")) > settings.max_source_bytes:
            raise HTTPException(status_code=413, detail="source code is too large")
        _check_service_limits(request, settings)

        job_id = uuid4().hex
        record = JobRecord(
            job_id=job_id,
            status=JobState.QUEUED,
            language=request.language,
            source=request.source,
            timeout_ms=request.timeout_ms,
            memory_mb=request.memory_mb,
            cpu_cores=request.cpu_cores,
            pids_limit=request.pids_limit,
            created_at=datetime.now(UTC).isoformat(),
        )
        key = _job_key(settings, job_id)
        pipe = redis.pipeline(transaction=True)
        pipe.hset(key, mapping=record.to_redis())
        pipe.expire(key, settings.job_ttl_seconds)
        pipe.rpush(settings.queue_key, job_id)
        await pipe.execute()
        return ExecuteAccepted(job_id=job_id, status=JobState.QUEUED)

    @router.get("/jobs/{job_id}", response_model=JobResponse)
    async def get_job(job_id: str) -> JobResponse:
        data = await redis.hgetall(_job_key(settings, job_id))
        if not data:
            raise HTTPException(status_code=404, detail="job not found")
        return JobRecord.from_redis(data).to_response()

    return router
