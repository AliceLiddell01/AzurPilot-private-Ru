"""Контракты первого Python tooling vertical slice."""

from __future__ import annotations

import errno
import io
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest
from pydantic import ValidationError

import azurpilot.tooling.application_state as tooling_application_state
import azurpilot.tooling.coordination as tooling_coordination
import azurpilot.tooling.mcp as tooling_mcp
import azurpilot.tooling.path as tooling_path
import azurpilot.tooling.process_core as tooling_process
from azurpilot.cli import CliInvocationError, build_parser, main
from azurpilot.integrations.contracts import (
    IntegrationDetails,
    IntegrationEvidence,
    IntegrationEvidenceBundle,
    IntegrationName,
    IntegrationRecord,
    IntegrationState,
)
from azurpilot.tooling import adb as tooling_adb
from azurpilot.tooling import bootstrap as tooling_bootstrap
from azurpilot.tooling import doctor as tooling_doctor
from azurpilot.tooling.config import DeploySettings, project_python
from azurpilot.tooling.contracts import (
    CapabilityStatus,
    DoctorDetails,
    ExitCode,
    McpAcceptanceDetails,
    OperationState,
    RepositoryRootEvidence,
    ResultCode,
    RootSource,
    ToolingResult,
    exit_code_for,
)
from azurpilot.tooling.coordination import FileLock, observe_tcp_port
from azurpilot.tooling.doctor import DoctorService
from azurpilot.tooling.errors import RepositoryResolutionError, ToolingError
from azurpilot.tooling.filesystem import ScopedPath, StateLayout
from azurpilot.tooling.git import GitClient
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


@pytest.mark.parametrize(
    ("code", "expected"),
    (
        (ResultCode.MCP_SOURCE_BUNDLE_INVALID, ExitCode.PRECONDITION),
        (ResultCode.MCP_SOURCE_BUNDLE_DRIFT, ExitCode.PRECONDITION),
        (ResultCode.MCP_VERSION_BUMP_REQUIRED, ExitCode.PRECONDITION),
        (ResultCode.MCP_ENVIRONMENT_STALE, ExitCode.PRECONDITION),
    ),
)
def test_mcp_result_codes_map_to_stable_exit_categories(
    code: ResultCode, expected: ExitCode
) -> None:
    assert exit_code_for(code) is expected


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


def test_process_environment_policy_requires_explicit_test_opt_in() -> None:
    test_key = "TEST_LOCAL_MCP_CRASH_AFTER_READY"
    with pytest.raises(ValueError):
        safe_environment({test_key: "azurpilot-dev"})

    spec = ProcessSpec(
        executable=sys.executable,
        cwd=REPOSITORY_ROOT,
        env={test_key: "azurpilot-dev"},
        allow_test_environment=True,
    )
    assert spec.launch_environment[test_key] == "azurpilot-dev"


def test_process_environment_keeps_credential_file_reference_typed() -> None:
    environment = safe_environment(
        {"GRAFANA_SERVICE_ACCOUNT_TOKEN_FILE": "C:/private/grafana-token"}
    )

    assert environment["GRAFANA_SERVICE_ACCOUNT_TOKEN_FILE"] == (
        "C:/private/grafana-token"
    )


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


def test_process_controller_does_not_treat_access_denied_as_terminated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = tooling_process.ProcessIdentity(
        pid=12345,
        start_time=1.0,
        executable=Path(sys.executable),
        argv=(sys.executable, "-c", "pass"),
        cwd=REPOSITORY_ROOT,
    )
    matches = iter((True, True))
    monkeypatch.setattr(
        tooling_process.ProcessIdentity,
        "matches",
        lambda _identity: next(matches, True),
    )

    def deny_process(_pid: int) -> object:
        raise psutil.AccessDenied(pid=12345)

    monkeypatch.setattr(tooling_process.psutil, "Process", deny_process)

    assert not ProcessController.terminate(identity, timeout_seconds=0.1)


