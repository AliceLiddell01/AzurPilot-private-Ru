"""Контракты первого Python tooling vertical slice."""

from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import azurpilot.tooling.path as tooling_path
from azurpilot.cli import main
from azurpilot.tooling import bootstrap as tooling_bootstrap
from azurpilot.tooling.contracts import (
    DoctorDetails,
    RepositoryRootEvidence,
    ResultCode,
    RootSource,
    ToolingResult,
)
from azurpilot.tooling.coordination import FileLock
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
