from __future__ import annotations

import os
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from azurpilot.tooling import lifecycle
from azurpilot.tooling.config import DeploySettings
from azurpilot.tooling.contracts import (
    LifecycleRecord,
    OperationState,
    RepositoryRootEvidence,
    ResultCode,
    RootSource,
)
from azurpilot.tooling.coordination import PortObservation
from azurpilot.tooling.errors import ToolingError
from azurpilot.tooling.lifecycle import LifecycleService
from azurpilot.tooling.process import ProcessIdentity
from azurpilot.tooling.repository import ResolvedRepository

_TEST_WEBUI_PORT = 49152


def _candidate(root: Path, *, argv: tuple[str, ...] | None = None) -> ProcessIdentity:
    executable = lifecycle.ProcessSpec(
        executable=lifecycle.project_python(root, DeploySettings(source_path=None)),
        argv=("gui.py",),
        cwd=root,
    ).launch_executable
    return ProcessIdentity(
        pid=12345,
        start_time=1000.0,
        executable=executable.resolve(),
        argv=argv or (str(executable.resolve()), "gui.py"),
        cwd=root.resolve(),
    )


def _root(tmp_path: Path, *, interpreter: bool = True) -> Path:
    root = tmp_path / "repository"
    venv_bin = root / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    if interpreter:
        venv_bin.mkdir(parents=True)
        (venv_bin / ("python.exe" if os.name == "nt" else "python")).touch()
    else:
        root.mkdir(parents=True)
    (root / "gui.py").write_text("", encoding="utf-8")
    return root


@pytest.mark.parametrize(
    "observation,process_changes,live",
    [
        (
            PortObservation(_TEST_WEBUI_PORT, (12345, 67890), listener_present=True),
            {},
            True,
        ),
        (
            PortObservation(
                _TEST_WEBUI_PORT, (12345,), listener_present=True, pid_unknown=True
            ),
            {},
            True,
        ),
        (
            PortObservation(_TEST_WEBUI_PORT, (12345,), listener_present=True),
            {"argv": ("python.exe", "gui.py", "--extra")},
            True,
        ),
        (
            PortObservation(_TEST_WEBUI_PORT, (12345,), listener_present=True),
            {"argv": ("<current-executable>", "./gui.py")},
            True,
        ),
        (
            PortObservation(_TEST_WEBUI_PORT, (12345,), listener_present=True),
            {"executable": "other"},
            True,
        ),
        (
            PortObservation(_TEST_WEBUI_PORT, (12345,), listener_present=True),
            {"cwd": "other"},
            True,
        ),
        (PortObservation(_TEST_WEBUI_PORT, (12345,), listener_present=True), {}, False),
    ],
)
def test_legacy_webui_recovery_rejects_ambiguous_or_mismatched_identity(
    monkeypatch, tmp_path, observation, process_changes, live
):
    root = _root(tmp_path)
    identity = _candidate(root)
    if "executable" in process_changes:
        identity = ProcessIdentity(
            pid=identity.pid,
            start_time=identity.start_time,
            executable=(root / "other.exe").resolve(),
            argv=identity.argv,
            cwd=identity.cwd,
        )
    if "cwd" in process_changes:
        other = tmp_path / "other"
        other.mkdir()
        identity = ProcessIdentity(
            pid=identity.pid,
            start_time=identity.start_time,
            executable=identity.executable,
            argv=identity.argv,
            cwd=other,
        )
    if "argv" in process_changes:
        argv = process_changes["argv"]
        if argv[0] == "<current-executable>":
            argv = (str(identity.executable), argv[1])
        identity = _candidate(root, argv=argv)
    monkeypatch.setattr(lifecycle, "iter_processes_for_root", lambda _root: (identity,))
    monkeypatch.setattr(ProcessIdentity, "matches", lambda _identity: live)

    result = LifecycleService._legacy_webui_identity(
        root,
        DeploySettings(source_path=None),
        observation,
    )

    assert result is None


