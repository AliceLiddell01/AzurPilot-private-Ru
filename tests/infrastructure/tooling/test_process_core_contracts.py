from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest
import yaml

import azurpilot.tooling.process_core as tooling_process
from azurpilot.tooling.config import load_deploy_settings, project_python
from azurpilot.tooling.contracts import ResultCode
from azurpilot.tooling.errors import ToolingError
from azurpilot.tooling.process import (
    ProcessController,
    ProcessSpec,
    StructuredProcessRunner,
)
from tests.support.paths import REPOSITORY_ROOT


def _identity(*, process_group: int | None = None) -> tooling_process.ProcessIdentity:
    return tooling_process.ProcessIdentity(
        pid=12345,
        start_time=10.0,
        executable=Path(sys.executable).resolve(),
        argv=(sys.executable, "-c", "pass"),
        cwd=REPOSITORY_ROOT.resolve(),
        process_group=process_group,
    )


class _IdentityProcess:
    def __init__(
        self,
        *,
        start_time: float = 10.0,
        executable: str | Path = sys.executable,
        argv: tuple[str, ...] = (sys.executable, "-c", "pass"),
        cwd: str | Path = REPOSITORY_ROOT,
    ) -> None:
        self._start_time = start_time
        self._executable = executable
        self._argv = argv
        self._cwd = cwd

    def create_time(self) -> float:
        return self._start_time

    def exe(self) -> str:
        return str(self._executable)

    def cmdline(self) -> list[str]:
        return list(self._argv)

    def cwd(self) -> str:
        return str(self._cwd)


@pytest.mark.parametrize(
    "changed",
    [
        {"start_time": 11.0},
        {"executable": REPOSITORY_ROOT / "other-python.exe"},
        {"argv": (sys.executable, "-c", "different")},
        {"cwd": REPOSITORY_ROOT.parent},
    ],
)
def test_process_identity_rejects_pid_reuse_or_any_identity_mismatch(
    changed: dict[str, object],
) -> None:
    identity = _identity()
    current = _IdentityProcess(**changed)

    assert identity.matches(current) is False


def _capture_error(kind: str) -> BaseException:
    if kind == "access_denied":
        return psutil.AccessDenied(pid=12345)
    if kind == "no_such_process":
        return psutil.NoSuchProcess(pid=12345)
    return OSError("не удалось прочитать поле identity")


@pytest.mark.parametrize(
    ("field", "error_kind"),
    [
        ("exe", "access_denied"),
        ("exe", "no_such_process"),
        ("exe", "os_error"),
        ("cmdline", "access_denied"),
        ("cmdline", "no_such_process"),
        ("cwd", "access_denied"),
        ("cwd", "no_such_process"),
        ("cwd", "os_error"),
    ],
)
def test_process_identity_capture_uses_only_explicit_fallback_values(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    error_kind: str,
) -> None:
    process = _IdentityProcess()
    fallback = ProcessSpec(
        executable=sys.executable,
        argv=("-c", "fallback"),
        cwd=REPOSITORY_ROOT,
    )

    def failing_method(*_args: object, **_kwargs: object) -> object:
        raise _capture_error(error_kind)

    setattr(process, field, failing_method)
    monkeypatch.setattr(tooling_process.psutil, "Process", lambda _pid: process)

    captured = tooling_process.ProcessIdentity.capture(12345, fallback)

    if field == "exe":
        # Ownership-проверки используют каноническую identity образа процесса,
        # а не путь запуска, который на POSIX может быть symlink внутри venv.
        assert captured.executable == fallback.identity_executable
    else:
        assert captured.executable == Path(sys.executable).resolve()
    if field == "cmdline":
        assert captured.argv == fallback.launch_command
    else:
        assert captured.argv == (sys.executable, "-c", "pass")
    if field == "cwd":
        assert captured.cwd == fallback.cwd
    else:
        assert captured.cwd == REPOSITORY_ROOT.resolve()


@pytest.mark.parametrize(
    ("field", "error_kind"),
    [("exe", "access_denied"), ("cwd", "os_error")],
)
def test_process_identity_capture_does_not_hide_critical_unavailability_without_fallback(
    monkeypatch: pytest.MonkeyPatch, field: str, error_kind: str
) -> None:
    process = _IdentityProcess()

    def failing_method(*_args: object, **_kwargs: object) -> object:
        raise _capture_error(error_kind)

    setattr(process, field, failing_method)
    monkeypatch.setattr(tooling_process.psutil, "Process", lambda _pid: process)

    with pytest.raises((psutil.Error, OSError)):
        tooling_process.ProcessIdentity.capture(12345)


def test_process_identity_capture_tolerates_posix_process_group_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _IdentityProcess()
    monkeypatch.setattr(tooling_process.psutil, "Process", lambda _pid: process)
    monkeypatch.setattr(
        tooling_process,
        "os",
        SimpleNamespace(name="posix", getpgid=lambda _pid: (_ for _ in ()).throw(OSError())),
    )

    captured = tooling_process.ProcessIdentity.capture(12345)

    assert captured.process_group is None


@pytest.mark.parametrize(
    ("process_state", "expected"),
    [("exact", "alive"), ("different_start", "absent"), ("unreadable", "unknown")],
)
def test_process_controller_inspect_state_is_exact_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch, process_state: str, expected: str
) -> None:
    identity = _identity()

    class InspectableProcess(_IdentityProcess):
        def is_running(self) -> bool:
            return True

    if process_state == "exact":
        process = InspectableProcess()
    elif process_state == "different_start":
        process = InspectableProcess(start_time=11.0)
    else:
        process = InspectableProcess()

        def unreadable() -> str:
            raise psutil.AccessDenied(pid=identity.pid)

        process.exe = unreadable  # type: ignore[method-assign]

    monkeypatch.setattr(tooling_process.psutil, "Process", lambda _pid: process)

    assert ProcessController.inspect_state(identity) == expected


