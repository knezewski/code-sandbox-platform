from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

import docker
import pytest


def wait_for_container(job_id: str, timeout: float = 5):
    client = docker.from_env()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        containers = client.containers.list(all=True, filters={"label": f"sandbox.job_id={job_id}"})
        if containers:
            return containers[0]
        time.sleep(0.02)
    raise AssertionError(f"container for job {job_id} was not observed")


def test_success_and_separate_output(submit: Callable, wait_job: Callable):
    job = wait_job(submit("import sys\nprint(42)\nprint('problem', file=sys.stderr)"))

    assert job["status"] == "succeeded", job["stderr"]
    assert job["exit_code"] == 0
    assert job["stdout"] == "42\n"
    assert job["stderr"] == "problem\n"
    assert job["duration_ms"] >= 0
    assert job["peak_memory_bytes"] >= 0


def test_output_is_visible_while_running(platform: Any, submit: Callable, wait_job: Callable):
    job_id = submit("import time\nprint('first', flush=True)\ntime.sleep(1)\nprint('second')", timeout_ms=3000)
    deadline = time.monotonic() + 2
    observed_live_output = False
    while time.monotonic() < deadline:
        job = platform.get(f"/jobs/{job_id}").json()
        if job["status"] == "running" and job["stdout"] == "first\n":
            observed_live_output = True
            break
        time.sleep(0.02)

    assert observed_live_output
    assert wait_job(job_id)["stdout"] == "first\nsecond\n"


def test_timeout_and_container_cleanup(submit: Callable, wait_job: Callable):
    job_id = submit("while True:\n    pass", timeout_ms=300)
    job = wait_job(job_id)

    assert job["status"] == "timed_out"
    assert job["timed_out"] is True
    assert job["duration_ms"] < 3000
    assert docker.from_env().containers.list(all=True, filters={"label": f"sandbox.job_id={job_id}"}) == []


def test_runtime_security_controls(submit: Callable, wait_job: Callable):
    source = """
from pathlib import Path
import time
try:
    Path('/phase1-root-write').write_text('bad')
    print('ROOT_WRITABLE')
except OSError:
    print('ROOT_READ_ONLY')
Path('/workspace/output').write_text('ok')
Path('/tmp/output').write_text('ok')
time.sleep(1)
"""
    job_id = submit(source, timeout_ms=3000, memory_mb=64, cpu_cores=0.5, pids_limit=16)
    container = wait_for_container(job_id)
    container.reload()
    host_config = container.attrs["HostConfig"]

    assert host_config["NetworkMode"] == "none"
    assert host_config["ReadonlyRootfs"] is True
    assert host_config["Memory"] == 64 * 1024 * 1024
    assert host_config["MemorySwap"] == 64 * 1024 * 1024
    assert host_config["NanoCpus"] == 500_000_000
    assert host_config["PidsLimit"] == 16
    assert host_config["CapDrop"] == ["ALL"]
    assert "no-new-privileges:true" in host_config["SecurityOpt"]
    assert set(host_config["Tmpfs"]) == {"/workspace", "/tmp"}
    assert container.attrs["Config"]["User"] == "65532:65532"
    assert container.attrs["Mounts"] == []
    job = wait_job(job_id)
    assert job["status"] == "succeeded"
    assert job["stdout"] == "ROOT_READ_ONLY\n"


def test_host_file_is_unreachable(submit: Callable, wait_job: Callable, tmp_path):
    # Meaningful on a Linux host. On Docker Desktop the "host" is a VM that never
    # sees this macOS path, so there the test only proves nothing is bind-mounted.
    token = uuid.uuid4().hex
    canary = tmp_path / "canary.txt"
    canary.write_text(token)
    source = f"""
from pathlib import Path
p = {str(canary)!r}
for base in ("", "/host", "/proc/1/root"):
    try:
        print("READ", Path(base + p).read_text())
    except Exception as exc:
        print("ERR", type(exc).__name__)
print("---MOUNTINFO---")
print(Path("/proc/self/mountinfo").read_text())
"""
    job = wait_job(submit(source))
    assert job["status"] == "succeeded", job["stderr"]
    assert token not in job["stdout"]
    assert str(tmp_path) not in job["stdout"].split("---MOUNTINFO---", 1)[1]