def test_legacy_webui_recovery_accepts_only_exact_checkout_python_gui(
    monkeypatch, tmp_path
):
    root = _root(tmp_path)
    identity = _candidate(root)
    monkeypatch.setattr(lifecycle, "iter_processes_for_root", lambda _root: (identity,))
    monkeypatch.setattr(ProcessIdentity, "matches", lambda _identity: True)

    result = LifecycleService._legacy_webui_identity(
        root,
        DeploySettings(source_path=None),
        PortObservation(_TEST_WEBUI_PORT, (identity.pid,), listener_present=True),
    )

    assert result is identity


def test_legacy_webui_recovery_rejects_duplicate_matching_process_records(
    monkeypatch, tmp_path
):
    root = _root(tmp_path)
    identity = _candidate(root)
    monkeypatch.setattr(
        lifecycle, "iter_processes_for_root", lambda _root: (identity, identity)
    )
    monkeypatch.setattr(ProcessIdentity, "matches", lambda _identity: True)

    result = LifecycleService._legacy_webui_identity(
        root,
        DeploySettings(source_path=None),
        PortObservation(_TEST_WEBUI_PORT, (identity.pid,), listener_present=True),
    )

    assert result is None


def test_legacy_webui_recovery_fails_closed_when_project_interpreter_is_missing(
    monkeypatch, tmp_path
):
    root = _root(tmp_path, interpreter=False)
    observe_processes = Mock(side_effect=AssertionError("Нельзя перебирать процессы"))
    monkeypatch.setattr(lifecycle, "iter_processes_for_root", observe_processes)

    result = LifecycleService._legacy_webui_identity(
        root,
        DeploySettings(source_path=None),
        PortObservation(_TEST_WEBUI_PORT, (12345,), listener_present=True),
    )

    assert result is None
    observe_processes.assert_not_called()


@pytest.mark.parametrize(
    "listener_pids,identity_matches", [((67890,), True), ((12345,), False)]
)
def test_legacy_stop_rechecks_listener_owner_immediately_before_terminate(
    monkeypatch, tmp_path, listener_pids, identity_matches
):
    root = _root(tmp_path)
    identity = _candidate(root)
    settings = DeploySettings(source_path=None, webui_port=_TEST_WEBUI_PORT)
    observation = PortObservation(
        _TEST_WEBUI_PORT, listener_pids, listener_present=True
    )
    monkeypatch.setattr(lifecycle, "observe_tcp_port", lambda _port: observation)
    monkeypatch.setattr(ProcessIdentity, "matches", lambda _identity: identity_matches)
    terminate = Mock(return_value=True)
    monkeypatch.setattr(lifecycle.ProcessController, "terminate", terminate)

    with pytest.raises(ToolingError) as error:
        LifecycleService(require_infrastructure=False)._stop_legacy_webui(
            object(), settings, object(), identity, 1, "stop-test"
        )

    assert error.value.code is ResultCode.TOOLING_VERIFICATION_UNKNOWN
    terminate.assert_not_called()


@pytest.mark.parametrize(
    "capture_result,expected",
    [
        ("absent", "absent"),
        ("access_denied", "unknown"),
        ("mismatch", "absent"),
        ("same", "alive"),
    ],
)
def test_process_identity_state_separates_absent_from_unknown(
    monkeypatch, tmp_path, capture_result, expected
):
    root = _root(tmp_path)
    identity = _candidate(root)

    def capture(_cls, _pid):
        if capture_result == "absent":
            raise psutil.NoSuchProcess(pid=identity.pid)
        if capture_result == "access_denied":
            raise psutil.AccessDenied(pid=identity.pid)
        if capture_result == "mismatch":
            return ProcessIdentity(
                pid=identity.pid,
                start_time=identity.start_time + 1,
                executable=identity.executable,
                argv=identity.argv,
                cwd=identity.cwd,
            )
        return identity

    monkeypatch.setattr(ProcessIdentity, "capture", classmethod(capture))

    assert LifecycleService._process_identity_state(identity) == expected


