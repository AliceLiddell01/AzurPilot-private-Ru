"""Контрактные проверки Python tooling."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import dev_tools.postgresql_runtime as tooling_postgresql_runtime
from deploy import uv as deploy_uv

from azurpilot.cli import build_parser, main
from azurpilot.tooling import adb as tooling_adb
from azurpilot.tooling import bootstrap as tooling_bootstrap
from azurpilot.tooling import lifecycle as tooling_lifecycle
from azurpilot.tooling import update as tooling_update
from azurpilot.tooling.bootstrap import BuildService
from azurpilot.tooling.config import DeploySettings
from azurpilot.tooling.coordination import PortObservation
from azurpilot.tooling.contracts import (
    CapabilityCheck,
    CapabilityStatus,
    DoctorDetails,
    DoctorEvidence,
    PostgreSqlBackupEvidence,
    RepositoryRootEvidence,
    ResultCode,
    RootSource,
    ToolingResult,
)
from azurpilot.tooling.errors import ToolingError
from azurpilot.tooling.filesystem import JournalStore, StateLayout, path_identity
from azurpilot.tooling.git import canonical_remote_identity
from azurpilot.tooling.lifecycle import LifecycleService
from azurpilot.tooling.postgres import BackupOutcome, PostgreSqlBackupService
from azurpilot.tooling.process import ProcessIdentity, RunningProcess
from azurpilot.tooling.repair import RepairService
from azurpilot.tooling.repository import ResolvedRepository


def _repository_evidence() -> RepositoryRootEvidence:
    return RepositoryRootEvidence(
        source=RootSource.EXPLICIT,
        candidate_count=1,
        validation_checks=("test",),
        root_identity="a" * 24,
    )


def _layout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> StateLayout:
    root = tmp_path / "repository"
    root.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(state))
    return StateLayout.for_repository(root)


def test_cli_human_output_uses_russian_operator_presentation() -> None:
    class DoctorStub:
        def run(self, _root: object) -> ToolingResult[DoctorDetails, DoctorEvidence]:
            return ToolingResult[DoctorDetails, DoctorEvidence](
                ok=True,
                code=ResultCode.OK,
                state="ready",
                message="Проверка завершена.",
                details=DoctorDetails(
                    checks=(
                        CapabilityCheck(
                            name="repository",
                            status=CapabilityStatus.READY,
                            message="Корень репозитория подтверждён.",
                        ),
                    ),
                    healthy=True,
                ),
            )

    stdout = io.StringIO()
    stderr = io.StringIO()
    services = SimpleNamespace(doctor=DoctorStub())

    assert main(["doctor"], services=services, stdout=stdout, stderr=stderr) == 0
    assert "✓" in stdout.getvalue()
    assert "AzurPilot Doctor" in stdout.getvalue()
    assert "[OK]" not in stdout.getvalue()
    assert stderr.getvalue() == ""


def test_cli_json_suppresses_service_side_output() -> None:
    class DoctorStub:
        def run(self, _root: object) -> ToolingResult[DoctorDetails, DoctorEvidence]:
            print("служебный вывод не должен попасть в JSON")
            return ToolingResult[DoctorDetails, DoctorEvidence](
                ok=True,
                code=ResultCode.OK,
                state="ready",
                message="Проверка завершена.",
                details=DoctorDetails(checks=(), healthy=True),
            )

    stdout = io.StringIO()
    stderr = io.StringIO()
    services = SimpleNamespace(doctor=DoctorStub())

    assert main(["doctor", "--json"], services=services, stdout=stdout, stderr=stderr) == 0
    report = json.loads(stdout.getvalue())
    assert report["code"] == ResultCode.OK.value
    assert stdout.getvalue().count("\n") == 1
    assert stderr.getvalue() == ""


def test_lifecycle_keeps_state_when_termination_is_not_confirmed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "repository"
    (root / "config").mkdir(parents=True)
    (root / "config" / "deploy.yaml").write_text("Deploy:\n", encoding="utf-8")
    (root / "gui.py").write_text("", encoding="utf-8")
    python_name = "python.exe" if os.name == "nt" else "python"
    (root / ".venv" / ("Scripts" if os.name == "nt" else "bin")).mkdir(
        parents=True
    )
    (root / ".venv" / ("Scripts" if os.name == "nt" else "bin") / python_name).write_bytes(
        b"python"
    )

    class FakeLock:
        def acquire(self, timeout_seconds: float = 0.0) -> bool:
            return True

        def release(self) -> None:
            return None

    class FakeCoordinator:
        def __init__(self) -> None:
            self.record = None
            self.cleared = False

        def lock(self, _operation: str) -> FakeLock:
            return FakeLock()

        def read_lifecycle(self) -> None:
            return self.record

        def write_lifecycle(self, record: object) -> None:
            self.record = record

        def record_from_identity(
            self, _identity: ProcessIdentity, _root: Path, _port: int
        ) -> object:
            return object()

        def clear_lifecycle(self) -> None:
            self.cleared = True

        def clear_stop_request(self) -> None:
            return None

    coordinator = FakeCoordinator()
    resolver = SimpleNamespace(
        resolve=lambda _root=None: ResolvedRepository(root, _repository_evidence())
    )
    identity = ProcessIdentity(
        pid=12345,
        start_time=1.0,
        executable=Path(sys.executable).resolve(),
        argv=(str(sys.executable), "gui.py"),
        cwd=root.resolve(),
    )
    process = SimpleNamespace(pid=identity.pid, poll=lambda: None)
    running = RunningProcess(process=process, identity=identity)

    class FakeRunner:
        def start(self, _spec: object) -> RunningProcess:
            return running

    monkeypatch.setattr(
        tooling_lifecycle.RepositoryCoordinator,
        "for_root",
        lambda _root: coordinator,
    )
    monkeypatch.setattr(
        tooling_lifecycle,
        "observe_tcp_port",
        lambda _port: tooling_lifecycle.PortObservation(25548, ()),
    )
    monkeypatch.setattr(
        tooling_lifecycle.ProcessController,
        "terminate",
        lambda _identity, timeout_seconds=15.0: False,
    )
    service = LifecycleService(
        resolver=resolver,
        runner=FakeRunner(),
        require_infrastructure=False,
    )
    monkeypatch.setattr(
        service,
        "_wait_readiness",
        lambda *_args: (False, "no_http_response"),
    )
    monkeypatch.setattr(
        service,
        "_wait_cleanup",
        lambda *_args, **_kwargs: (
            True,
            tooling_lifecycle.PortObservation(25548, ()),
        ),
    )

    with pytest.raises(ToolingError) as error:
        service.start(root, timeout_seconds=10)

    assert error.value.code is ResultCode.TOOLING_CLEANUP_UNKNOWN
    assert coordinator.record is not None
    assert coordinator.cleared is False


def test_build_shortcut_defaults_are_explicitly_overridable() -> None:
    default_args = build_parser().parse_args(["build"])
    disabled_args = build_parser().parse_args(["build", "--no-shortcut"])

    assert not hasattr(default_args, "shortcut")
    assert disabled_args.shortcut is False


def test_canonical_remote_identity_rejects_unsafe_forms() -> None:
    expected = canonical_remote_identity(
        "https://github.com/AliceLiddell01/AzurPilot-private-Ru.git"
    )
    assert (
        canonical_remote_identity(
            "git@github.com:AliceLiddell01/AzurPilot-private-Ru"
        )
        == expected
    )
    for value in (
        "https://user:password@github.com/example/repository",
        "https://github.com/example/repository?token=secret",
        "git@github.com:example/repository#fragment",
        "relative/repository",
    ):
        with pytest.raises(ToolingError) as error:
            canonical_remote_identity(value)
        assert error.value.code is ResultCode.TOOLING_REMOTE_IDENTITY_UNVERIFIED


def test_journal_retention_is_bounded_and_state_directories_are_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    layout = _layout(monkeypatch, tmp_path)
    store = JournalStore(layout, "build")

    for index in range(40):
        transaction = store.create().model_copy(
            update={
                "transaction_id": f"build-{index:08d}",
                "phase": "completed",
            }
        )
        store.save(transaction)
        store.retain_terminal()

    assert layout.backups_directory.is_dir()
    assert layout.bootstrap_cache_directory.is_dir()
    assert store.active() is None
    assert len(store.terminal()) <= 32


def test_journal_rejects_directory_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    layout = _layout(monkeypatch, tmp_path)
    store = JournalStore(layout, "build")
    transaction = store.create()
    store.save(transaction)
    source = layout.transactions_directory / transaction.transaction_id / "journal.json"
    wrong = layout.transactions_directory / "build-wrong-directory"
    wrong.mkdir()
    shutil.copy2(source, wrong / "journal.json")

    with pytest.raises(ToolingError) as error:
        store.active()
    assert error.value.code is ResultCode.TOOLING_VERIFICATION_UNKNOWN


def test_postgres_backup_is_external_and_keeps_provenance_path_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    layout = _layout(monkeypatch, tmp_path)
    root = layout.repository_root
    backup_root = tmp_path / "external-backups"
    settings = DeploySettings(
        source_path=None,
        postgres_backup_root=str(backup_root),
    )

    def fake_backup(_root: Path, output: Path, **_options: object) -> Path:
        output.write_bytes(b"backup" * 512)
        return output

    monkeypatch.setattr(
        tooling_postgresql_runtime,
        "backup_for_repository",
        fake_backup,
    )
    outcome = PostgreSqlBackupService().create(
        root,
        layout,
        settings,
        pre_head="b" * 40,
        operation_id="update-test1234",
    )

    assert outcome.evidence.validated is True
    assert outcome.evidence.external is True
    provenance = json.loads(
        (outcome.path.parent / "provenance.json").read_text(encoding="utf-8")
    )
    assert "path" not in provenance
    assert provenance["sha256"] == outcome.evidence.sha256


def test_postgres_backup_failure_blocks_before_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    layout = _layout(monkeypatch, tmp_path)
    settings = DeploySettings(
        source_path=None,
        postgres_backup_root=str(tmp_path / "external-backups"),
    )

    def failing_backup(_root: Path, _output: Path, **_options: object) -> Path:
        raise RuntimeError("резервная копия недоступна")

    monkeypatch.setattr(
        tooling_postgresql_runtime,
        "backup_for_repository",
        failing_backup,
    )
    with pytest.raises(ToolingError) as error:
        PostgreSqlBackupService().create(
            layout.repository_root,
            layout,
            settings,
            pre_head="c" * 40,
            operation_id="update-test1234",
        )
    assert error.value.code is ResultCode.TOOLING_BACKUP_FAILED


def test_adb_archive_rejects_traversal_path(tmp_path: Path) -> None:
    archive = tmp_path / "platform-tools.zip"
    destination = tmp_path / "extracted"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("platform-tools/../../outside.txt", b"do-not-extract")

    with pytest.raises(ToolingError) as error:
        tooling_adb._extract_archive(archive, destination)
    assert error.value.code is ResultCode.TOOLING_ADB_FAILED
    assert not (tmp_path / "outside.txt").exists()


def test_update_environment_move_failure_is_reported_as_confirmed_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    venv = root / ".venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_bytes(b"old")
    candidate_environment = tmp_path / "candidate-environment"
    candidate_environment.mkdir()
    (candidate_environment / "python.exe").write_bytes(b"new")
    candidate = SimpleNamespace(
        environment=candidate_environment,
        previous=tmp_path / "previous",
        backup=tmp_path / "backup",
    )

    def fail_move(_source: str, _destination: str) -> None:
        raise OSError("смоделирован отказ перемещения")

    monkeypatch.setattr(tooling_update.shutil, "move", fail_move)
    with pytest.raises(ToolingError) as error:
        tooling_update.UpdateService()._replace_environment(
            root,
            candidate,
            "update-test1234",
        )
    assert error.value.code is ResultCode.TOOLING_APPLY_FAILED_ROLLED_BACK
    assert (venv / "Scripts" / "python.exe").read_bytes() == b"old"


def test_external_candidate_sync_targets_candidate_venv_layout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "repository"
    candidate_project = tmp_path / "candidate-project"
    candidate_environment = tmp_path / "candidate-environment"
    state_root = tmp_path / "transaction"
    root.mkdir()
    candidate_project.mkdir()
    (candidate_project / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    source_python = tmp_path / ("python.exe" if os.name == "nt" else "python")
    source_python.write_bytes(b"python")
    calls: list[list[str]] = []

    def fake_run(command: list[object], *_args: object) -> None:
        values = [str(item) for item in command]
        calls.append(values)
        if "venv" in values:
            directory = "Scripts" if os.name == "nt" else "bin"
            python_name = "python.exe" if os.name == "nt" else "python"
            target = candidate_environment / directory / python_name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"candidate python")

    monkeypatch.setattr(deploy_uv, "_deploy_bool", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(deploy_uv, "_resolve_uv", lambda *_args, **_kwargs: Path("uv"))
    monkeypatch.setattr(deploy_uv, "_uv_index_args", lambda *_args: [])
    monkeypatch.setattr(deploy_uv, "_run_and_collect", fake_run)

    deploy_uv.sync_project_venv(
        root=root,
        project_environment=candidate_environment,
        project_path=candidate_project,
        python_executable=source_python,
        state_root=state_root,
        install_project=False,
    )

    sync_command = calls[-1]
    directory = "Scripts" if os.name == "nt" else "bin"
    python_name = "python.exe" if os.name == "nt" else "python"
    assert str(candidate_environment / directory / python_name) in sync_command
    assert "--no-install-project" in sync_command


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_update_uses_real_git_fast_forward_and_blocks_on_backup_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git недоступен в тестовой среде")

    origin = tmp_path / "origin.git"
    upstream = tmp_path / "upstream.git"
    root = tmp_path / "client"
    producer = tmp_path / "producer"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "init", "--bare", str(upstream)], check=True, stdout=subprocess.DEVNULL)
    root.mkdir()
    _git(root, "init", "-b", "fixture")
    (root / "module").mkdir()
    (root / "deploy").mkdir()
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'azurpilot'\nversion = '0'\n", encoding="utf-8"
    )
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (root / "gui.py").write_text("", encoding="utf-8")
    (root / "config").mkdir()
    (root / "config" / "deploy.yaml").write_text(
        "Deploy:\n"
        "  Webui:\n"
        "    WebuiPort: 29999\n"
        "  Git:\n"
        "    Remote: origin\n"
        "    Branch: fixture\n"
        f"    Repository: '{origin.as_posix()}'\n"
        "    UpstreamRemote: upstream\n"
        "    UpstreamPushUrl: DISABLED\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "-c", "user.name=AzurPilot Test", "-c", "user.email=tooling@example.invalid", "commit", "-m", "initial")
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "remote", "add", "upstream", str(upstream))
    _git(root, "config", "remote.upstream.pushurl", "DISABLED")
    _git(root, "push", "origin", "fixture")
    _git(root, "branch", "--set-upstream-to", "origin/fixture")
    monkeypatch.setattr(
        tooling_update,
        "observe_tcp_port",
        lambda port: PortObservation(port=port, pids=()),
    )
    subprocess.run(
        ["git", "clone", "--branch", "fixture", str(origin), str(producer)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    _git(producer, "-c", "user.name=AzurPilot Test", "-c", "user.email=tooling@example.invalid", "config", "user.name", "AzurPilot Test")
    _git(producer, "config", "user.email", "tooling@example.invalid")

    def commit_remote(text: str) -> None:
        (producer / "README.md").write_text(text, encoding="utf-8")
        _git(producer, "add", "README.md")
        _git(producer, "commit", "-m", "fixture update")
        _git(producer, "push", "origin", "fixture")

    class SuccessfulBackup:
        def __init__(self) -> None:
            self.path = tmp_path / "backup.dump"
            self.path.write_bytes(b"backup" * 512)
            self.evidence = PostgreSqlBackupEvidence(
                backup_id="update-fixture-backup",
                validated=True,
                external=True,
                sha256="1" * 64,
                pre_head="a" * 40,
                provenance=path_identity(root),
            )

        def create(self, *_args: object, **_kwargs: object) -> BackupOutcome:
            return BackupOutcome(self.evidence, self.path)

    commit_remote("first\n")
    backup = SuccessfulBackup()
    result = tooling_update.UpdateService(backup_service=backup).update(
        root, timeout_seconds=60
    )
    assert result.ok
    assert result.details is not None and result.details.fast_forwarded
    first_head = result.evidence.post_head if result.evidence is not None else ""

    commit_remote("second\n")

    class FailingBackup:
        def create(self, *_args: object, **_kwargs: object) -> BackupOutcome:
            raise ToolingError(
                ResultCode.TOOLING_BACKUP_FAILED,
                "Резервная копия недоступна.",
            )

    with pytest.raises(ToolingError) as error:
        tooling_update.UpdateService(backup_service=FailingBackup()).update(
            root, timeout_seconds=60
        )
    assert error.value.code is ResultCode.TOOLING_BACKUP_FAILED
    assert tooling_update.GitClient(root).head() == first_head
    assert not tooling_update.GitClient(root).status_porcelain()


def test_build_failed_transaction_can_retry_after_confirmed_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "repository"
    (root / "config").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'test'\n", encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (root / "gui.py").write_text("", encoding="utf-8")
    (root / "config" / "deploy.template.yaml").write_text(
        "Deploy:\n", encoding="utf-8"
    )
    fake_uv = tmp_path / "uv.exe"
    fake_uv.write_bytes(b"uv")
    evidence = _repository_evidence()

    class FakeBootstrap:
        def resolve_uv(self, _root: Path) -> tuple[Path, str]:
            return fake_uv, "test"

        def sync(self, project_root: Path, _uv: Path, _timeout: float) -> str:
            directory = "Scripts" if os.name == "nt" else "bin"
            python_name = "python.exe" if os.name == "nt" else "python"
            uv_name = "uv.exe" if os.name == "nt" else "uv"
            python = project_root / ".venv" / directory / python_name
            uv = project_root / ".venv" / directory / uv_name
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_bytes(b"python")
            uv.write_bytes(b"uv")
            return ""

    class FakeRunner:
        def run(self, _spec: object) -> SimpleNamespace:
            return SimpleNamespace(
                ok=True,
                stdout="uv 0.12.13\n",
                stderr="",
                returncode=0,
                timed_out=False,
            )

    resolver = SimpleNamespace(
        resolve=lambda _root=None: ResolvedRepository(root, evidence)
    )
    service = BuildService(
        resolver=resolver,
        runner=FakeRunner(),
        bootstrap=FakeBootstrap(),
    )
    monkeypatch.setattr(service, "_assert_stopped", lambda _root: None)
    monkeypatch.setattr(
        service,
        "_copy_template",
        lambda _root: (_ for _ in ()).throw(RuntimeError("template failure")),
    )
    if os.name == "nt":
        monkeypatch.setattr(
            tooling_bootstrap,
            "resolve_adb",
            lambda *_args: tooling_adb.AdbResolution(
                CapabilityStatus.UNSUPPORTED,
                None,
                None,
                "test",
            ),
        )

    with pytest.raises(ToolingError) as first_error:
        service.build(root, create_shortcut=False)
    assert first_error.value.code is ResultCode.TOOLING_APPLY_FAILED_ROLLED_BACK
    assert not (root / ".venv").exists()
    assert JournalStore(StateLayout.for_repository(root), "build").active() is None

    monkeypatch.setattr(service, "_copy_template", lambda _root: False)
    result = service.build(root, create_shortcut=False)
    assert result.ok
    directory = "Scripts" if os.name == "nt" else "bin"
    python_name = "python.exe" if os.name == "nt" else "python"
    assert (root / ".venv" / directory / python_name).is_file()


@pytest.mark.skipif(os.name == "nt", reason="требуется POSIX symlink в venv")
def test_repair_and_update_allow_only_regular_file_symlink_in_posix_venv(
    tmp_path: Path,
) -> None:
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(sys.executable)

    assert tooling_update.UpdateService._tree_safe(venv)
    assert RepairService._tree_safe(venv)


def test_backup_contract_model_does_not_accept_plain_path() -> None:
    evidence = PostgreSqlBackupEvidence(
        backup_id="update-test1234",
        validated=True,
        external=True,
        sha256="d" * 64,
        pre_head="e" * 40,
        provenance="f" * 24,
    )
    assert "path" not in evidence.model_dump()