def test_process_controller_inspect_state_returns_absent_for_missing_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tooling_process.psutil,
        "Process",
        lambda _pid: (_ for _ in ()).throw(psutil.NoSuchProcess(pid=12345)),
    )

    assert ProcessController.inspect_state(_identity()) == "absent"


class _FakeStream:
    def __init__(
        self,
        *,
        read_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.read_error = read_error
        self.close_error = close_error
        self.closed = False

    def read(self, _size: int) -> bytes:
        if self.read_error is not None:
            raise self.read_error
        return b""

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class _FakePopen:
    pid = 12345

    def __init__(
        self,
        *,
        returncode: int | None = None,
        wait_outcomes: list[BaseException | None] | None = None,
        stdout: _FakeStream | None = None,
        stderr: _FakeStream | None = None,
    ) -> None:
        self.returncode = returncode
        self.wait_outcomes = list(wait_outcomes or [])
        self.wait_calls: list[float | None] = []
        self.actions: list[str] = []
        self.stdout = stdout or _FakeStream()
        self.stderr = stderr or _FakeStream()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self.wait_outcomes:
            outcome = self.wait_outcomes.pop(0)
            if outcome is not None:
                raise outcome
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.actions.append("terminate")
        self.returncode = -signal.SIGTERM

    def kill(self) -> None:
        self.actions.append("kill")
        self.returncode = -getattr(signal, "SIGKILL", signal.SIGTERM)


class _FakeChild:
    pid = 12346

    def __init__(
        self,
        *,
        wait_error: BaseException | None = None,
        running: bool = True,
        kill_terminates: bool = True,
    ) -> None:
        self.wait_error = wait_error
        self.running = running
        self.kill_terminates = kill_terminates
        self.actions: list[str] = []

    def terminate(self) -> None:
        self.actions.append("terminate")
        self.running = False

    def kill(self) -> None:
        self.actions.append("kill")
        if self.kill_terminates:
            self.running = False

    def wait(self, timeout: float | None = None) -> None:
        del timeout
        if self.wait_error is not None:
            raise self.wait_error
        self.running = False

    def is_running(self) -> bool:
        return self.running


def test_terminate_process_does_nothing_for_exited_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakePopen(returncode=0)
    monkeypatch.setattr(
        tooling_process.ProcessIdentity,
        "matches",
        lambda _identity: pytest.fail("завершённый процесс не должен инспектироваться"),
    )
    monkeypatch.setattr(
        tooling_process.psutil,
        "Process",
        lambda _pid: pytest.fail("exited process must not enumerate children"),
    )

    tooling_process._terminate_process(process, _identity())

    assert process.actions == []


def test_terminate_process_does_nothing_after_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakePopen()
    monkeypatch.setattr(tooling_process.ProcessIdentity, "matches", lambda _identity: False)
    monkeypatch.setattr(
        tooling_process.psutil,
        "Process",
        lambda _pid: pytest.fail("чужой процесс не должен инспектироваться"),
    )

    tooling_process._terminate_process(process, _identity())

    assert process.actions == []


@pytest.mark.parametrize("exception_kind", ["no_such_process", "access_denied"])
def test_terminate_process_continues_without_descendants_when_inventory_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, exception_kind: str
) -> None:
    process = _FakePopen()
    exception = (
        psutil.NoSuchProcess(pid=12345)
        if exception_kind == "no_such_process"
        else psutil.AccessDenied(pid=12345)
    )
    monkeypatch.setattr(tooling_process.ProcessIdentity, "matches", lambda _identity: True)
    monkeypatch.setattr(
        tooling_process.psutil,
        "Process",
        lambda _pid: (_ for _ in ()).throw(exception),
    )
    monkeypatch.setattr(tooling_process, "_signal_process_group", lambda *_args: False)

    tooling_process._terminate_process(process, _identity())

    assert process.actions == ["terminate"]


def test_terminate_process_graceful_path_does_not_escalate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakePopen()
    child = _FakeChild()
    monkeypatch.setattr(tooling_process.ProcessIdentity, "matches", lambda _identity: True)
    monkeypatch.setattr(
        tooling_process.psutil,
        "Process",
        lambda _pid: SimpleNamespace(children=lambda recursive: (child,)),
    )
    monkeypatch.setattr(tooling_process, "_signal_process_group", lambda *_args: False)

    tooling_process._terminate_process(process, _identity())

    assert process.actions == ["terminate"]
    assert child.actions == ["terminate"]


def test_terminate_process_uses_group_signal_without_duplicate_individual_terminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakePopen()
    child = _FakeChild()
    signals: list[int] = []
    monkeypatch.setattr(tooling_process.ProcessIdentity, "matches", lambda _identity: True)
    monkeypatch.setattr(
        tooling_process.psutil,
        "Process",
        lambda _pid: SimpleNamespace(children=lambda recursive: (child,)),
    )
    monkeypatch.setattr(
        tooling_process,
        "_signal_process_group",
        lambda _group, signal_number: signals.append(signal_number) or True,
    )

    tooling_process._terminate_process(process, _identity(process_group=99))

    assert signals == [signal.SIGTERM]
    assert process.actions == []
    assert child.actions == []


