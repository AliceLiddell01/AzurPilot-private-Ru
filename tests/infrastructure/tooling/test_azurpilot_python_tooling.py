"""Контракты первого Python tooling vertical slice."""

from __future__ import annotations

import io
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest
from pydantic import ValidationError

import azurpilot.tooling.coordination as tooling_coordination
import azurpilot.tooling.path as tooling_path
import azurpilot.tooling.process as tooling_process
from azurpilot.cli import main
from azurpilot.tooling import adb as tooling_adb
from azurpilot.tooling import bootstrap as tooling_bootstrap
from azurpilot.tooling import doctor as tooling_doctor
from azurpilot.tooling.config import DeploySettings, project_python
from azurpilot.tooling.contracts import (
    DoctorDetails,
    RepositoryRootEvidence,
    ResultCode,
    RootSource,
    ToolingResult,
)
from azurpilot.tooling.coordination import FileLock, observe_tcp_port
from azurpilot.tooling.doctor import DoctorService
from azurpilot.tooling.errors import RepositoryResolutionError, ToolingError
from azurpilot.tooling.filesystem import ScopedPath, StateLayout
from azurpilot.tooling.process import (
    ProcessController,
    ProcessSpec,
    StructuredProcessRunner,
    public_argv,
    safe_environment,
)
from azurpilot.tooling.repository import RepositoryResolver, ResolvedRepository
from tests.support.paths import REPOSITORY_ROOT


def test_closed_result_rejects_unknown_properties() -> None:
    with pytest.raises(ValidationError):
        DoctorDetails(checks=(), healthy=True, unexpected="value")

    result = ToolingResult[DoctorDetails, RepositoryRootEvidence](
        ok=True,
        code=ResultCode.OK,
        state="ready",
        message="Проверка завершена.",
        details=DoctorDetails(checks=(), healthy=True),
        evidence=RepositoryRootEvidence(
            source=RootSource.EXPLICIT,
            candidate_count=1,
            validation_checks=("test",),
            root_identity="0" * 16,
        ),
    )
    assert result.warnings == ()
    assert "unexpected" not in result.model_dump_json()


def test_explicit_root_does_not_fallback() -> None:
    resolver = RepositoryResolver()
    with pytest.raises(RepositoryResolutionError) as error:
        resolver.resolve(REPOSITORY_ROOT / "missing-root")
    assert error.value.code is ResultCode.TOOLING_REPOSITORY_NOT_FOUND


