from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest


TERMINAL_STATES = {"succeeded", "failed", "timed_out", "oom_killed"}


@pytest.fixture(scope="session")
def platform(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Any]:
    if os.getenv("RUN_PHASE1_BLACKBOX") != "1":
        pytest.skip("set RUN_PHASE1_BLACKBOX=1 to run Docker black-box tests")

    import docker
    import httpx
    import redis

    root = Path(__file__).resolve().parents[2]
    base_url = os.getenv("PHASE1_BASE_URL", "http://127.0.0.1:18080")
    port = base_url.rsplit(":", 1)[-1]
    # Dedicated Redis DB: the suite flushes it, so never point it at real data.
    redis_url = os.getenv("PHASE1_REDIS_URL", "redis://localhost:6379/15")
    if urlparse(redis_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail(f"refusing to flush non-local Redis: {redis_url}")
    env = os.environ | {"PYTHONUNBUFFERED": "1", "REDIS_URL": redis_url}

    subprocess.run(["docker", "compose", "up", "-d", "redis"], cwd=root, check=True)
    subprocess.run(
        ["docker", "build", "-f", "Dockerfile.executor", "-t", "sandbox-executor:py311", "."],
        cwd=root,
        check=True,
    )

    redis_client = redis.Redis.from_url(redis_url)
    deadline = time.monotonic() + 15
    while True:
        try:
            redis_client.ping()
            break
        except redis.ConnectionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)
    redis_client.flushdb()

    log_dir = tmp_path_factory.mktemp("phase1-processes")
    processes: list[subprocess.Popen[bytes]] = []
    log_files = []
    commands = [
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", port],
        [sys.executable, "-m", "app.worker"],
        [sys.executable, "-m", "app.worker"],
    ]
    for index, command in enumerate(commands):
        log_file = (log_dir / f"process-{index}.log").open("wb")
        log_files.append(log_file)
        processes.append(subprocess.Popen(command, cwd=root, env=env, stdout=log_file, stderr=subprocess.STDOUT))

    client = httpx.Client(base_url=base_url, timeout=5)
    deadline = time.monotonic() + 15
    try:
        while True:
            if any(process.poll() is not None for process in processes):
                raise RuntimeError(f"Phase 1 process exited; logs are in {log_dir}")
            try:
                if client.get("/healthz").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError(f"API did not become healthy; logs are in {log_dir}")
            time.sleep(0.1)

        yield client
    finally:
        client.close()
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for log_file in log_files:
            log_file.close()
        docker_client = docker.from_env()
        for container in docker_client.containers.list(all=True, filters={"label": "sandbox.executor=true"}):
            container.remove(force=True)


@pytest.fixture
def submit(platform: Any) -> Callable[..., str]:
    def _submit(source: str, **limits: object) -> str:
        response = platform.post("/execute", json={"language": "python", "source": source, **limits})
        assert response.status_code == 202, response.text
        return response.json()["job_id"]

    return _submit


@pytest.fixture
def wait_job(platform: Any) -> Callable[[str, float], dict]:
    def _wait_job(job_id: str, timeout: float = 15) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = platform.get(f"/jobs/{job_id}")
            assert response.status_code == 200, response.text
            job = response.json()
            if job["status"] in TERMINAL_STATES:
                return job
            time.sleep(0.05)
        raise AssertionError(f"job {job_id} did not finish in {timeout} seconds")

    return _wait_job