def test_terminate_process_escalates_parent_and_descendants_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakePopen(
        wait_outcomes=[subprocess.TimeoutExpired("fake", 2.0)]
    )
    child = _FakeChild(
        wait_error=psutil.TimeoutExpired(2.0, pid=12346),
    )
    monkeypatch.setattr(tooling_process.ProcessIdentity, "matches", lambda _identity: True)
    monkeypatch.setattr(
        tooling_process.psutil,
        "Process",
        lambda _pid: SimpleNamespace(children=lambda recursive: (child,)),
    )
    monkeypatch.setattr(tooling_process, "_signal_process_group", lambda *_args: False)

    tooling_process._terminate_process(process, _identity())

    assert process.actions == ["terminate", "kill"]
    assert child.actions == ["terminate", "kill"]


def test_terminate_process_ignores_expected_termination_races(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakePopen(wait_outcomes=[subprocess.TimeoutExpired("fake", 2.0)])
    child = _FakeChild(
        wait_error=psutil.NoSuchProcess(pid=12346),
    )
    monkeypatch.setattr(tooling_process.ProcessIdentity, "matches", lambda _identity: True)
    monkeypatch.setattr(
        tooling_process.psutil,
        "Process",
        lambda _pid: SimpleNamespace(children=lambda recursive: (child,)),
    )
    monkeypatch.setattr(tooling_process, "_signal_process_group", lambda *_args: False)
    original_kill = process.kill
    process.kill = lambda: (_ for _ in ()).throw(OSError("already gone"))  # type: ignore[method-assign]

    tooling_process._terminate_process(process, _identity())

    assert child.actions == ["terminate", "kill"]
    del original_kill


def test_cleanup_process_instance_is_noop_for_exited_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakePopen(returncode=0)
    monkeypatch.setattr(
        tooling_process.psutil,
        "Process",
        lambda _pid: pytest.fail("завершённый Popen не должен перечислять дочерние процессы"),
    )

    tooling_process._cleanup_process_instance(process)

    assert process.actions == []


def test_cleanup_process_instance_uses_parent_fallback_when_children_or_group_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakePopen()
    monkeypatch.setattr(
        tooling_process.psutil,
        "Process",
        lambda _pid: (_ for _ in ()).throw(psutil.AccessDenied(pid=12345)),
    )
    monkeypatch.setattr(tooling_process, "_signal_process_group", lambda *_args: False)

    tooling_process._cleanup_process_instance(process)

    assert process.actions == ["terminate"]


def test_cleanup_process_instance_does_not_duplicate_group_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakePopen()
    child = _FakeChild()
    monkeypatch.setattr(
        tooling_process.psutil,
        "Process",
        lambda _pid: SimpleNamespace(children=lambda recursive: (child,)),
    )
    monkeypatch.setattr(tooling_process, "_signal_process_group", lambda *_args: True)

    tooling_process._cleanup_process_instance(process)

    assert process.actions == []
    assert child.actions == []


@pytest.mark.parametrize(
    "child_error",
    [
        psutil.TimeoutExpired(2.0, pid=12346),
        psutil.NoSuchProcess(pid=12346),
    ],
)
def test_cleanup_process_instance_bounds_force_path_and_child_races(
    monkeypatch: pytest.MonkeyPatch, child_error: BaseException
) -> None:
    process = _FakePopen(
        wait_outcomes=[
            subprocess.TimeoutExpired("fake", 2.0),
            subprocess.TimeoutExpired("fake", 2.0),
        ]
    )
    child = _FakeChild(wait_error=child_error)
    monkeypatch.setattr(
        tooling_process.psutil,
        "Process",
        lambda _pid: SimpleNamespace(children=lambda recursive: (child,)),
    )
    monkeypatch.setattr(tooling_process, "_signal_process_group", lambda *_args: False)

    tooling_process._cleanup_process_instance(process)

    assert process.actions == ["terminate", "kill"]
    assert child.actions == ["terminate", "kill"]


@pytest.mark.skipif(os.name == "nt", reason="проверяется только POSIX getpgid path")
def test_cleanup_process_instance_ignores_posix_getpgid_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakePopen()
    monkeypatch.setattr(
        tooling_process,
        "os",
        SimpleNamespace(
            name="posix",
            getpgid=lambda _pid: (_ for _ in ()).throw(OSError("gone")),
        ),
    )
    monkeypatch.setattr(tooling_process, "_signal_process_group", lambda *_args: False)

    tooling_process._cleanup_process_instance(process)

    assert process.actions == ["terminate"]


def _process_spec() -> ProcessSpec:
    return ProcessSpec(
        executable=sys.executable,
        argv=("-c", "pass"),
        cwd=REPOSITORY_ROOT,
        timeout_seconds=0.1,
    )


@pytest.mark.parametrize("error", [OSError("popen"), ValueError("popen")])
def test_structured_runner_start_maps_popen_failures_to_capability_unavailable(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    def fail(*_args: object, **_kwargs: object) -> object:
        raise error

    monkeypatch.setattr(tooling_process.subprocess, "Popen", fail)

    with pytest.raises(ToolingError) as caught:
        StructuredProcessRunner().start(_process_spec())

    assert caught.value.code is ResultCode.TOOLING_CAPABILITY_UNAVAILABLE
    assert isinstance(caught.value, tooling_process.ProcessStartError)
    assert caught.value.spawn_state == "not_spawned"
    assert caught.value.cleanup_state == "absent"


def test_structured_runner_start_cleans_up_after_identity_capture_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakePopen()
    cleanup_calls: list[object] = []
    monkeypatch.setattr(tooling_process.subprocess, "Popen", lambda *_args, **_kwargs: process)

    def fail_capture(*_args: object, **_kwargs: object) -> object:
        raise psutil.AccessDenied(pid=12345)

    monkeypatch.setattr(
        tooling_process.ProcessIdentity,
        "capture",
        classmethod(fail_capture),
    )
    monkeypatch.setattr(
        tooling_process,
        "_cleanup_process_instance",
        lambda candidate: cleanup_calls.append(candidate),
    )

    with pytest.raises(ToolingError) as caught:
        StructuredProcessRunner().start(_process_spec())

    assert caught.value.code is ResultCode.TOOLING_CAPABILITY_UNAVAILABLE
    assert isinstance(caught.value, tooling_process.ProcessStartError)
    assert caught.value.spawn_state == "unknown"
    assert caught.value.cleanup_state == "unknown"
    assert cleanup_calls == [process]
    assert process.stdout.closed
    assert process.stderr.closed


@pytest.mark.parametrize("error", [OSError("popen"), ValueError("popen")])
def test_structured_runner_run_maps_popen_failures_to_capability_unavailable(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    def fail(*_args: object, **_kwargs: object) -> object:
        raise error

    monkeypatch.setattr(tooling_process.subprocess, "Popen", fail)

    with pytest.raises(ToolingError) as caught:
        StructuredProcessRunner().run(_process_spec())

    assert caught.value.code is ResultCode.TOOLING_CAPABILITY_UNAVAILABLE


def test_structured_runner_run_uses_fallback_identity_when_cli_exits_during_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakePopen(returncode=0)
    cleanup_calls: list[object] = []
    monkeypatch.setattr(tooling_process.subprocess, "Popen", lambda *_args, **_kwargs: process)

    def fail_capture(*_args: object, **_kwargs: object) -> object:
        raise psutil.NoSuchProcess(pid=12345)

    monkeypatch.setattr(
        tooling_process.ProcessIdentity,
        "capture",
        classmethod(fail_capture),
    )
    monkeypatch.setattr(
        tooling_process,
        "_cleanup_process_instance",
        lambda candidate: cleanup_calls.append(candidate),
    )

    result = StructuredProcessRunner().run(_process_spec())

    assert result.returncode == 0
    assert result.timed_out is False
    assert result.identity.pid == process.pid
    assert cleanup_calls == []


@pytest.mark.parametrize("capture_error", [OSError("capture"), psutil.AccessDenied(pid=12345)])
def test_structured_runner_run_cleans_up_live_process_after_capture_failure(
    monkeypatch: pytest.MonkeyPatch, capture_error: BaseException
) -> None:
    process = _FakePopen()
    cleanup_calls: list[object] = []
    monkeypatch.setattr(tooling_process.subprocess, "Popen", lambda *_args, **_kwargs: process)

    def fail_capture(*_args: object, **_kwargs: object) -> object:
        raise capture_error

    monkeypatch.setattr(
        tooling_process.ProcessIdentity,
        "capture",
        classmethod(fail_capture),
    )
    monkeypatch.setattr(
        tooling_process,
        "_cleanup_process_instance",
        lambda candidate: cleanup_calls.append(candidate),
    )

    with pytest.raises(ToolingError) as caught:
        StructuredProcessRunner().run(_process_spec())

    assert caught.value.code is ResultCode.TOOLING_VERIFICATION_UNKNOWN
    assert cleanup_calls == [process]


@pytest.mark.parametrize("read_error", [OSError("read"), ValueError("read")])
def test_structured_runner_ignores_reader_failures_and_keeps_bounded_result(
    monkeypatch: pytest.MonkeyPatch, read_error: BaseException
) -> None:
    process = _FakePopen(
        stdout=_FakeStream(read_error=read_error),
        stderr=_FakeStream(read_error=read_error),
    )
    monkeypatch.setattr(tooling_process.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        tooling_process.ProcessIdentity,
        "capture",
        classmethod(lambda _cls, _pid, _fallback: _identity()),
    )

    result = StructuredProcessRunner().run(_process_spec())

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_structured_runner_ignores_stream_close_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakePopen(
        stdout=_FakeStream(close_error=OSError("close")),
        stderr=_FakeStream(close_error=ValueError("close")),
    )
    monkeypatch.setattr(tooling_process.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        tooling_process.ProcessIdentity,
        "capture",
        classmethod(lambda _cls, _pid, _fallback: _identity()),
    )

    result = StructuredProcessRunner().run(_process_spec())

    assert result.returncode == 0
    assert result.timed_out is False


def test_structured_runner_bounds_second_wait_after_timeout_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakePopen(
        wait_outcomes=[
            subprocess.TimeoutExpired("fake", 0.1),
            subprocess.TimeoutExpired("fake", 3.0),
        ]
    )
    terminate_calls: list[object] = []
    monkeypatch.setattr(tooling_process.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        tooling_process.ProcessIdentity,
        "capture",
        classmethod(lambda _cls, _pid, _fallback: _identity()),
    )
    monkeypatch.setattr(
        tooling_process,
        "_terminate_process",
        lambda candidate, identity: terminate_calls.append((candidate, identity)),
    )
    monkeypatch.setattr(
        tooling_process.ProcessController,
        "inspect_state",
        staticmethod(lambda _identity: "unknown"),
    )

    result = StructuredProcessRunner().run(_process_spec())

    assert result.timed_out is True
    assert result.termination_state == "unknown"
    assert terminate_calls == [(process, result.identity)]
    assert len(process.wait_calls) == 2


def test_process_controller_rejects_identity_mismatch_without_psutil_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    monkeypatch.setattr(tooling_process.ProcessIdentity, "matches", lambda _identity: False)
    monkeypatch.setattr(
        tooling_process.psutil,
        "Process",
        lambda _pid: pytest.fail("несовпавшая идентичность должна останавливать проверку до поиска"),
    )

    assert ProcessController.terminate(identity, timeout_seconds=0.01) is False


def test_process_controller_treats_no_such_process_after_match_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    monkeypatch.setattr(tooling_process.ProcessIdentity, "matches", lambda _identity: True)
    monkeypatch.setattr(
        tooling_process.psutil,
        "Process",
        lambda _pid: (_ for _ in ()).throw(psutil.NoSuchProcess(pid=12345)),
    )

    assert ProcessController.terminate(identity, timeout_seconds=0.01) is True


@pytest.mark.parametrize("children_error", [psutil.NoSuchProcess(pid=12346), psutil.AccessDenied(pid=12346)])
def test_process_controller_continues_when_descendant_inventory_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, children_error: BaseException
) -> None:
    identity = _identity()
    process = _FakeChild(running=True)
    process.children = lambda recursive: (_ for _ in ()).throw(children_error)  # type: ignore[attr-defined]
    matches = iter((True, False))
    monotonic = iter((0.0, 1.0))
    monkeypatch.setattr(tooling_process.ProcessIdentity, "matches", lambda _identity: next(matches, False))
    monkeypatch.setattr(tooling_process.psutil, "Process", lambda _pid: process)
    monkeypatch.setattr(tooling_process, "_signal_process_group", lambda *_args: False)
    monkeypatch.setattr(tooling_process.time, "monotonic", lambda: next(monotonic, 1.0))
    monkeypatch.setattr(tooling_process.time, "sleep", lambda _seconds: None)

    assert ProcessController.terminate(identity, timeout_seconds=0.1) is True
    assert process.actions == ["terminate"]


def test_process_controller_returns_true_after_graceful_owned_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity()
    process = _FakeChild(running=True)
    process.children = lambda recursive: ()  # type: ignore[attr-defined]
    matches = iter((True, False))
    monotonic = iter((0.0, 1.0))
    monkeypatch.setattr(tooling_process.ProcessIdentity, "matches", lambda _identity: next(matches, False))
    monkeypatch.setattr(tooling_process.psutil, "Process", lambda _pid: process)
    monkeypatch.setattr(tooling_process, "_signal_process_group", lambda *_args: False)
    monkeypatch.setattr(tooling_process.time, "monotonic", lambda: next(monotonic, 1.0))
    monkeypatch.setattr(tooling_process.time, "sleep", lambda _seconds: None)

    assert ProcessController.terminate(identity, timeout_seconds=0.1) is True
    assert process.actions == ["terminate"]


@pytest.mark.parametrize(
    ("identity_matches_after_kill", "kill_terminates", "expected"),
    [(False, True, True), (True, False, False)],
)
def test_process_controller_force_path_reports_actual_postcondition(
    monkeypatch: pytest.MonkeyPatch,
    identity_matches_after_kill: bool,
    kill_terminates: bool,
    expected: bool,
) -> None:
    identity = _identity()
    process = _FakeChild(running=True)
    child = _FakeChild(running=True, kill_terminates=kill_terminates)
    process.children = lambda recursive: (child,)  # type: ignore[attr-defined]
    matches = iter((True, True, identity_matches_after_kill))
    monotonic = iter((0.0, 1.0))
    monkeypatch.setattr(tooling_process.ProcessIdentity, "matches", lambda _identity: next(matches, identity_matches_after_kill))
    monkeypatch.setattr(tooling_process.psutil, "Process", lambda _pid: process)
    monkeypatch.setattr(tooling_process, "_signal_process_group", lambda *_args: False)
    monkeypatch.setattr(tooling_process.time, "monotonic", lambda: next(monotonic, 1.0))
    monkeypatch.setattr(tooling_process.time, "sleep", lambda _seconds: None)

    assert ProcessController.terminate(identity, timeout_seconds=0.1) is expected
    assert process.actions == ["terminate", "kill"]
    assert child.actions == ["terminate", "kill"]


@pytest.mark.skipif(os.name != "nt", reason="контракт перенаправителя Windows venv")
@pytest.mark.parametrize(
    "case",
    [
        "not_python_executable",
        "not_scripts_directory",
        "no_home",
        "nul_home",
        "relative_home",
        "missing_runtime",
    ],
)
def test_windows_venv_runtime_rejects_invalid_redirector_metadata(
    tmp_path: Path, case: str
) -> None:
    venv = tmp_path / "venv"
    scripts = venv / "Scripts"
    scripts.mkdir(parents=True)
    if case == "not_scripts_directory":
        executable = venv / "bin" / "python.exe"
        executable.parent.mkdir()
    elif case == "not_python_executable":
        executable = scripts / "pythonw.exe"
    else:
        executable = scripts / "python.exe"
    executable.write_bytes(b"redirector")
    config = venv / "pyvenv.cfg"
    if case == "no_home":
        config.write_text("prompt = test\n", encoding="utf-8")
    elif case == "nul_home":
        config.write_text("home = C:\\safe\x00bad\n", encoding="utf-8")
    elif case == "relative_home":
        config.write_text("home = relative-runtime\n", encoding="utf-8")
    elif case == "missing_runtime":
        config.write_text(f"home = {tmp_path / 'missing-home'}\n", encoding="utf-8")
    else:
        config.write_text("home = C:\\missing-home\n", encoding="utf-8")

    assert tooling_process._windows_venv_runtime(executable) is None


@pytest.mark.skipif(os.name != "nt", reason="контракт перенаправителя Windows venv")
def test_windows_venv_runtime_rejects_config_read_and_unsafe_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    venv = tmp_path / "venv"
    scripts = venv / "Scripts"
    home = tmp_path / "runtime-home"
    scripts.mkdir(parents=True)
    home.mkdir()
    executable = scripts / "python.exe"
    executable.write_bytes(b"redirector")
    runtime = home / "python.exe"
    runtime.write_bytes(b"python")
    config = venv / "pyvenv.cfg"
    config.write_text(f"home = {home}\n", encoding="utf-8")

    monkeypatch.setattr(
        tooling_process,
        "bounded_read_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read")),
    )
    assert tooling_process._windows_venv_runtime(executable) is None

    monkeypatch.undo()
    monkeypatch.setattr(
        tooling_process,
        "is_unsafe_path",
        lambda path: Path(path) == config,
    )
    assert tooling_process._windows_venv_runtime(executable) is None

    monkeypatch.undo()
    monkeypatch.setattr(
        tooling_process,
        "is_unsafe_path",
        lambda path: Path(path) == runtime.resolve(),
    )
    assert tooling_process._windows_venv_runtime(executable) is None

    monkeypatch.undo()
    original_is_file = Path.is_file

    def fail_runtime_stat(path: Path) -> bool:
        if path == runtime:
            raise OSError("stat")
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", fail_runtime_stat)
    assert tooling_process._windows_venv_runtime(executable) is None