def test_force_termination_signal_is_platform_safe() -> None:
    expected = signal.SIGTERM if os.name == "nt" else signal.SIGKILL
    assert tooling_process._FORCE_TERMINATION_SIGNAL == expected


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


def test_tcp_port_observation_falls_back_for_psutil_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = tooling_coordination.PortObservation(
        port=29998,
        pids=(),
        listener_present=False,
        pid_unknown=True,
    )

    def fail_connections(**_kwargs: object) -> object:
        raise psutil.NoSuchProcess(pid=29998)

    monkeypatch.setattr(
        tooling_coordination.psutil, "net_connections", fail_connections
    )
    monkeypatch.setattr(
        tooling_coordination,
        "_probe_tcp_port_without_pid",
        lambda _port: expected,
    )

    assert observe_tcp_port(29998) == expected


def test_tcp_port_observation_skips_unavailable_ipv6_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSocket:
        def __init__(self, family: int) -> None:
            self.family = family

        def __enter__(self) -> "FakeSocket":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def bind(self, _address: tuple[str, int]) -> None:
            if self.family == socket.AF_INET6:
                raise OSError(errno.EAFNOSUPPORT, "IPv6 недоступен")

    monkeypatch.setattr(
        tooling_coordination.psutil,
        "net_connections",
        lambda **_kwargs: (_ for _ in ()).throw(psutil.AccessDenied(pid=None)),
    )
    monkeypatch.setattr(tooling_coordination.socket, "has_ipv6", True)
    monkeypatch.setattr(
        tooling_coordination.socket,
        "socket",
        lambda family, _socket_type: FakeSocket(family),
    )

    observation = observe_tcp_port(29998)

    assert observation.listener_present is False
    assert observation.pid_unknown
    assert not observation.inspection_failed


def test_posix_adb_health_does_not_require_windows_dlls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adb = tmp_path / "bin" / "adb"
    adb.parent.mkdir()
    adb.write_bytes(b"adb")

    class FakeRunner:
        def run(self, spec: ProcessSpec) -> SimpleNamespace:
            assert spec.executable == adb
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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    evidence = RepositoryRootEvidence(
        source=RootSource.EXPLICIT,
        candidate_count=1,
        validation_checks=("test",),
        root_identity="1" * 16,
    )
    resolver = SimpleNamespace(
        resolve=lambda _root: ResolvedRepository(root, evidence)
    )
    monkeypatch.setattr(
        tooling_doctor,
        "load_deploy_settings",
        lambda _root, **_kwargs: DeploySettings(source_path=None),
    )
    monkeypatch.setattr(
        DoctorService,
        "_git_check",
        lambda _self, _root, _settings: (
            CapabilityStatus.NOT_CONFIGURED,
            "Каноническая идентичность репозитория не настроена.",
        ),
    )
    monkeypatch.setattr(
        DoctorService,
        "_runtime_check",
        lambda _self, _root: (CapabilityStatus.READY, "Среда остановлена."),
    )
    monkeypatch.setattr(
        tooling_doctor,
        "project_python",
        lambda *_args: Path(sys.executable),
    )
    monkeypatch.setattr(
        tooling_doctor,
        "project_uv",
        lambda *_args: Path(sys.executable),
    )
    monkeypatch.setattr(
        tooling_doctor,
        "project_adb",
        lambda *_args: tmp_path / "missing-adb",
    )
    monkeypatch.setattr(
        tooling_doctor,
        "inspect_console_path",
        lambda _python: SimpleNamespace(
            installed=False,
            status=CapabilityStatus.NOT_CONFIGURED,
            message="Консольная команда не настроена.",
        ),
    )
    monkeypatch.setattr(
        tooling_doctor.shutil,
        "which",
        lambda name: str(Path(sys.executable)) if name == "uv" else None,
    )

    result = DoctorService(
        resolver=resolver, runner=SimpleNamespace()
    ).run(root)
    checks = {item.name: item for item in result.details.checks}

    assert not result.ok
    assert result.state is OperationState.NOT_CONFIGURED
    assert result.code is ResultCode.TOOLING_PRECONDITION_FAILED
    assert checks["deploy_config"].status.value == "not_configured"
    assert checks["git"].status is CapabilityStatus.NOT_CONFIGURED