def test_untrusted_cwd_is_not_a_discovery_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for variable in (
        "AZURPILOT_REPOSITORY_ROOT",
        "AZURPILOT_USER_CONFIG",
        "AZURPILOT_MACHINE_CONFIG",
        "AZURPILOT_CONFIG_FILE",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.chdir(tmp_path)
    resolver = RepositoryResolver()
    monkeypatch.setattr(resolver, "_installation_candidates", lambda: ())
    with pytest.raises(RepositoryResolutionError) as error:
        resolver.resolve()
    assert error.value.code is ResultCode.TOOLING_REPOSITORY_NOT_FOUND


def test_user_configuration_is_a_real_root_resolution_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "azurpilot.toml"
    config_path.write_text(
        "[repository]\nroot = '" + str(REPOSITORY_ROOT).replace("\\", "/") + "'\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("AZURPILOT_REPOSITORY_ROOT", raising=False)
    monkeypatch.setenv("AZURPILOT_CONFIG_FILE", str(config_path))
    monkeypatch.setattr(RepositoryResolver, "_installation_candidates", lambda self: ())

    resolved = RepositoryResolver().resolve()

    assert resolved.path == REPOSITORY_ROOT.resolve()
    assert resolved.source is RootSource.CONFIGURED


def test_configured_roots_conflict_within_one_priority_level(monkeypatch: pytest.MonkeyPatch) -> None:
    resolver = RepositoryResolver()
    roots = (REPOSITORY_ROOT, REPOSITORY_ROOT.parent)
    monkeypatch.setattr(resolver, "_configured_candidates", lambda: roots)

    def fake_validate(candidate: Path, source: RootSource, count: int) -> ResolvedRepository:
        return ResolvedRepository(
            candidate,
            RepositoryRootEvidence(
                source=source,
                candidate_count=count,
                validation_checks=("test",),
                root_identity="1" * 16,
            ),
        )

    monkeypatch.setattr(resolver, "_validate", fake_validate)
    with pytest.raises(RepositoryResolutionError) as error:
        resolver.resolve()
    assert error.value.code is ResultCode.TOOLING_REPOSITORY_AMBIGUOUS


def test_process_runner_bounds_output_and_preserves_argv() -> None:
    runner = StructuredProcessRunner()
    result = runner.run(
        ProcessSpec(
            executable=sys.executable,
            argv=(
                "-c",
                "import sys; print('x' * 200); print('y' * 200, file=sys.stderr)",
            ),
            cwd=REPOSITORY_ROOT,
            timeout_seconds=10,
            max_output_bytes=32,
        )
    )
    assert result.ok
    assert len(result.stdout.encode("utf-8")) <= 32
    assert len(result.stderr.encode("utf-8")) <= 32
    assert result.stdout_truncated
    assert result.stderr_truncated
    assert public_argv(("--password=secret", "--name=ok")) == ("--password=<redacted>", "--name=ok")
    assert public_argv((str(REPOSITORY_ROOT / "private.txt"), "--config=" + str(REPOSITORY_ROOT / "config"))) == ("<path>", "--config=<path>")


def test_process_environment_policy_rejects_unapproved_explicit_values() -> None:
    with pytest.raises(ValueError):
        safe_environment({"SECRET_VALUE": "must-not-be-inherited"})


def test_process_runner_classifies_timeout() -> None:
    started = time.monotonic()
    result = StructuredProcessRunner().run(
        ProcessSpec(
            executable=sys.executable,
            argv=("-c", "import time; time.sleep(2)"),
            cwd=REPOSITORY_ROOT,
            timeout_seconds=0.2,
            max_output_bytes=1024,
        )
    )
    assert result.timed_out
    assert time.monotonic() - started < 8


def test_process_runner_closes_output_streams_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStream:
        def __init__(self, chunk: bytes) -> None:
            self.chunk = chunk
            self.closed = False
            self.reads = 0

        def read(self, _size: int) -> bytes:
            self.reads += 1
            return self.chunk if self.reads == 1 else b""

        def close(self) -> None:
            self.closed = True

    class FakeProcess:
        pid = 12345
        returncode: int | None = None

        def __init__(self) -> None:
            self.stdout = FakeStream(b"stdout")
            self.stderr = FakeStream(b"stderr")
            self.wait_calls = 0

        def wait(self, timeout: float | None = None) -> None:
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise subprocess.TimeoutExpired("fake", timeout)
            self.returncode = -15

        def poll(self) -> int | None:
            return self.returncode

    process = FakeProcess()
    identity = tooling_process.ProcessIdentity(
        pid=process.pid,
        start_time=1.0,
        executable=Path(sys.executable),
        argv=(sys.executable, "-c", "pass"),
        cwd=REPOSITORY_ROOT,
    )
    monkeypatch.setattr(
        tooling_process.subprocess,
        "Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(
        tooling_process.ProcessIdentity,
        "capture",
        classmethod(lambda _cls, _pid, _fallback: identity),
    )
    monkeypatch.setattr(tooling_process, "_terminate_process", lambda *_args: None)

    result = tooling_process.StructuredProcessRunner().run(
        tooling_process.ProcessSpec(
            executable=sys.executable,
            argv=("-c", "pass"),
            cwd=REPOSITORY_ROOT,
            timeout_seconds=0.1,
        )
    )

    assert result.timed_out
    assert process.stdout.closed
    assert process.stderr.closed


def test_tcp_port_observation_falls_back_when_pid_listing_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = int(listener.getsockname()[1])

    def deny_connections(**_kwargs: object) -> object:
        raise psutil.AccessDenied(pid=None)

    monkeypatch.setattr(
        tooling_coordination.psutil, "net_connections", deny_connections
    )
    try:
        occupied = observe_tcp_port(port)
        assert occupied.listener_present is True
        assert occupied.pid_unknown
        assert occupied.pids == ()
    finally:
        listener.close()

    free = observe_tcp_port(port)
    assert free.listener_present is False
    assert free.pid_unknown
    assert not free.inspection_failed


def test_posix_adb_health_does_not_require_windows_dlls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adb = tmp_path / "bin" / "adb"
    adb.parent.mkdir()
    adb.write_bytes(b"adb")

    class FakeRunner:
        def run(self, spec: object) -> SimpleNamespace:
            assert getattr(spec, "executable") == adb
            return SimpleNamespace(
                ok=True,
                stdout="Android Debug Bridge version 1.0.41\nVersion 37.0.0",
                stderr="",
            )

    monkeypatch.setattr(tooling_adb, "os", SimpleNamespace(name="posix"))
    assert tooling_adb.is_healthy(adb, tmp_path, FakeRunner())


@pytest.mark.skipif(os.name != "nt", reason="требуется Windows venv redirector")
def test_windows_venv_runtime_keeps_exact_cwd_and_ownership() -> None:
    python = REPOSITORY_ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        pytest.skip("project venv отсутствует")
    running = StructuredProcessRunner().start(
        ProcessSpec(
            executable=python,
            argv=("-c", "import time; time.sleep(10)"),
            cwd=REPOSITORY_ROOT,
            timeout_seconds=20,
        )
    )
    try:
        assert running.identity.matches()
        assert running.identity.cwd == REPOSITORY_ROOT.resolve()
        assert running.identity.executable == running.identity.executable.resolve()
        assert running.identity.executable != python.resolve()
    finally:
        assert ProcessController.terminate(running.identity, timeout_seconds=10)


def test_scoped_path_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("текущая Windows policy не разрешает создать symlink")
    with pytest.raises(ToolingError):
        ScopedPath(tmp_path).resolve(link / "file.txt")


def test_file_lock_is_non_reentrant_across_instances(tmp_path: Path) -> None:
    first = FileLock(tmp_path / "state" / "operation.lock")
    second = FileLock(tmp_path / "state" / "operation.lock")
    assert first.acquire()
    assert first.acquire()
    try:
        assert not second.acquire()
    finally:
        first.release()
    assert second.acquire()
    second.release()


def test_file_lock_closes_stream_when_initialization_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lock_path = tmp_path / "state" / "operation.lock"
    streams = []
    original_open = Path.open

    def tracking_open(path: Path, *args: object, **kwargs: object):
        stream = original_open(path, *args, **kwargs)
        streams.append(stream)
        return stream

    original_stat = Path.stat

    def failing_stat(path: Path, *args: object, **kwargs: object):
        if path == lock_path:
            raise OSError("stat failed")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracking_open)
    monkeypatch.setattr(Path, "stat", failing_stat)

    with pytest.raises(OSError):
        FileLock(lock_path).acquire()

    assert len(streams) == 1
    assert streams[0].closed


def test_process_group_signal_never_targets_current_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int]] = []
    fake_os = SimpleNamespace(
        name="posix",
        getpgid=lambda _pid: 42,
        killpg=lambda group, signal_number: calls.append((group, signal_number)),
    )
    monkeypatch.setattr(tooling_process, "os", fake_os)

    assert not tooling_process._signal_process_group(42, signal.SIGTERM)
    assert calls == []
    assert tooling_process._signal_process_group(43, signal.SIGTERM)
    assert calls == [(43, signal.SIGTERM)]