def test_windows_venv_runtime_returns_none_on_non_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "Scripts" / "python.exe"
    monkeypatch.setattr(
        tooling_process,
        "os",
        SimpleNamespace(name="posix"),
    )

    assert tooling_process._windows_venv_runtime(executable) is None


_POSIX_VENV_REQUIRED = pytest.mark.skipif(
    os.name == "nt", reason="контракт POSIX venv symlink"
)


def _create_posix_venv(tmp_path: Path, *, name: str = "venv") -> Path:
    """Создать герметичный POSIX venv с venv-only модулем в site-packages."""

    venv = tmp_path.resolve() / name
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)],
        check=True,
        capture_output=True,
    )
    python = venv / "bin" / "python"
    if not python.is_symlink():
        # Копирующий режим ``venv`` сохраняет раскладку и site-packages, но
        # подменяет интерпретатор обычным файлом. Возвращаем каноничную для
        # POSIX symlink-раскладку, чтобы контракт запуска проверялся всегда,
        # а не молча пропускался на платформе без symlink-интерпретатора.
        python.unlink()
        python.symlink_to(Path(sys.executable).resolve())
    purelib = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (Path(purelib) / "azurpilot_venv_probe.py").write_text(
        "MARKER = 'venv-only'\n", encoding="utf-8"
    )
    return python


