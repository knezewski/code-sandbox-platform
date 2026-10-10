from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from .api import build_router, register_error_handlers
from .config import get_settings

settings = get_settings()
redis = Redis.from_url(settings.redis_url, decode_responses=False)

# A JSON-escaped source can be up to 6x larger than the raw text (\u0001 per
# control char), so the cheap pre-parse cap is 6x the source limit plus slack for
# the other fields. The exact byte limit is still enforced on the decoded source.
MAX_BODY_BYTES = settings.max_source_bytes * 6 + 4096

app = FastAPI(title="Sandbox Executor", version="0.1.0")
register_error_handlers(app)


@app.middleware("http")
async def reject_oversized_body(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_BODY_BYTES:
        return JSONResponse({"detail": "request body is too large"}, status_code=413)
    return await call_next(request)


app.include_router(build_router(redis, settings))