@pytest.mark.parametrize(
    "identity_state,expected", [("absent", True), ("alive", False), ("unknown", False)]
)
def test_wait_stop_cleanup_requires_absent_identity_and_free_port(
    monkeypatch, tmp_path, identity_state, expected
):
    root = _root(tmp_path)
    identity = _candidate(root)
    settings = DeploySettings(source_path=None, webui_port=_TEST_WEBUI_PORT)
    free = PortObservation(_TEST_WEBUI_PORT, (), listener_present=False)
    monkeypatch.setattr(lifecycle, "observe_tcp_port", lambda _port: free)
    monkeypatch.setattr(
        LifecycleService,
        "_process_identity_state",
        staticmethod(lambda _identity: identity_state),
    )

    confirmed, observation = LifecycleService._wait_stop_cleanup(
        identity, settings, timeout_seconds=0
    )

    assert confirmed is expected
    assert observation is free


@pytest.mark.parametrize(
    "observation",
    [
        PortObservation(_TEST_WEBUI_PORT, (12345,), listener_present=True),
        PortObservation(
            _TEST_WEBUI_PORT,
            (),
            inspection_failed=True,
            listener_present=None,
            pid_unknown=True,
        ),
    ],
)
def test_clear_confirmed_cleanup_keeps_markers_when_port_is_not_proven_free(
    monkeypatch, observation
):
    settings = DeploySettings(source_path=None, webui_port=_TEST_WEBUI_PORT)
    cleared: list[str] = []

    class FakeCoordinator:
        def clear_lifecycle(self):
            cleared.append("lifecycle")

        def clear_stop_request(self):
            cleared.append("stop.request")

    monkeypatch.setattr(lifecycle, "observe_tcp_port", lambda _port: observation)

    with pytest.raises(ToolingError) as error:
        LifecycleService()._clear_confirmed_cleanup(
            FakeCoordinator(),
            settings,
            "stop-test",
            pid=12345,
            readiness="legacy_stopped",
        )

    assert error.value.code is ResultCode.TOOLING_CLEANUP_UNKNOWN
    assert error.value.details.cleanup_confirmed is False
    assert cleared == []