def test_default_project_python_keeps_venv_script_directory() -> None:
    expected_directory = REPOSITORY_ROOT / ".venv" / (
        "Scripts" if os.name == "nt" else "bin"
    )

    assert project_python(REPOSITORY_ROOT, DeploySettings(source_path=None)).parent == (
        expected_directory
    )


@pytest.mark.skipif(os.name == "nt", reason="контракт POSIX venv symlink")
def test_configured_project_python_keeps_logical_venv_path(tmp_path: Path) -> None:
    """Регрессия: configured venv path не канонизируется до process layer."""

    root = tmp_path.resolve() / "repository"
    script_directory = root / ".venv" / "bin"
    script_directory.mkdir(parents=True)
    python = script_directory / "python"
    python.symlink_to(Path(sys.executable).resolve())
    settings = DeploySettings(source_path=None, python_executable="./.venv/bin/python")

    resolved = project_python(root, settings)

    assert resolved == python
    assert resolved.parent == script_directory
    assert resolved != python.resolve()


def test_cli_json_is_single_report_on_invocation_error() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = main(["--json", "unknown-command"], stdout=stdout, stderr=stderr)
    report = json.loads(stdout.getvalue())
    assert exit_code == 2
    assert report["code"] == ResultCode.TOOLING_INVALID_INVOCATION.value
    assert stderr.getvalue() == ""