def _venv_probe_argv() -> tuple[str, ...]:
    return (
        "-X",
        "utf8",
        "-c",
        (
            "import json, sys\n"
            "report = {"
            "'prefix': sys.prefix, "
            "'base_prefix': sys.base_prefix, "
            "'executable': sys.executable}\n"
            "try:\n"
            "    import azurpilot_venv_probe as probe\n"
            "except Exception as error:\n"
            "    report['module_error'] = f'{type(error).__name__}: {error}'\n"
            "else:\n"
            "    report['module_marker'] = probe.MARKER\n"
            "print(json.dumps(report))\n"
        ),
    )


@_POSIX_VENV_REQUIRED
def test_posix_venv_logical_executable_keeps_venv_semantics_in_child(
    tmp_path: Path,
) -> None:
    """Регрессия: канонизация не должна подменять venv-интерпретатор базовым."""

    python = _create_posix_venv(tmp_path)
    spec = ProcessSpec(
        executable=python,
        argv=_venv_probe_argv(),
        cwd=REPOSITORY_ROOT,
        timeout_seconds=60.0,
    )

    assert spec.launch_executable == python
    assert spec.identity_executable == python.resolve()
    assert spec.identity_executable == spec.resolved_executable

    result = StructuredProcessRunner().run(spec)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert "module_error" not in report
    assert report["module_marker"] == "venv-only"
    assert report["prefix"] == str(python.parent.parent)
    assert report["base_prefix"] != report["prefix"]
    assert report["executable"] == str(python)