@pytest.mark.parametrize("has_stale_record", [False, True])
def test_stop_recovers_one_owned_legacy_listener_and_clears_only_lifecycle_state(
    monkeypatch, tmp_path, has_stale_record
):
    root = _root(tmp_path)
    identity = _candidate(root)
    evidence = RepositoryRootEvidence(
        source=RootSource.EXPLICIT,
        candidate_count=1,
        validation_checks=("test",),
        root_identity="a" * 24,
    )
    resolved = ResolvedRepository(root, evidence)
    settings = DeploySettings(source_path=None, webui_port=_TEST_WEBUI_PORT)
    observation = PortObservation(
        _TEST_WEBUI_PORT, (identity.pid,), listener_present=True
    )
    stale_identity = (
        ProcessIdentity(
            pid=identity.pid,
            start_time=identity.start_time + 10,
            executable=identity.executable,
            argv=identity.argv,
            cwd=identity.cwd,
        )
        if has_stale_record
        else None
    )
    stale_record = (
        LifecycleRecord(
            root_identity="a" * 24,
            pid=identity.pid,
            started_at=identity.start_time + 10,
            executable=str(identity.executable),
            argv=identity.argv,
            working_directory=str(identity.cwd),
            port=_TEST_WEBUI_PORT,
            updated_at="2026-09-25T00:00:00Z",
        )
        if has_stale_record
        else None
    )
    cleared: list[str] = []

    class FakeLock:
        def acquire(self, timeout_seconds: float = 0.0) -> bool:
            return True

        def release(self) -> None:
            return None

    class FakeCoordinator:
        def lock(self, _operation: str) -> FakeLock:
            return FakeLock()

        def read_lifecycle(self):
            return stale_record

        def clear_lifecycle(self):
            cleared.append("lifecycle")

        def clear_stop_request(self):
            cleared.append("stop.request")

    coordinator = FakeCoordinator()
    monkeypatch.setattr(
        LifecycleService,
        "_port_state",
        staticmethod(
            lambda *_args: (
                lifecycle._PortState(observation, "foreign", False),
                stale_identity,
            )
        ),
    )
    monkeypatch.setattr(
        LifecycleService,
        "_legacy_webui_identity",
        staticmethod(lambda *_args: identity),
    )
    monkeypatch.setattr(
        lifecycle.RepositoryCoordinator,
        "for_root",
        lambda _root: coordinator,
    )
    monkeypatch.setattr(lifecycle, "load_deploy_settings", lambda _root: settings)
    free = PortObservation(_TEST_WEBUI_PORT, (), listener_present=False)
    observed = iter((observation, free))
    monkeypatch.setattr(lifecycle, "observe_tcp_port", lambda _port: next(observed))
    matches = iter((True, False))
    monkeypatch.setattr(ProcessIdentity, "matches", lambda _identity: next(matches))
    terminate = Mock(return_value=True)
    monkeypatch.setattr(lifecycle.ProcessController, "terminate", terminate)
    service = LifecycleService(
        resolver=SimpleNamespace(resolve=lambda _root=None: resolved),
        runner=SimpleNamespace(),
        require_infrastructure=False,
    )
    monkeypatch.setattr(
        service,
        "_wait_stop_cleanup",
        lambda *_args: (
            True,
            PortObservation(_TEST_WEBUI_PORT, (), listener_present=False),
        ),
    )

    result = service.stop(root, timeout_seconds=1)

    assert result.ok
    assert result.state is OperationState.STOPPED
    assert result.details.readiness == "legacy_stopped"
    assert result.details.cleanup_confirmed
    terminate.assert_called_once_with(
        identity,
        timeout_seconds=1,
        include_children=False,
    )
    assert cleared == ["lifecycle", "stop.request"]


