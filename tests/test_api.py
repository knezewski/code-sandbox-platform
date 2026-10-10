from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from app.api import build_router, register_error_handlers
from app.config import Settings


class DownRedis:
    """Stands in for a Redis client whose server is unreachable."""

    async def ping(self):
        raise RedisConnectionError("redis is down")

    async def hgetall(self, key):
        raise RedisConnectionError("redis is down")

    def pipeline(self, transaction=True):
        raise RedisConnectionError("redis is down")


def make_client() -> TestClient:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(build_router(DownRedis(), Settings()))
    return TestClient(app)


def test_execute_returns_503_with_retry_hint_when_redis_is_down():
    response = make_client().post("/execute", json={"source": "print(1)"})

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"


def test_get_job_returns_503_when_redis_is_down():
    response = make_client().get("/jobs/abc")

    assert response.status_code == 503


def test_healthz_reports_503_when_redis_is_down():
    response = make_client().get("/healthz")

    assert response.status_code == 503