@_POSIX_VENV_REQUIRED
def test_posix_venv_start_keeps_canonical_identity_and_ownership(
    tmp_path: Path,
) -> None:
    """Идентичность запущенного venv-процесса остаётся канонической и совпадает с ожидаемым образом."""

    python = _create_posix_venv(tmp_path)
    spec = ProcessSpec(
        executable=python,
        argv=("-c", "import time; time.sleep(30)"),
        cwd=REPOSITORY_ROOT,
        timeout_seconds=60.0,
    )

    running = StructuredProcessRunner().start(spec)
    try:
        assert running.identity.executable == spec.identity_executable
        assert running.identity.executable != spec.launch_executable
        assert running.identity.argv == (str(spec.launch_executable), *spec.argv)
        assert running.identity.cwd == REPOSITORY_ROOT.resolve()
        assert running.identity.matches() is True
        assert ProcessController.inspect(running.identity) is True
        assert ProcessController.inspect_state(running.identity) == "alive"
    finally:
        assert ProcessController.terminate(running.identity, timeout_seconds=15.0) is True


def _configured_linux_checkout(tmp_path: Path) -> tuple[Path, Path]:
    """Создать checkout с настоящим ``.venv`` и штатным ``config/deploy.yaml`` Linux."""

    root = tmp_path.resolve() / "repository"
    (root / "config").mkdir(parents=True)
    python = _create_posix_venv(root, name=".venv")
    shutil.copy2(
        REPOSITORY_ROOT / "config" / "deploy.template-linux.yaml",
        root / "config" / "deploy.yaml",
    )
    return root, python


