from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    redis_url: str = "redis://localhost:6379/0"
    job_ttl_seconds: int = 3600
    queue_key: str = "execution:queue"
    job_key_prefix: str = "job:"

    executor_image: str = "sandbox-executor:py311"
    max_source_bytes: int = 256 * 1024
    max_output_bytes: int = 1024 * 1024

    max_timeout_ms: int = 10_000
    max_memory_mb: int = 512
    max_cpu_cores: float = 2.0
    max_pids_limit: int = 256


@lru_cache
def get_settings() -> Settings:
    return Settings()