def test_large_source_is_accepted(submit: Callable, wait_job: Callable):
    source = "# " + "x" * 250_000 + "\nprint('big-ok')\n"
    job = wait_job(submit(source))
    assert job["status"] == "succeeded", job["stderr"]
    assert job["stdout"] == "big-ok\n"


def test_outbound_network_is_blocked(submit: Callable, wait_job: Callable):
    source = """
import urllib.request
try:
    urllib.request.urlopen('https://example.com', timeout=1)
    print('NETWORK_ALLOWED')
except Exception:
    print('NETWORK_BLOCKED')
"""
    job = wait_job(submit(source))
    assert job["status"] == "succeeded"
    assert job["stdout"] == "NETWORK_BLOCKED\n"


def test_memory_bomb_is_oom_killed(submit: Callable, wait_job: Callable):
    source = "chunks = []\nwhile True:\n    chunks.append(bytearray(10 * 1024 * 1024))"
    job = wait_job(submit(source, timeout_ms=5000, memory_mb=32), timeout=10)
    assert job["status"] == "oom_killed"
    assert job["oom_killed"] is True
    assert job["exit_code"] != 0


def test_fork_bomb_reaches_pid_limit(submit: Callable, wait_job: Callable):
    source = """
import os
import time
for _ in range(256):
    try:
        child = os.fork()
        if child == 0:
            time.sleep(5)
            os._exit(0)
    except OSError:
        print('PID_LIMIT_REACHED', flush=True)
        break
else:
    print('PID_LIMIT_BYPASSED', flush=True)
"""
    job = wait_job(submit(source, timeout_ms=3000, pids_limit=16), timeout=8)
    assert "PID_LIMIT_REACHED" in job["stdout"]
    assert "PID_LIMIT_BYPASSED" not in job["stdout"]


def test_combined_output_is_bounded(submit: Callable, wait_job: Callable):
    source = "import sys\nsys.stdout.write('o' * 700000)\nsys.stderr.write('e' * 700000)"
    job = wait_job(submit(source), timeout=10)
    assert job["status"] == "succeeded"
    assert len(job["stdout"].encode()) + len(job["stderr"].encode()) == 1024 * 1024


def test_jobs_have_clean_filesystems(submit: Callable, wait_job: Callable):
    first = wait_job(submit("from pathlib import Path\nPath('/tmp/sentinel').write_text('secret')"))
    second = wait_job(submit("from pathlib import Path\nprint(Path('/tmp/sentinel').exists())"))
    assert first["status"] == "succeeded"
    assert second["stdout"] == "False\n"


def test_two_workers_use_distinct_containers(submit: Callable, wait_job: Callable):
    source = "import time\nprint('started', flush=True)\ntime.sleep(2)"
    first_id = submit(source, timeout_ms=4000)
    second_id = submit(source, timeout_ms=4000)
    first_container = wait_for_container(first_id)
    second_container = wait_for_container(second_id)
    assert first_container.id != second_container.id
    assert wait_job(first_id)["status"] == "succeeded"
    assert wait_job(second_id)["status"] == "succeeded"


@pytest.mark.parametrize(
    "payload, expected_status",
    [
        ({"language": "javascript", "source": "1"}, 422),
        ({"language": "python", "source": ""}, 422),
        ({"language": "python", "source": "x\x00"}, 422),
        ({"language": "python", "source": "1", "timeout_ms": 10001}, 422),
        ({"language": "python", "source": "1", "memory_mb": 513}, 422),
        ({"language": "python", "source": "1", "cpu_cores": 2.1}, 422),
        ({"language": "python", "source": "1", "pids_limit": 257}, 422),
    ],
)
def test_invalid_requests_are_rejected(platform: Any, payload: dict, expected_status: int):
    response = platform.post("/execute", json=payload)
    assert response.status_code == expected_status


def test_missing_job_returns_404(platform: Any):
    assert platform.get("/jobs/does-not-exist").status_code == 404


def test_oversized_source_returns_413(platform: Any):
    response = platform.post("/execute", json={"language": "python", "source": "x" * (256 * 1024 + 1)})
    assert response.status_code == 413


def test_oversized_body_is_rejected_before_parsing(platform: Any):
    response = platform.post("/execute", json={"language": "python", "source": "x" * (2 * 1024 * 1024)})
    assert response.status_code == 413
    assert response.json()["detail"] == "request body is too large"