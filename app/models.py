from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class Language(StrEnum):
    PYTHON = "python"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    OOM_KILLED = "oom_killed"


class ExecuteRequest(BaseModel):
    language: Language = Language.PYTHON
    source: str = Field(min_length=1)
    # These bounds are the API contract. Deployment ceilings (MAX_* in Settings)
    # are checked in the API layer and can only tighten them.
    timeout_ms: int = Field(default=2_000, ge=100, le=10_000)
    memory_mb: int = Field(default=128, ge=16, le=512)
    cpu_cores: float = Field(default=1.0, gt=0, le=2.0)
    pids_limit: int = Field(default=64, ge=8, le=256)

    @field_validator("source")
    @classmethod
    def source_must_not_contain_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("source contains NUL byte")
        return value


class ExecuteAccepted(BaseModel):
    job_id: str
    status: JobState


class JobResponse(BaseModel):
    job_id: str
    status: JobState
    language: Language
    timeout_ms: int
    memory_mb: int
    cpu_cores: float
    pids_limit: int
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    peak_memory_bytes: int | None = None
    timed_out: bool = False
    oom_killed: bool = False
    stdout: str = ""
    stderr: str = ""


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


def _opt(value: str | int | None) -> str:
    """Optional fields are stored as empty strings in Redis."""
    return "" if value is None else str(value)


def _opt_text(values: Mapping[str, str], name: str) -> str | None:
    value = values.get(name)
    return None if value in (None, "", "null") else value


@dataclass
class JobRecord:
    """Internal job state, as stored in the Redis hash for one job."""

    job_id: str
    status: JobState
    language: Language
    source: str
    timeout_ms: int
    memory_mb: int
    cpu_cores: float
    pids_limit: int
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    peak_memory_bytes: int | None = None
    timed_out: bool = False
    oom_killed: bool = False
    stdout: str = ""
    stderr: str = ""

    @classmethod
    def from_redis(cls, data: Mapping[bytes | str, bytes | str]) -> JobRecord:
        values = {_text(key): _text(value) for key, value in data.items()}

        def number(name: str) -> int | None:
            value = _opt_text(values, name)
            return None if value is None else int(value)

        return cls(
            job_id=values["job_id"],
            status=JobState(values["status"]),
            language=Language(values["language"]),
            source=values["source"],
            timeout_ms=int(values["timeout_ms"]),
            memory_mb=int(values["memory_mb"]),
            cpu_cores=float(values["cpu_cores"]),
            pids_limit=int(values["pids_limit"]),
            created_at=values["created_at"],
            started_at=_opt_text(values, "started_at"),
            finished_at=_opt_text(values, "finished_at"),
            exit_code=number("exit_code"),
            duration_ms=number("duration_ms"),
            peak_memory_bytes=number("peak_memory_bytes"),
            timed_out=values.get("timed_out") == "true",
            oom_killed=values.get("oom_killed") == "true",
            stdout=values.get("stdout", ""),
            stderr=values.get("stderr", ""),
        )

    def to_redis(self) -> dict[str, str]:
        return {
            "job_id": self.job_id,
            "status": self.status.value,
            "language": self.language.value,
            "source": self.source,
            "timeout_ms": str(self.timeout_ms),
            "memory_mb": str(self.memory_mb),
            "cpu_cores": str(self.cpu_cores),
            "pids_limit": str(self.pids_limit),
            "created_at": self.created_at,
            "started_at": _opt(self.started_at),
            "finished_at": _opt(self.finished_at),
            "exit_code": _opt(self.exit_code),
            "duration_ms": _opt(self.duration_ms),
            "peak_memory_bytes": _opt(self.peak_memory_bytes),
            "timed_out": "true" if self.timed_out else "false",
            "oom_killed": "true" if self.oom_killed else "false",
            "stdout": self.stdout,
            "stderr": self.stderr,
        }

    def to_response(self) -> JobResponse:
        return JobResponse(**{f.name: getattr(self, f.name) for f in fields(self) if f.name != "source"})
