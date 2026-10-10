from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable

import docker
from docker.errors import APIError, DockerException

# Properties of the executor image and sandbox, not deployment settings.
POLL_INTERVAL_S = 0.05
STATS_INTERVAL_S = 0.1
SNAPSHOT_INTERVAL_S = 0.1
JOIN_TIMEOUT_S = 2
KILL_WAIT_S = 5
SEND_TIMEOUT_S = 5
SHM_BYTES = 16 * 1024 * 1024
TMPFS_SIZE = "64m"
EXECUTOR_UID = "65532:65532"

OutputCallback = Callable[[str, str], None]

logger = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    peak_memory_bytes: int
    timed_out: bool
    oom_killed: bool


def memory_usage_bytes(stats: dict) -> int:
    memory_stats = stats.get("memory_stats", {})
    usage = int(memory_stats.get("usage", 0))
    stat_values = memory_stats.get("stats", {}) or {}
    # cgroup v2 reports inactive_file as reclaimable cache; excluding it gives
    # a more useful working-set approximation for peak memory.
    inactive_file = int(stat_values.get("inactive_file", 0))
    return max(0, usage - inactive_file)


class OutputCapture:
    """Collects stdout and stderr from container logs under one shared byte budget.

    Pump threads append under a lock and publish snapshots at most every
    SNAPSHOT_INTERVAL_S. finalize() closes the capture first, so a pump that is
    still running cannot overwrite the final output.
    """

    def __init__(self, limit: int, callback: OutputCallback | None) -> None:
        self._limit = limit
        self._callback = callback
        self._lock = threading.Lock()
        self._buffers = {"stdout": bytearray(), "stderr": bytearray()}
        self._threads: list[threading.Thread] = []
        self._last_snapshot = 0.0
        self._closed = False

    def attach(self, container) -> None:
        for stream in ("stdout", "stderr"):
            thread = threading.Thread(target=self._pump, args=(container, stream), daemon=True)
            thread.start()
            self._threads.append(thread)

    def finalize(self, container) -> None:
        for thread in self._threads:
            thread.join(timeout=JOIN_TIMEOUT_S)
        with self._lock:
            self._closed = True
            # Re-read complete logs after exit, so a log-stream race cannot drop final output.
            self._buffers = {"stdout": bytearray(), "stderr": bytearray()}
            self._append("stdout", container.logs(stdout=True, stderr=False) or b"")
            self._append("stderr", container.logs(stdout=False, stderr=True) or b"")
            self._publish()

    def texts(self) -> tuple[str, str]:
        with self._lock:
            stdout, stderr = self._text("stdout"), self._text("stderr")
        return stdout, stderr

    def _pump(self, container, stream: str) -> None:
        try:
            for chunk in container.logs(
                stream=True,
                follow=True,
                stdout=stream == "stdout",
                stderr=stream == "stderr",
            ):
                with self._lock:
                    if self._closed:
                        return
                    self._append(stream, chunk)
                    now = time.monotonic()
                    if now - self._last_snapshot >= SNAPSHOT_INTERVAL_S:
                        self._last_snapshot = now
                        self._publish()
        except DockerException:
            # Cleanup/kill races can close the log stream; finalize() re-reads the logs.
            pass

    def _append(self, stream: str, chunk: bytes) -> None:
        used = sum(len(buffer) for buffer in self._buffers.values())
        remaining = self._limit - used
        if remaining > 0:
            self._buffers[stream].extend(chunk[:remaining])

    def _text(self, stream: str) -> str:
        return self._buffers[stream].decode("utf-8", errors="replace")

    def _publish(self) -> None:
        if self._callback:
            self._callback(self._text("stdout"), self._text("stderr"))


class MemorySampler:
    """Samples container memory on a background thread and keeps the peak."""

    def __init__(self) -> None:
        self.peak = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, container) -> None:
        self._thread = threading.Thread(target=self._run, args=(container,), daemon=True)
        self._thread.start()

    def sample(self, container) -> None:
        try:
            self.peak = max(self.peak, memory_usage_bytes(container.stats(stream=False)))
        except DockerException:
            pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=JOIN_TIMEOUT_S)

    def _run(self, container) -> None:
        while not self._stop.is_set():
            self.sample(container)
            self._stop.wait(STATS_INTERVAL_S)