def test_cli_reconcile_rejects_bump_without_source() -> None:
    class UnexpectedMcpCall:
        def reconcile(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("MCP reconcile не должен вызываться")

    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = main(
        ["--json", "mcp", "reconcile", "--bump", "patch"],
        services=SimpleNamespace(mcp=UnexpectedMcpCall()),
        stdout=stdout,
        stderr=stderr,
    )

    report = json.loads(stdout.getvalue())
    assert exit_code == 2
    assert report["code"] == ResultCode.TOOLING_INVALID_INVOCATION.value
    assert stderr.getvalue() == ""


def test_cli_exposes_mcp_sync_and_retains_diagnostic_reconcile_routes() -> None:
    parser = build_parser()

    runtime = parser.parse_args(["mcp", "reconcile"])
    assert runtime.mcp_command == "reconcile"
    assert runtime.source is False
    assert runtime.bump is None

    source = parser.parse_args(
        ["mcp", "reconcile", "--source", "--bump", "auto"]
    )
    assert source.mcp_command == "reconcile"
    assert source.source is True
    assert source.bump == "auto"

    accept = parser.parse_args(["mcp", "accept"])
    assert accept.mcp_command == "accept"

    sync = parser.parse_args(["mcp", "sync", "--base", "a" * 40, "--json"])
    assert sync.mcp_command == "sync"
    assert sync.base == "a" * 40
    assert sync.json is True

    state = parser.parse_args(
        ["app", "state", "commission/recovery", "--profile", "ap"]
    )
    assert state.app_command == "state"
    assert state.state_id == "commission/recovery"
    assert state.profile == "ap"

    with pytest.raises(CliInvocationError):
        parser.parse_args(["mcp", "reconcile", "--runtime"])


def test_cli_routes_mcp_sync_to_canonical_service() -> None:
    class McpStub:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def sync(self, root: str, *, base_commit: str) -> ToolingResult:
            self.calls.append((root, base_commit))
            return ToolingResult(
                ok=True,
                code=ResultCode.OK,
                state=OperationState.READY,
                message="MCP sync завершён.",
            )

    mcp = McpStub()
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = main(
        [
            "--repository-root",
            str(REPOSITORY_ROOT),
            "--json",
            "mcp",
            "sync",
            "--base",
            "a" * 40,
        ],
        services=SimpleNamespace(mcp=mcp),
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert mcp.calls == [(str(REPOSITORY_ROOT), "a" * 40)]
    assert json.loads(stdout.getvalue())["code"] == ResultCode.OK.value
    assert stderr.getvalue() == ""


def test_cli_routes_mcp_accept_to_canonical_service() -> None:
    class McpStub:
        def __init__(self) -> None:
            self.calls: list[Path] = []

        def accept(self, root: Path) -> ToolingResult[McpAcceptanceDetails, object]:
            self.calls.append(root)
            return ToolingResult(
                ok=True,
                code=ResultCode.OK,
                state=OperationState.READY,
        message="Проверка нового клиента MCP подтверждена.",
                details=McpAcceptanceDetails(
                    acceptance_state="READY",
                    reason_code="MCP_FRESH_CLIENT_READY",
                    initialized=True,
                    tool_count=2,
                    tool_catalog_sha256="a" * 64,
                    capability_catalog_sha256="b" * 64,
                    contract_revision="c" * 64,
                    called_tools=("dev_get_contract", "dev_list_smoke_capabilities"),
                ),
            )

    mcp = McpStub()
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = main(
        [
            "--json",
            "mcp",
            "accept",
            "--repository-root",
            str(REPOSITORY_ROOT),
        ],
        services=SimpleNamespace(mcp=mcp),
        stdout=stdout,
        stderr=stderr,
    )

    report = json.loads(stdout.getvalue())
    assert exit_code == 0
    assert report["details"]["acceptance_state"] == "READY"
    assert mcp.calls == [str(REPOSITORY_ROOT)]
    assert stderr.getvalue() == ""


def test_mcp_accept_uses_repository_owned_fresh_client(monkeypatch: pytest.MonkeyPatch) -> None:
    from dev_tools import mcp_acceptance

    calls: list[tuple[Path, bool]] = []

    async def accept(root: Path, *, allow_dirty: bool = False) -> object:
        calls.append((root, allow_dirty))
        return SimpleNamespace(
            state=IntegrationState.READY,
            reason_code="MCP_FRESH_CLIENT_READY",
            initialized=True,
            protocol_version="2025-03-26",
            server_name="azurpilot-dev",
            server_version="1.2.3",
            source_revision="a" * 40,
            tool_count=2,
            tool_catalog_sha256="b" * 64,
            capability_catalog_sha256="c" * 64,
            contract_revision="d" * 64,
            called_tools=("dev_get_contract", "dev_list_smoke_capabilities"),
            diagnostics=(),
        )

    monkeypatch.setattr(mcp_acceptance, "accept", accept)
    service = tooling_mcp.McpService()
    result = service.accept(REPOSITORY_ROOT)
    dirty_result = service.accept(REPOSITORY_ROOT, allow_dirty=True)

    assert result.ok is True
    assert dirty_result.ok is True
    assert result.details.acceptance_state == "READY"
    assert result.details.called_tools == (
        "dev_get_contract",
        "dev_list_smoke_capabilities",
    )
    assert calls == [(REPOSITORY_ROOT, False), (REPOSITORY_ROOT, True)]


def test_application_state_query_reads_store_without_webui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from module.application import commission_recovery

    calls: list[object] = []

    class Store:
        @classmethod
        def from_environment(cls):
            calls.append("open")
            return cls()

        def read(self, profile: str) -> object:
            calls.append(("read", profile))
            return SimpleNamespace(
                profile=profile,
                status="confirmed",
                remaining=4,
                used=1,
                next_oil_cost=110,
                next_ap_gain=10,
                confirmed_at=datetime(2026, 9, 23, tzinfo=UTC),
                reset_at=datetime(2026, 9, 28, tzinfo=UTC),
                source="game_ocr",
                last_result=None,
                cache_status="READY",
                error=None,
            )

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(commission_recovery, "CommissionRecoveryStore", Store)

    result = tooling_application_state.ApplicationStateService().read(
        "commission/recovery", "ap"
    )

    assert result.ok is True
    assert result.details.value.remaining == 4
    assert calls == ["open", ("read", "ap"), "close"]


def test_cli_unexpected_error_exposes_only_bounded_exception_type() -> None:
    class ExplodingDoctor:
        def run(self, _root: object) -> object:
            raise RuntimeError("секретное диагностическое содержимое")

    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = main(
        ["doctor", "--json"],
        services=SimpleNamespace(doctor=ExplodingDoctor()),
        stdout=stdout,
        stderr=stderr,
    )

    report = json.loads(stdout.getvalue())
    assert exit_code == 30
    assert "RuntimeError" in report["message"]
    assert "секретное диагностическое содержимое" not in report["message"]
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
    assert checks["external_integrations"].status is CapabilityStatus.NOT_CHECKED
    assert "doctor --full" in checks["external_integrations"].message
    assert all(len(check.message) <= 240 for check in checks.values())
    git = GitClient(REPOSITORY_ROOT)
    assert git.executable is not None
    try:
        git.upstream()
    except ToolingError:
        assert checks["git"].status is CapabilityStatus.UNAVAILABLE
    else:
        assert checks["git"].status is CapabilityStatus.READY
    assert checks["console_script"].status.value == "ready"
    assert "console_path" in checks
    if checks["console_path"].status.value != "ready":
        assert any(
            warning.code.value == "TOOLING_CLI_NOT_ON_PATH"
            for warning in result.warnings
        )
    if checks["runtime"].status is not CapabilityStatus.READY:
        pytest.skip(
            "Полный Doctor требует свободного и подтверждённого состояния WebUI."
        )
    assert result.ok


def test_doctor_full_uses_closed_external_integration_record_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    evidence = RepositoryRootEvidence(
        source=RootSource.EXPLICIT,
        candidate_count=1,
        validation_checks=("test",),
        root_identity="2" * 16,
    )
    resolver = SimpleNamespace(
        resolve=lambda _root: ResolvedRepository(root, evidence)
    )

    class IntegrationStub:
        def status(self, _root: Path) -> ToolingResult:
            return ToolingResult(
                ok=True,
                code=ResultCode.OK,
                state=OperationState.READY,
                message="Проверка внешних интеграций пройдена.",
                details=IntegrationDetails(
                    action="status",
                    integrations=(
                        IntegrationRecord(
                            name=IntegrationName.CODERABBIT,
                            state=IntegrationState.READY,
                            reason_code="CODERABBIT_NATIVE_READY",
                            message="Исполняемый файл CodeRabbit и его версия подтверждены.",
                            evidence=IntegrationEvidence(
                                route="direct_native_agent",
                                configured=True,
                                reachable=True,
                                read_only=True,
                            ),
                        ),
                    ),
                ),
                evidence=IntegrationEvidenceBundle(
                    generated_at="2026-09-22T00:00:00Z"
                ),
            )

    monkeypatch.setattr(
        tooling_doctor,
        "load_deploy_settings",
        lambda _root, **_kwargs: DeploySettings(source_path=None),
    )
    monkeypatch.setattr(
        DoctorService,
        "_git_check",
        lambda _self, _root, _settings: (
            CapabilityStatus.READY,
            "Git подтверждён.",
        ),
    )
    monkeypatch.setattr(
        DoctorService,
        "_runtime_check",
        lambda _self, _root: (CapabilityStatus.READY, "Среда остановлена."),
    )
    monkeypatch.setattr(tooling_doctor, "project_python", lambda *_args: Path(sys.executable))
    monkeypatch.setattr(tooling_doctor, "project_uv", lambda *_args: Path(sys.executable))
    monkeypatch.setattr(tooling_doctor, "project_adb", lambda *_args: tmp_path / "missing-adb")
    monkeypatch.setattr(
        tooling_doctor,
        "inspect_console_path",
        lambda _python: SimpleNamespace(
            installed=False,
            status=CapabilityStatus.NOT_CONFIGURED,
            message="Консольная команда не настроена.",
        ),
    )
    monkeypatch.setattr(tooling_doctor.shutil, "which", lambda _name: None)

    result = DoctorService(
        resolver=resolver,
        runner=SimpleNamespace(),
        integrations=IntegrationStub(),
    ).run(root, include_external_integrations=True)

    assert result.ok is True
    assert len(result.details.external_integrations) == 1
    summary = result.details.external_integrations[0]
    assert summary.name == "coderabbit"
    assert summary.status == "READY"
    assert summary.reason_code == "CODERABBIT_NATIVE_READY"
    assert summary.route == "direct_native_agent"


def test_doctor_fails_closed_for_mismatched_canonical_git_remote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    settings = DeploySettings(
        source_path=root / "config" / "deploy.yaml",
        repository_url="git@github.com:AliceLiddell01/AzurPilot-private-Ru.git",
    )
    evidence = RepositoryRootEvidence(
        source=RootSource.EXPLICIT,
        candidate_count=1,
        validation_checks=("test",),
        root_identity="1" * 16,
    )

    class FakeResolver:
        def resolve(self, _root: Path) -> ResolvedRepository:
            return ResolvedRepository(root, evidence)

    class FakeGit:
        def __init__(self, _root: Path, _runner: object) -> None:
            pass

        def branch(self) -> str:
            return "personal/stable"

        def head(self) -> str:
            return "a" * 40

        def upstream(self) -> str:
            return "origin/personal/stable"

        def active_operation(self) -> bool:
            return False

        def remote_url(self, _remote: str) -> str:
            return "https://github.com/example/not-azurpilot.git"

        def status_porcelain(self) -> str:
            return ""

    monkeypatch.setattr(
        tooling_doctor,
        "load_deploy_settings",
        lambda _root, **_kwargs: settings,
    )
    monkeypatch.setattr(tooling_doctor, "GitClient", FakeGit)
    monkeypatch.setattr(
        tooling_doctor,
        "project_python",
        lambda *_args: Path(sys.executable),
    )
    monkeypatch.setattr(
        tooling_doctor,
        "project_uv",
        lambda *_args: Path(sys.executable),
    )
    monkeypatch.setattr(
        tooling_doctor,
        "project_adb",
        lambda *_args: tmp_path / "missing-adb",
    )
    monkeypatch.setattr(
        tooling_doctor,
        "inspect_console_path",
        lambda _python: SimpleNamespace(
            installed=False,
            current_shell=False,
            status=CapabilityStatus.NOT_CONFIGURED,
            message="Консольная команда не настроена.",
        ),
    )
    monkeypatch.setattr(
        tooling_doctor.shutil,
        "which",
        lambda name: str(Path(sys.executable)) if name == "uv" else None,
    )
    monkeypatch.setattr(
        DoctorService,
        "_runtime_check",
        lambda _self, _root: (CapabilityStatus.READY, "Среда остановлена."),
    )

    result = DoctorService(
        resolver=FakeResolver(), runner=SimpleNamespace()
    ).run(root)
    checks = {item.name: item for item in result.details.checks}

    assert not result.ok
    assert result.code is ResultCode.TOOLING_PRECONDITION_FAILED
    assert result.state is OperationState.FAILED
    assert checks["git"].status is CapabilityStatus.FAILED
    assert "не совпадает" in checks["git"].message


def test_doctor_marks_unconfirmed_running_runtime_as_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class LifecycleStub:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def inspect(self, _root: Path) -> SimpleNamespace:
            return SimpleNamespace(
                ok=True,
                state=OperationState.RUNNING,
                message="WebUI считается работающим без подтверждения готовности.",
            )

    monkeypatch.setattr(tooling_doctor, "LifecycleService", LifecycleStub)

    status, message = DoctorService()._runtime_check(tmp_path)

    assert status is CapabilityStatus.UNKNOWN
    assert "без подтверждения" in message


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