def test_doctor_treats_missing_deploy_config_as_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tooling_doctor,
        "load_deploy_settings",
        lambda _root: DeploySettings(source_path=None),
    )

    result = DoctorService().run(REPOSITORY_ROOT)
    checks = {item.name: item for item in result.details.checks}

    assert result.ok
    assert checks["deploy_config"].status.value == "not_configured"


def test_default_project_python_keeps_venv_script_directory() -> None:
    expected_directory = REPOSITORY_ROOT / ".venv" / (
        "Scripts" if os.name == "nt" else "bin"
    )

    assert project_python(REPOSITORY_ROOT, DeploySettings(source_path=None)).parent == (
        expected_directory
    )


def test_cli_json_is_single_report_on_invocation_error() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = main(["--json", "unknown-command"], stdout=stdout, stderr=stderr)
    report = json.loads(stdout.getvalue())
    assert exit_code == 2
    assert report["code"] == ResultCode.TOOLING_INVALID_INVOCATION.value
    assert stderr.getvalue() == ""


def test_cli_help_is_available_without_service_side_effects() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    assert main(["--help"], stdout=stdout, stderr=stderr) == 0
    assert "doctor" in stdout.getvalue()
    assert stderr.getvalue() == ""


def test_doctor_reports_console_script_and_path_capabilities() -> None:
    result = DoctorService().run(REPOSITORY_ROOT)
    checks = {item.name: item for item in result.details.checks}
    assert checks["console_script"].status.value == "ready"
    assert "console_path" in checks
    if checks["console_path"].status.value != "ready":
        assert any(
            warning.code.value == "TOOLING_CLI_NOT_ON_PATH"
            for warning in result.warnings
        )


