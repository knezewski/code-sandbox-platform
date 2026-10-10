from app.runner import OutputCapture, memory_usage_bytes


class FakeContainer:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b""):
        self._streams = {"stdout": stdout, "stderr": stderr}

    def logs(self, *, stdout: bool = False, stderr: bool = False, stream: bool = False, follow: bool = False):
        assert not stream, "pump threads are not started in these tests"
        if stdout and not stderr:
            return self._streams["stdout"]
        return self._streams["stderr"]


def test_final_output_shares_one_byte_budget():
    capture = OutputCapture(limit=10, callback=None)

    capture.finalize(FakeContainer(stdout=b"o" * 7, stderr=b"e" * 7))

    assert capture.texts() == ("o" * 7, "e" * 3)


def test_finalize_publishes_final_snapshot_once():
    snapshots: list[tuple[str, str]] = []
    capture = OutputCapture(limit=100, callback=lambda out, err: snapshots.append((out, err)))

    capture.finalize(FakeContainer(stdout=b"done\n", stderr=b"warn\n"))

    assert snapshots == [("done\n", "warn\n")]


def test_invalid_utf8_is_replaced_not_raised():
    capture = OutputCapture(limit=100, callback=None)

    capture.finalize(FakeContainer(stdout=b"ok\xff"))

    assert capture.texts()[0] == "ok�"


def test_memory_usage_excludes_reclaimable_file_cache():
    stats = {"memory_stats": {"usage": 100, "stats": {"inactive_file": 30}}}

    assert memory_usage_bytes(stats) == 70


def test_memory_usage_never_goes_negative():
    stats = {"memory_stats": {"usage": 10, "stats": {"inactive_file": 30}}}

    assert memory_usage_bytes(stats) == 0