@pytest.mark.skipif(os.name != "nt", reason="Проверка идентичности процесса Windows")
def test_windows_legacy_webui_identity_terminates_only_proven_listener(tmp_path):
    root = tmp_path / "legacy-checkout"
    root.mkdir()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    (root / "gui.py").write_text(
        "import socket\n"
        "import time\n"
        "listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        f"listener.bind(('127.0.0.1', {port}))\n"
        "listener.listen(1)\n"
        "while True:\n"
        "    time.sleep(0.1)\n",
        encoding="utf-8",
    )
    settings = DeploySettings(
        source_path=None,
        python_executable=str(sys.executable),
        webui_port=port,
    )
    spec = lifecycle.ProcessSpec(
        executable=lifecycle.project_python(root, settings),
        argv=("gui.py",),
        cwd=root,
        timeout_seconds=10,
    )
    running = lifecycle.StructuredProcessRunner().start(spec)
    try:
        deadline = time.monotonic() + 5
        observation = lifecycle.observe_tcp_port(port)
        while (
            not observation.inspection_failed
            and observation.listener_present is not True
            and time.monotonic() < deadline
        ):
            if running.poll() is not None:
                pytest.fail("Временный gui.py завершился до привязки тестового порта")
            time.sleep(0.05)
            observation = lifecycle.observe_tcp_port(port)

        identity = LifecycleService._legacy_webui_identity(root, settings, observation)
        assert identity is not None
        assert identity == running.identity
        assert observation.pids == (running.pid,)
        assert not observation.pid_unknown
        assert lifecycle.ProcessController.terminate(
            identity, timeout_seconds=5, include_children=False
        )

        deadline = time.monotonic() + 5
        observation = lifecycle.observe_tcp_port(port)
        while (
            not observation.inspection_failed
            and (observation.listener_present is not False or observation.pids)
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
            observation = lifecycle.observe_tcp_port(port)

        assert not observation.inspection_failed
        assert observation.listener_present is False
        assert not observation.pids
        assert LifecycleService._process_identity_state(identity) == "absent"
    finally:
        if running.poll() is None:
            lifecycle.ProcessController.terminate(
                running.identity, timeout_seconds=5, include_children=False
            )


def test_legacy_recovery_allows_a_normal_fresh_start(monkeypatch, tmp_path):
    root = _root(tmp_path)
    legacy_identity = _candidate(root)
    fresh_identity = ProcessIdentity(
        pid=54321,
        start_time=2000.0,
        executable=legacy_identity.executable,
        argv=legacy_identity.argv,
        cwd=legacy_identity.cwd,
    )
    resolved = ResolvedRepository(
        root,
        RepositoryRootEvidence(
            source=RootSource.EXPLICIT,
            candidate_count=1,
            validation_checks=("test",),
            root_identity="b" * 24,
        ),
    )
    settings = DeploySettings(source_path=None, webui_port=_TEST_WEBUI_PORT)
    occupied = PortObservation(
        _TEST_WEBUI_PORT, (legacy_identity.pid,), listener_present=True
    )
    free = PortObservation(_TEST_WEBUI_PORT, (), listener_present=False)
    states = iter(
        (
            (lifecycle._PortState(occupied, "foreign", False), None),
            (lifecycle._PortState(free, "free", False), None),
        )
    )
    cleared: list[str] = []

    class FakeLock:
        def acquire(self, timeout_seconds: float = 0.0) -> bool:
            return True

        def release(self) -> None:
            return None

    class FakeCoordinator:
        record = None

        def lock(self, _operation: str) -> FakeLock:
            return FakeLock()

        def read_lifecycle(self):
            return self.record

        def clear_lifecycle(self):
            cleared.append("lifecycle")
            self.record = None

        def clear_stop_request(self):
            cleared.append("stop.request")

        def record_from_identity(self, identity, _root, port):
            return {"pid": identity.pid, "port": port}

        def write_lifecycle(self, record):
            self.record = record

    coordinator = FakeCoordinator()
    new_process = SimpleNamespace(
        identity=fresh_identity,
        pid=fresh_identity.pid,
        poll=lambda: None,
    )
    runner = SimpleNamespace(start=Mock(return_value=new_process))
    monkeypatch.setattr(
        LifecycleService,
        "_port_state",
        staticmethod(lambda *_args: next(states)),
    )
    monkeypatch.setattr(
        LifecycleService,
        "_legacy_webui_identity",
        staticmethod(lambda *_args: legacy_identity),
    )
    monkeypatch.setattr(
        lifecycle.RepositoryCoordinator,
        "for_root",
        lambda _root: coordinator,
    )
    monkeypatch.setattr(lifecycle, "load_deploy_settings", lambda _root: settings)
    free = PortObservation(_TEST_WEBUI_PORT, (), listener_present=False)
    observed = iter((occupied, free))
    monkeypatch.setattr(lifecycle, "observe_tcp_port", lambda _port: next(observed))
    legacy_is_running = True

    def terminate_legacy(*_args, **_kwargs):
        nonlocal legacy_is_running
        legacy_is_running = False
        return True

    def identity_matches(identity):
        if identity.pid == legacy_identity.pid:
            return legacy_is_running
        return identity.pid == fresh_identity.pid

    monkeypatch.setattr(ProcessIdentity, "matches", identity_matches)
    monkeypatch.setattr(lifecycle.ProcessController, "terminate", terminate_legacy)
    service = LifecycleService(
        resolver=SimpleNamespace(resolve=lambda _root=None: resolved),
        runner=runner,
        require_infrastructure=False,
    )
    monkeypatch.setattr(service, "_check_start_prerequisites", lambda *_args: None)
    monkeypatch.setattr(service, "_ensure_infrastructure", lambda *_args: ())
    monkeypatch.setattr(service, "_wait_stop_cleanup", lambda *_args: (True, free))
    monkeypatch.setattr(service, "_wait_readiness", lambda *_args: (True, "http_200"))

    stopped = service.stop(root, timeout_seconds=1)
    started = service.start(root, timeout_seconds=1)

    assert stopped.ok and stopped.details.cleanup_confirmed
    assert started.ok and started.state is OperationState.READY
    assert started.details.pid == fresh_identity.pid
    assert coordinator.record == {"pid": fresh_identity.pid, "port": _TEST_WEBUI_PORT}
    assert cleared == ["lifecycle", "stop.request"]
    runner.start.assert_called_once()


def test_stop_does_not_recover_legacy_process_when_ownership_is_ambiguous(
    monkeypatch, tmp_path
):
    root = _root(tmp_path)
    resolved = ResolvedRepository(
        root,
        RepositoryRootEvidence(
            source=RootSource.EXPLICIT,
            candidate_count=1,
            validation_checks=("test",),
            root_identity="a" * 24,
        ),
    )
    settings = DeploySettings(source_path=None, webui_port=_TEST_WEBUI_PORT)
    observation = PortObservation(
        _TEST_WEBUI_PORT, (12345, 67890), listener_present=True
    )

    class FakeLock:
        def acquire(self, timeout_seconds: float = 0.0) -> bool:
            return True

        def release(self) -> None:
            return None

    class FakeCoordinator:
        def lock(self, _operation: str) -> FakeLock:
            return FakeLock()

        def read_lifecycle(self):
            return None

    coordinator = FakeCoordinator()
    monkeypatch.setattr(
        lifecycle.RepositoryCoordinator,
        "for_root",
        lambda _root: coordinator,
    )
    monkeypatch.setattr(lifecycle, "load_deploy_settings", lambda _root: settings)
    monkeypatch.setattr(
        LifecycleService,
        "_port_state",
        staticmethod(
            lambda *_args: (
                lifecycle._PortState(observation, "foreign", False),
                None,
            )
        ),
    )
    recover = Mock(return_value=None)
    monkeypatch.setattr(LifecycleService, "_legacy_webui_identity", recover)
    service = LifecycleService(
        resolver=SimpleNamespace(resolve=lambda _root=None: resolved),
        runner=SimpleNamespace(),
        require_infrastructure=False,
    )

    with pytest.raises(ToolingError) as error:
        service.stop(root, timeout_seconds=1)

    assert error.value.code is ResultCode.TOOLING_PORT_CONFLICT
    recover.assert_called_once()


def test_inspect_reports_stopped_for_reused_stale_pid_without_mutating_state(
    monkeypatch, tmp_path
):
    root = _root(tmp_path)
    identity = _candidate(root)
    resolved = ResolvedRepository(
        root,
        RepositoryRootEvidence(
            source=RootSource.EXPLICIT,
            candidate_count=1,
            validation_checks=("test",),
            root_identity="c" * 24,
        ),
    )
    settings = DeploySettings(source_path=None, webui_port=_TEST_WEBUI_PORT)
    free = PortObservation(_TEST_WEBUI_PORT, (), listener_present=False)
    cleared: list[str] = []

    class FakeCoordinator:
        def read_lifecycle(self):
            return object()

        def clear_lifecycle(self):
            cleared.append("lifecycle")

        def clear_stop_request(self):
            cleared.append("stop.request")

    coordinator = FakeCoordinator()
    monkeypatch.setattr(
        lifecycle.RepositoryCoordinator,
        "for_root",
        lambda _root: coordinator,
    )
    monkeypatch.setattr(lifecycle, "load_deploy_settings", lambda _root: settings)
    monkeypatch.setattr(
        LifecycleService,
        "_port_state",
        staticmethod(
            lambda *_args: (
                lifecycle._PortState(free, "free", False),
                identity,
            )
        ),
    )
    monkeypatch.setattr(
        LifecycleService,
        "_process_identity_state",
        staticmethod(lambda _identity: "absent"),
    )
    monkeypatch.setattr(ProcessIdentity, "matches", lambda _identity: False)

    service = LifecycleService(
        resolver=SimpleNamespace(resolve=lambda _root=None: resolved),
        runner=SimpleNamespace(),
        require_infrastructure=False,
    )
    result = service.inspect(root)

    assert result.ok
    assert result.state is OperationState.STOPPED
    assert result.details.readiness == "stale_record"
    assert result.details.cleanup_confirmed is False
    assert cleared == []


def test_stop_clears_reused_stale_pid_without_terminating_foreign_process(
    monkeypatch, tmp_path
):
    root = _root(tmp_path)
    identity = _candidate(root)
    resolved = ResolvedRepository(
        root,
        RepositoryRootEvidence(
            source=RootSource.EXPLICIT,
            candidate_count=1,
            validation_checks=("test",),
            root_identity="d" * 24,
        ),
    )
    settings = DeploySettings(source_path=None, webui_port=_TEST_WEBUI_PORT)
    free = PortObservation(_TEST_WEBUI_PORT, (), listener_present=False)
    cleared: list[str] = []

    class FakeLock:
        def acquire(self, timeout_seconds: float = 0.0) -> bool:
            return True

        def release(self) -> None:
            return None

    class FakeCoordinator:
        record = object()

        def lock(self, _operation: str) -> FakeLock:
            return FakeLock()

        def read_lifecycle(self):
            return self.record

        def clear_lifecycle(self):
            cleared.append("lifecycle")
            self.record = None

        def clear_stop_request(self):
            cleared.append("stop.request")

    coordinator = FakeCoordinator()
    monkeypatch.setattr(
        lifecycle.RepositoryCoordinator,
        "for_root",
        lambda _root: coordinator,
    )
    monkeypatch.setattr(lifecycle, "load_deploy_settings", lambda _root: settings)
    monkeypatch.setattr(
        LifecycleService,
        "_port_state",
        staticmethod(
            lambda _settings, _coordinator, record: (
                lifecycle._PortState(free, "free", False),
                identity if record is not None else None,
            )
        ),
    )
    monkeypatch.setattr(
        LifecycleService,
        "_process_identity_state",
        staticmethod(lambda _identity: "absent"),
    )
    monkeypatch.setattr(lifecycle, "observe_tcp_port", lambda _port: free)
    monkeypatch.setattr(ProcessIdentity, "matches", lambda _identity: False)
    terminate = Mock(
        side_effect=AssertionError("Переиспользованный PID нельзя завершать")
    )
    monkeypatch.setattr(lifecycle.ProcessController, "terminate", terminate)

    service = LifecycleService(
        resolver=SimpleNamespace(resolve=lambda _root=None: resolved),
        runner=SimpleNamespace(),
        require_infrastructure=False,
    )
    result = service.stop(root, timeout_seconds=1)

    assert result.ok
    assert result.state is OperationState.STOPPED
    assert result.details.readiness == "stale_record_cleared"
    assert result.details.cleanup_confirmed
    assert cleared == ["lifecycle", "stop.request"]
    terminate.assert_not_called()

    status = service.inspect(root)
    assert status.ok
    assert status.state is OperationState.STOPPED
    assert status.details.readiness == "not_running"


def test_start_clears_reused_stale_pid_before_fresh_webui_launch(
    monkeypatch, tmp_path
):
    root = _root(tmp_path)
    stale_identity = _candidate(root)
    fresh_identity = ProcessIdentity(
        pid=54321,
        start_time=2000.0,
        executable=stale_identity.executable,
        argv=stale_identity.argv,
        cwd=stale_identity.cwd,
    )
    resolved = ResolvedRepository(
        root,
        RepositoryRootEvidence(
            source=RootSource.EXPLICIT,
            candidate_count=1,
            validation_checks=("test",),
            root_identity="e" * 24,
        ),
    )
    settings = DeploySettings(source_path=None, webui_port=_TEST_WEBUI_PORT)
    free = PortObservation(_TEST_WEBUI_PORT, (), listener_present=False)
    cleared: list[str] = []

    class FakeLock:
        def acquire(self, timeout_seconds: float = 0.0) -> bool:
            return True

        def release(self) -> None:
            return None

    class FakeCoordinator:
        record = object()

        def lock(self, _operation: str) -> FakeLock:
            return FakeLock()

        def read_lifecycle(self):
            return self.record

        def clear_lifecycle(self):
            cleared.append("lifecycle")
            self.record = None

        def clear_stop_request(self):
            cleared.append("stop.request")

        def record_from_identity(self, identity, _root, port):
            return {"pid": identity.pid, "port": port}

        def write_lifecycle(self, record):
            self.record = record

    coordinator = FakeCoordinator()
    running = SimpleNamespace(
        identity=fresh_identity,
        pid=fresh_identity.pid,
        poll=lambda: None,
    )
    runner = SimpleNamespace(start=Mock(return_value=running))

    monkeypatch.setattr(
        lifecycle.RepositoryCoordinator,
        "for_root",
        lambda _root: coordinator,
    )
    monkeypatch.setattr(lifecycle, "load_deploy_settings", lambda _root: settings)
    monkeypatch.setattr(
        LifecycleService,
        "_port_state",
        staticmethod(
            lambda *_args: (
                lifecycle._PortState(free, "free", False),
                stale_identity,
            )
        ),
    )
    monkeypatch.setattr(
        LifecycleService,
        "_process_identity_state",
        staticmethod(lambda _identity: "absent"),
    )
    monkeypatch.setattr(lifecycle, "observe_tcp_port", lambda _port: free)

    def identity_matches(identity):
        return identity.pid == fresh_identity.pid

    monkeypatch.setattr(ProcessIdentity, "matches", identity_matches)
    service = LifecycleService(
        resolver=SimpleNamespace(resolve=lambda _root=None: resolved),
        runner=runner,
        require_infrastructure=False,
    )
    monkeypatch.setattr(service, "_check_start_prerequisites", lambda *_args: None)
    monkeypatch.setattr(service, "_ensure_infrastructure", lambda *_args: ())
    monkeypatch.setattr(service, "_wait_readiness", lambda *_args: (True, "http_200"))

    result = service.start(root, timeout_seconds=1)

    assert result.ok
    assert result.state is OperationState.READY
    assert result.details.pid == fresh_identity.pid
    assert cleared == ["lifecycle", "stop.request"]
    assert coordinator.record == {
        "pid": fresh_identity.pid,
        "port": _TEST_WEBUI_PORT,
    }
    runner.start.assert_called_once()


def test_stale_record_recovery_keeps_state_when_process_inspection_is_unknown(
    monkeypatch, tmp_path
):
    root = _root(tmp_path)
    identity = _candidate(root)
    settings = DeploySettings(source_path=None, webui_port=_TEST_WEBUI_PORT)
    cleared: list[str] = []

    class FakeCoordinator:
        def clear_lifecycle(self):
            cleared.append("lifecycle")

        def clear_stop_request(self):
            cleared.append("stop.request")

    monkeypatch.setattr(
        LifecycleService,
        "_process_identity_state",
        staticmethod(lambda _identity: "unknown"),
    )
    service = LifecycleService(require_infrastructure=False)

    with pytest.raises(ToolingError) as error:
        service._recover_stale_lifecycle_record(
            FakeCoordinator(),
            settings,
            identity,
            "stale-test",
        )

    assert error.value.code is ResultCode.TOOLING_VERIFICATION_UNKNOWN
    assert cleared == []