@_POSIX_VENV_REQUIRED
def test_configured_linux_template_python_keeps_venv_semantics(tmp_path: Path) -> None:
    """Регрессия: configured ``./.venv/bin/python`` доходит до process layer логически."""

    root, expected = _configured_linux_checkout(tmp_path)
    settings = load_deploy_settings(root)
    template = yaml.safe_load(
        (REPOSITORY_ROOT / "config" / "deploy.template-linux.yaml").read_text(
            encoding="utf-8"
        )
    )["Deploy"]

    # Configured значение обязано быть штатным значением Linux template, иначе
    # регрессия проверяла бы не тот путь, который ломается у оператора.
    assert settings.python_executable == template["Python"]["PythonExecutable"]

    python = project_python(root, settings)

    assert python == expected
    assert python.parent == root / ".venv" / "bin"
    assert python != python.resolve()

    spec = ProcessSpec(
        executable=python,
        argv=_venv_probe_argv(),
        cwd=root,
        timeout_seconds=60.0,
    )

    assert spec.launch_executable == python
    assert spec.identity_executable == python.resolve()
    assert spec.identity_executable != spec.launch_executable

    result = StructuredProcessRunner().run(spec)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert "module_error" not in report
    assert report["module_marker"] == "venv-only"
    assert report["prefix"] == str(root / ".venv")
    assert report["base_prefix"] != report["prefix"]
    assert report["executable"] == str(python)


@_POSIX_VENV_REQUIRED
@pytest.mark.parametrize("absolute", [False, True])
def test_configured_non_venv_python_keeps_canonical_launch(
    tmp_path: Path, absolute: bool
) -> None:
    """Configured custom Python вне venv-разметки не получает ложную venv-семантику."""

    root, _ = _configured_linux_checkout(tmp_path)
    tools = root / "tools"
    tools.mkdir()
    custom = tools / "python"
    custom.symlink_to(Path(sys.executable).resolve())
    settings = replace(
        load_deploy_settings(root),
        python_executable=str(custom) if absolute else "./tools/python",
    )

    python = project_python(root, settings)

    assert python == custom
    spec = ProcessSpec(
        executable=python,
        argv=_venv_probe_argv(),
        cwd=root,
        timeout_seconds=60.0,
    )

    assert spec.launch_executable == spec.resolved_executable == custom.resolve()

    result = StructuredProcessRunner().run(spec)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    module_error = report.get("module_error", "")
    assert "ModuleNotFoundError" in module_error
    assert "azurpilot_venv_probe" in module_error
    assert report["prefix"] == report["base_prefix"]


@_POSIX_VENV_REQUIRED
def test_configured_missing_python_falls_back_to_project_venv(tmp_path: Path) -> None:
    """Несуществующий configured путь оставляет fallback на venv проекта."""

    root, expected = _configured_linux_checkout(tmp_path)
    settings = replace(
        load_deploy_settings(root), python_executable="./tools/absent-python"
    )

    python = project_python(root, settings)

    assert python == expected
    spec = ProcessSpec(
        executable=python,
        argv=_venv_probe_argv(),
        cwd=root,
        timeout_seconds=60.0,
    )

    result = StructuredProcessRunner().run(spec)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["module_marker"] == "venv-only"
    assert report["prefix"] == str(root / ".venv")


@_POSIX_VENV_REQUIRED
@pytest.mark.parametrize(
    "case",
    [
        "no_marker",
        "missing_home",
        "nul_home",
        "marker_is_symlink",
        "home_is_file",
        "relative_home",
    ],
)
def test_posix_venv_launch_refuses_unproven_or_unsafe_venv_marker(
    tmp_path: Path, case: str
) -> None:
    """Без подтверждённой venv-разметки запускается только канонический runtime."""

    layout = tmp_path / "layout"
    (layout / "bin").mkdir(parents=True)
    python = layout / "bin" / "python"
    python.symlink_to(Path(sys.executable).resolve())
    marker = layout / "pyvenv.cfg"
    if case == "marker_is_symlink":
        target = tmp_path / "real-pyvenv.cfg"
        target.write_text("home = /usr\n", encoding="utf-8")
        marker.symlink_to(target)
    elif case == "home_is_file":
        # ``home`` на файл ломает вычисление путей дочернего интерпретатора,
        # поэтому такой маркер не принимается.
        target = tmp_path / "runtime-is-file"
        target.write_bytes(b"")
        marker.write_text(f"home = {target}\n", encoding="utf-8")
    elif case != "no_marker":
        marker.write_text(
            {
                "missing_home": "prompt = synthetic\n",
                "nul_home": "home = /us\x00r\n",
                "relative_home": "home = runtime\n",
            }[case],
            encoding="utf-8",
        )

    spec = ProcessSpec(executable=python, argv=("-c", "pass"), cwd=REPOSITORY_ROOT)

    assert spec.launch_executable == spec.resolved_executable
    assert spec.launch_executable == Path(sys.executable).resolve()


