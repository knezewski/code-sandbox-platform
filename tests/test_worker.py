import pytest

from app.models import JobState
from app.runner import ExecutionResult
from app.worker import _final_state, _retry_delay


def _result(**overrides) -> ExecutionResult:
    values = dict(
        exit_code=0,
        stdout="",
        stderr="",
        duration_ms=1,
        peak_memory_bytes=0,
        timed_out=False,
        oom_killed=False,
    )
    values.update(overrides)
    return ExecutionResult(**values)


@pytest.mark.parametrize(
    "result, expected",
    [
        (_result(), JobState.SUCCEEDED),
        (_result(exit_code=3), JobState.FAILED),
        (_result(exit_code=137, oom_killed=True), JobState.OOM_KILLED),
        (_result(exit_code=137, timed_out=True), JobState.TIMED_OUT),
        # A timeout wins over an OOM flag: the deadline is what stopped the process.
        (_result(exit_code=137, timed_out=True, oom_killed=True), JobState.TIMED_OUT),
    ],
)
def test_final_state_mapping(result, expected):
    assert _final_state(result) == expected


def test_retry_delay_backs_off_and_caps():
    assert [_retry_delay(n) for n in (1, 2, 3)] == [0.5, 1.0, 2.0]
    assert _retry_delay(100) == 30.0