def test_console_path_inspection_requires_matching_command_and_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    python = tmp_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    script = python.parent / ("azur.exe" if os.name == "nt" else "azur")
    script.write_bytes(b"")
    monkeypatch.setenv("PATH", str(script.parent))
    monkeypatch.setattr(tooling_path, "which", lambda _name: str(script))

    status = tooling_path.inspect_console_path(python)

    assert status.installed is True
    assert status.current_shell is True
    assert status.status.value == "ready"


def test_console_path_inspection_fails_closed_for_wrong_path_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    python = tmp_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    script = python.parent / ("azur.exe" if os.name == "nt" else "azur")
    script.write_bytes(b"")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    wrong_script = unrelated / script.name
    wrong_script.write_bytes(b"")
    monkeypatch.setenv("PATH", str(script.parent))
    monkeypatch.setattr(tooling_path, "which", lambda _name: str(wrong_script))

    status = tooling_path.inspect_console_path(python)

    assert status.installed is True
    assert status.current_shell is False
    assert status.status.value != "ready"


def test_bootstrap_accepts_same_minor_external_uv_for_missing_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    external_uv = tmp_path / ("uv.exe" if os.name == "nt" else "uv")
    external_uv.write_bytes(b"uv")

    class ProbeRunner:
        def run(self, _spec: ProcessSpec) -> SimpleNamespace:
            return SimpleNamespace(ok=True, stdout="uv 0.12.1\n")

    monkeypatch.delenv("AZURPILOT_BOOTSTRAP_UV", raising=False)
    monkeypatch.setattr(
        tooling_bootstrap,
        "project_uv",
        lambda *_args: tmp_path / "missing-uv",
    )
    monkeypatch.setattr(tooling_bootstrap.shutil, "which", lambda _name: str(external_uv))

    resolved, source = tooling_bootstrap.BootstrapService(ProbeRunner()).resolve_uv(
        REPOSITORY_ROOT
    )

    assert resolved == external_uv.resolve()
    assert source == "PATH"


def test_state_layout_is_external(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(state_root))
    layout = StateLayout.for_repository(REPOSITORY_ROOT)
    assert layout.repository_directory.parent == state_root
    assert not layout.repository_directory.is_relative_to(REPOSITORY_ROOT)