class SandboxRunner:
    # The source arrives on stdin (exactly argv[1] bytes), not via an env var:
    # execve() caps a single env string at ~128 KiB and env is visible through
    # `docker inspect`. /workspace is a tmpfs, so the rootfs stays read-only.
    _BOOTSTRAP = (
        "import runpy,sys;"
        "path='/workspace/main.py';"
        "size=int(sys.argv[1]);"
        "sys.argv[:]=[path];"
        "open(path,'wb').write(sys.stdin.buffer.read(size));"
        "runpy.run_path(path,run_name='__main__')"
    )

    def __init__(self, image: str, max_output_bytes: int) -> None:
        self.client = docker.from_env()
        self.image = image
        self.max_output_bytes = max_output_bytes

    def execute(
        self,
        *,
        source: str,
        timeout_ms: int,
        memory_mb: int,
        cpu_cores: float,
        pids_limit: int,
        job_id: str | None = None,
        output_callback: OutputCallback | None = None,
    ) -> ExecutionResult:
        payload = source.encode("utf-8")
        started = time.monotonic()
        capture = OutputCapture(self.max_output_bytes, output_callback)
        sampler = MemorySampler()
        container = self._create(payload, memory_mb, cpu_cores, pids_limit, job_id)
        try:
            deadline = time.monotonic() + timeout_ms / 1000
            container.start()
            try:
                self._send_source(container, payload)
            except (DockerException, OSError):
                # If the container already died, the real cause (exit code, stderr)
                # is collected below.
                pass
            capture.attach(container)
            sampler.start(container)

            timed_out = self._wait_or_kill(container, deadline)
            exit_code, oom_killed = self._exit_state(container)

            sampler.stop()
            capture.finalize(container)
            sampler.sample(container)  # last sample before removal
            stdout, stderr = capture.texts()
            return ExecutionResult(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                duration_ms=int((time.monotonic() - started) * 1000),
                peak_memory_bytes=sampler.peak,
                timed_out=timed_out,
                oom_killed=oom_killed,
            )
        finally:
            sampler.stop()
            self._remove(container)

    def _create(self, payload: bytes, memory_mb: int, cpu_cores: float, pids_limit: int, job_id: str | None):
        memory_bytes = memory_mb * 1024 * 1024
        return self.client.containers.create(
            image=self.image,
            command=["python", "-I", "-B", "-c", self._BOOTSTRAP, str(len(payload))],
            working_dir="/workspace",
            # docker-py's default detach=False sets StdinOnce, so the sandboxed
            # program sees EOF on stdin once we close our side.
            stdin_open=True,
            tty=False,
            network_mode="none",
            read_only=True,
            user=EXECUTOR_UID,
            init=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            pids_limit=pids_limit,
            mem_limit=memory_bytes,
            memswap_limit=memory_bytes,
            nano_cpus=int(cpu_cores * 1_000_000_000),
            shm_size=SHM_BYTES,
            tmpfs={
                "/workspace": f"rw,nosuid,nodev,noexec,size={TMPFS_SIZE},mode=1777",
                "/tmp": f"rw,nosuid,nodev,noexec,size={TMPFS_SIZE},mode=1777",
            },
            labels={
                "sandbox.executor": "true",
                "sandbox.job_id": job_id or "unknown",
            },
        )

    @staticmethod
    def _send_source(container, payload: bytes) -> None:
        sock = container.attach_socket(params={"stdin": 1, "stream": 1})
        raw = getattr(sock, "_sock", sock)
        try:
            raw.settimeout(SEND_TIMEOUT_S)
            raw.sendall(payload)
            raw.shutdown(socket.SHUT_WR)
        finally:
            sock.close()

    @staticmethod
    def _wait_or_kill(container, deadline: float) -> bool:
        """Returns True if the container had to be killed at the deadline."""
        while time.monotonic() < deadline:
            container.reload()
            if container.status == "exited":
                return False
            time.sleep(POLL_INTERVAL_S)
        try:
            container.kill(signal="SIGKILL")
            return True
        except APIError as exc:
            # 409: the process exited on its own between the last poll and the
            # deadline, so this is not a timeout.
            if exc.status_code != 409:
                raise
            return False
        finally:
            container.wait(timeout=KILL_WAIT_S)

    @staticmethod
    def _exit_state(container) -> tuple[int, bool]:
        container.reload()
        state = container.attrs.get("State", {})
        return int(state.get("ExitCode") or 0), bool(state.get("OOMKilled", False))

    @staticmethod
    def _remove(container) -> None:
        try:
            container.remove(force=True)
        except DockerException as exc:
            # A leaked container keeps its resources; make it visible to operators.
            logger.error("failed to remove container %s: %s", container.id[:12], exc)
