from pathlib import Path
from types import SimpleNamespace

import pytest

import conftest as pytest_plugin


class _FakeLog:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.wait_timeouts = []

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        self.returncode = -15
        return self.returncode


def test_parallel_runner_cleans_processes_and_logs_when_spawn_fails(monkeypatch):
    process = _FakeProcess()
    logs = []
    popen_calls = 0

    def fake_open(self, *args, **kwargs):
        log = _FakeLog()
        logs.append(log)
        return log

    def fake_popen(*args, **kwargs):
        nonlocal popen_calls
        popen_calls += 1
        if popen_calls == 1:
            return process
        raise OSError("искусственный сбой запуска shard-процесса")

    monkeypatch.setenv(pytest_plugin._PARALLEL_ENV, "1")
    monkeypatch.delenv(pytest_plugin._CHILD_ENV, raising=False)
    monkeypatch.setattr(
        pytest_plugin,
        "_test_files",
        lambda _root: (Path("tests/a.py"), Path("tests/b.py")),
    )
    monkeypatch.setattr(pytest_plugin, "_parallel_shard_count", lambda _count: 2)
    monkeypatch.setattr(
        pytest_plugin,
        "_split_test_files",
        lambda _files, _count: (
            (Path("tests/a.py"),),
            (Path("tests/b.py"),),
        ),
    )
    monkeypatch.setattr(Path, "open", fake_open)
    monkeypatch.setattr(pytest_plugin.subprocess, "Popen", fake_popen)
    config = SimpleNamespace(
        invocation_params=SimpleNamespace(args=("tests",)),
    )

    with pytest.raises(OSError, match="искусственный сбой запуска"):
        pytest_plugin.pytest_cmdline_main(config)

    assert process.terminated is True
    assert process.wait_timeouts == [10]
    assert len(logs) == 2
    assert all(log.closed for log in logs)


def test_parallel_cleanup_escalates_to_kill_after_terminate_timeout():
    class SlowProcess(_FakeProcess):
        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            if not self.killed:
                raise pytest_plugin.subprocess.TimeoutExpired("pytest", timeout)
            self.returncode = -9
            return self.returncode

    process = SlowProcess()
    pytest_plugin._stop_parallel_processes(
        [(process, Path("shard.log"), _FakeLog(), 1)]
    )

    assert process.terminated is True
    assert process.killed is True
    assert process.wait_timeouts == [10, 10]
    assert process.returncode == -9