@_POSIX_VENV_REQUIRED
def test_posix_venv_launch_refuses_relative_path_from_path_lookup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Относительный результат поиска по PATH не запускается как есть.

    Popen искал бы такое имя уже относительно ``cwd`` дочернего процесса, то есть
    мог бы исполнить файл, который здесь не проверялся.
    """

    work = tmp_path.resolve() / "work"
    work.mkdir()
    (work / "python").symlink_to(Path(sys.executable).resolve())
    (work / "pyvenv.cfg").write_text("home = /usr/sbin\n", encoding="utf-8")

    monkeypatch.chdir(work)
    monkeypatch.setenv("PATH", ".")
    found = shutil.which("python")
    assert found is not None and not Path(found).is_absolute()

    spec = ProcessSpec(executable="python", argv=("-c", "pass"), cwd=work)

    assert spec.launch_executable == spec.resolved_executable
    assert spec.launch_executable == Path(sys.executable).resolve()


@_POSIX_VENV_REQUIRED
def test_posix_venv_launch_uses_logical_path_only_for_proven_venv_marker(
    tmp_path: Path,
) -> None:
    """Логический путь выбирается только при подтверждённой venv-разметке."""

    layout = tmp_path / "layout"
    (layout / "bin").mkdir(parents=True)
    python = layout / "bin" / "python"
    python.symlink_to(Path(sys.executable).resolve())
    (layout / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")

    spec = ProcessSpec(executable=python, argv=("-c", "pass"), cwd=REPOSITORY_ROOT)

    assert spec.launch_executable == python
    assert spec.resolved_executable == Path(sys.executable).resolve()
    assert spec.identity_executable == spec.resolved_executable
    # Evidence результата остаётся каноническим, а argv запуска — логическим.
    assert spec.command == (str(spec.resolved_executable), "-c", "pass")
    assert spec.launch_command == (str(python), "-c", "pass")
    # Маркер перенаправителя Windows venv на POSIX не выставляется.
    assert spec.launch_environment.get("__PYVENV_LAUNCHER__") is None


@_POSIX_VENV_REQUIRED
def test_posix_venv_launch_resolves_relative_executable_through_cwd(
    tmp_path: Path,
) -> None:
    """Относительный executable разрешается через cwd и сохраняет venv-путь."""

    layout = tmp_path.resolve() / "layout"
    (layout / ".venv" / "bin").mkdir(parents=True)
    python = layout / ".venv" / "bin" / "python"
    python.symlink_to(Path(sys.executable).resolve())
    (layout / ".venv" / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")

    spec = ProcessSpec(
        executable=Path(".venv/bin/python"), argv=("-c", "pass"), cwd=layout
    )

    assert spec.launch_executable == python
    assert spec.resolved_executable == Path(sys.executable).resolve()
    assert spec.identity_executable == spec.resolved_executable


@_POSIX_VENV_REQUIRED
def test_posix_venv_launch_stays_canonical_when_path_travels_through_link(
    tmp_path: Path,
) -> None:
    """Через symlink-предок разметка venv не подтверждается: запуск канонический.

    Ограничение осознанное и fail-closed: общий безопасный читатель разметки
    отвергает link-like предков, поэтому такая рабочая копия возвращается к
    прежнему поведению вместо запуска логического пути без доказательства.
    """

    real = tmp_path.resolve() / "real-work"
    (real / ".venv" / "bin").mkdir(parents=True)
    (real / ".venv" / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
    python = real / ".venv" / "bin" / "python"
    python.symlink_to(Path(sys.executable).resolve())
    link = tmp_path.resolve() / "linked-work"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - среда без прав на symlink
        pytest.skip("среда не разрешает symlink для проверки link-like предка")

    through_link = link / ".venv" / "bin" / "python"
    spec = ProcessSpec(executable=through_link, argv=("-c", "pass"), cwd=real)

    assert tooling_process._venv_marker_home(through_link.parent.parent) is None
    assert spec.launch_executable == spec.resolved_executable
    assert spec.launch_executable == Path(sys.executable).resolve()


def test_posix_venv_launch_ignores_canonical_executable_without_link(tmp_path: Path) -> None:
    """Канонический файл внутри venv запускается как есть, без подмены пути."""

    layout = tmp_path / "layout"
    (layout / "bin").mkdir(parents=True)
    python = layout / "bin" / "python"
    python.write_bytes(b"")
    (layout / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")

    spec = ProcessSpec(executable=python, argv=("-c", "pass"), cwd=REPOSITORY_ROOT)

    assert spec.launch_executable == spec.resolved_executable == python


def test_venv_marker_home_is_shared_bounded_metadata_source(tmp_path: Path) -> None:
    """Общий читатель venv-маркера принимает только безопасную непустую разметку."""

    venv = tmp_path / "venv"
    venv.mkdir()
    marker = venv / "pyvenv.cfg"

    assert tooling_process._venv_marker_home(venv) is None

    marker.write_text("home = /opt/runtime\n", encoding="utf-8")
    assert tooling_process._venv_marker_home(venv) == "/opt/runtime"

    marker.write_text("prompt = synthetic\n", encoding="utf-8")
    assert tooling_process._venv_marker_home(venv) is None

    marker.write_text("home = /us\x00r\n", encoding="utf-8")
    assert tooling_process._venv_marker_home(venv) is None

    marker.write_text(
        "home = /opt/runtime\n" + "#" * tooling_process._VENV_CONFIG_LIMIT,
        encoding="utf-8",
    )
    assert tooling_process._venv_marker_home(venv) is None

    marker.unlink()
    target = tmp_path / "real-pyvenv.cfg"
    target.write_text("home = /opt/runtime\n", encoding="utf-8")
    try:
        marker.symlink_to(target)
    except (OSError, NotImplementedError):  # pragma: no cover - среда без прав на symlink
        pytest.skip("среда не разрешает symlink для проверки link-like разметки venv")
    assert tooling_process._venv_marker_home(venv) is None


def test_windows_venv_runtime_resolves_proven_home_runtime_on_any_platform(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ветка перенаправителя Windows venv использует общий читатель маркера без регрессий."""

    monkeypatch.setattr(tooling_process, "os", SimpleNamespace(name="nt"))
    venv = tmp_path / "venv"
    scripts = venv / "Scripts"
    scripts.mkdir(parents=True)
    executable = scripts / "python.exe"
    executable.write_bytes(b"redirector")
    home = tmp_path / "home"
    home.mkdir()
    runtime = home / "python.exe"
    runtime.write_bytes(b"python")
    marker = venv / "pyvenv.cfg"
    marker.write_text(f"home = {home}\n", encoding="utf-8")

    assert tooling_process._windows_venv_runtime(executable) == runtime.resolve()

    missing_runtime_home = tmp_path / "empty-home"
    missing_runtime_home.mkdir()
    marker.write_text(f"home = {missing_runtime_home}\n", encoding="utf-8")
    assert tooling_process._windows_venv_runtime(executable) is None
