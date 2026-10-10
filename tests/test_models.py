import pytest
from pydantic import ValidationError

from app.models import ExecuteRequest, JobRecord, JobState, Language


def test_defaults_are_bounded():
    request = ExecuteRequest(source="print('hello')")
    assert request.timeout_ms == 2000
    assert request.memory_mb == 128
    assert request.pids_limit == 64


def test_nul_is_rejected():
    with pytest.raises(ValidationError):
        ExecuteRequest(source="print('x')\x00")


def test_job_record_round_trips_through_redis_encoding():
    record = JobRecord(
        job_id="job-1",
        status=JobState.TIMED_OUT,
        language=Language.PYTHON,
        source="print(1)",
        timeout_ms=500,
        memory_mb=64,
        cpu_cores=0.5,
        pids_limit=16,
        created_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:01+00:00",
        exit_code=137,
        duration_ms=500,
        timed_out=True,
        stdout="a\n",
    )

    encoded = record.to_redis()

    assert encoded["timed_out"] == "true"
    assert encoded["oom_killed"] == "false"
    assert encoded["started_at"] == ""
    assert encoded["peak_memory_bytes"] == ""
    assert JobRecord.from_redis({key.encode(): value.encode() for key, value in encoded.items()}) == record


def test_job_record_exposes_response_without_source():
    record = JobRecord(
        job_id="job-2",
        status=JobState.QUEUED,
        language=Language.PYTHON,
        source="secret code",
        timeout_ms=2000,
        memory_mb=128,
        cpu_cores=1.0,
        pids_limit=64,
        created_at="2026-01-01T00:00:00+00:00",
    )

    response = record.to_response()

    assert response.job_id == "job-2"
    assert "source" not in response.model_dump()
