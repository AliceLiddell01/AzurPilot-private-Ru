"""Read-only diagnosis и transactional Repair project environment."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from secrets import token_hex

from .bootstrap import BootstrapService
from .config import load_deploy_settings, project_python, project_uv
from .contracts import (
    OperationState,
    RepairDetails,
    RepairEvidence,
    ResultCode,
    ToolingResult,
)
from .coordination import RepositoryCoordinator, observe_tcp_port
from .errors import ToolingError
from .filesystem import (
    JournalStore,
    ScopedPath,
    bounded_read_text,
    sha256_file,
)
from .process import ProcessSpec, StructuredProcessRunner
from .repository import RepositoryResolver


@dataclass(frozen=True)
class _Diagnosis:
    issues: tuple[str, ...]
    python_path: Path | None
    uv_path: Path | None
    lock_sha256: str | None

    @property
    def healthy(self) -> bool:
        return not self.issues


class RepairService:
    """Repair не меняет Git, config или PostgreSQL и не обходит active Update."""

    def __init__(
        self,
        resolver: RepositoryResolver | None = None,
        runner: StructuredProcessRunner | None = None,
        bootstrap: BootstrapService | None = None,
    ) -> None:
        self.resolver = resolver or RepositoryResolver()
        self.runner = runner or StructuredProcessRunner()
        self.bootstrap = bootstrap or BootstrapService(self.runner)

    def _diagnose(self, root: Path) -> _Diagnosis:
        issues: list[str] = []
        lock_path = root / "uv.lock"
        lock_hash = sha256_file(lock_path) if lock_path.is_file() else None
        if lock_hash is None:
            issues.append("uv.lock отсутствует")
        settings = load_deploy_settings(root)
        python = project_python(root, settings)
        uv = project_uv(root, settings)
        if not python.is_file():
            issues.append("project Python отсутствует")
        else:
            try:
                result = self.runner.run(
                    ProcessSpec(
                        executable=python,
                        argv=("-c", "import sys; raise SystemExit(0)"),
                        cwd=root,
                        timeout_seconds=30,
                        max_output_bytes=16 * 1024,
                    )
                )
                if not result.ok:
                    issues.append("project Python не запускается")
            except OSError, ValueError, ToolingError:
                issues.append("project Python не запускается")
        if not uv.is_file():
            issues.append("project uv отсутствует")
        else:
            try:
                result = self.runner.run(
                    ProcessSpec(
                        executable=uv,
                        argv=("--version",),
                        cwd=root,
                        timeout_seconds=10,
                        max_output_bytes=8 * 1024,
                    )
                )
                if not result.ok:
                    issues.append("project uv не запускается")
            except OSError, ValueError, ToolingError:
                issues.append("project uv не запускается")
        return _Diagnosis(
            tuple(issues[:16]),
            python if python.is_file() else None,
            uv if uv.is_file() else None,
            lock_hash,
        )

    def _assert_stopped(self, root: Path) -> None:
        coordinator = RepositoryCoordinator.for_root(root)
        record = coordinator.read_lifecycle()
        if record is not None and coordinator.identity_from_record(record).matches():
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Repair требует остановленного WebUI.",
            )
        settings = load_deploy_settings(root)
        port = observe_tcp_port(settings.webui_port)
        if port.inspection_failed:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Нельзя подтвердить свободный WebUI port.",
            )
        if port.pids:
            raise ToolingError(
                ResultCode.TOOLING_PORT_CONFLICT,
                "Repair не выполняется при занятом WebUI port.",
            )

    @staticmethod
    def _mark_owned_venv(venv: Path, transaction_id: str) -> Path:
        venv.mkdir(parents=True, exist_ok=True)
        return ScopedPath(venv).atomic_write_text(
            ".azurpilot-repair-owned",
            f"schema_version=1\ntransaction_id={transaction_id}\n",
        )

    @staticmethod
    def _is_owned_venv(venv: Path, transaction_id: str) -> bool:
        marker = venv / ".azurpilot-repair-owned"
        if not marker.is_file() or marker.is_symlink():
            return False
        try:
            return f"transaction_id={transaction_id}" in bounded_read_text(
                marker, max_bytes=4096
            )
        except ToolingError:
            return False

    def repair(
        self,
        repository_root: str | Path | None = None,
        *,
        diagnostic_only: bool = False,
        timeout_seconds: float = 30 * 60,
    ) -> ToolingResult[RepairDetails, RepairEvidence]:
        resolved = self.resolver.resolve(repository_root)
        root = resolved.path
        operation_id = f"repair-{token_hex(8)}"
        coordinator = RepositoryCoordinator.for_root(root)
        lock = coordinator.lock("repair")
        if not lock.acquire(timeout_seconds=0):
            raise ToolingError(
                ResultCode.TOOLING_OPERATION_CONFLICT,
                "Repair уже выполняется.",
                operation_id=operation_id,
            )
        transaction = None
        backup_path: Path | None = None
        venv = root / ".venv"
        try:
            self._assert_stopped(root)
            update_journal = JournalStore(coordinator.layout, "update").active()
            repair_journal = JournalStore(coordinator.layout, "repair").active()
            build_journal = JournalStore(coordinator.layout, "build").active()
            if (
                update_journal is not None
                or repair_journal is not None
                or build_journal is not None
            ):
                raise ToolingError(
                    ResultCode.TOOLING_OPERATION_CONFLICT,
                    "Незавершённая transaction блокирует Repair.",
                    operation_id=operation_id,
                )
            diagnosis = self._diagnose(root)
            if diagnostic_only or diagnosis.healthy:
                return ToolingResult[RepairDetails, RepairEvidence](
                    ok=diagnosis.healthy,
                    code=ResultCode.OK
                    if diagnosis.healthy
                    else ResultCode.TOOLING_REPAIR_REQUIRED,
                    state=OperationState.READY
                    if diagnosis.healthy
                    else OperationState.DIAGNOSTIC,
                    message="Диагностика Repair не обнаружила проблем."
                    if diagnosis.healthy
                    else "Диагностика выявила проблемы; запись не изменена.",
                    operation_id=operation_id,
                    details=RepairDetails(
                        status=OperationState.READY
                        if diagnosis.healthy
                        else OperationState.DIAGNOSTIC,
                        diagnostic_only=diagnostic_only,
                        issues=diagnosis.issues,
                        repaired=False,
                    ),
                    evidence=RepairEvidence(
                        repository=resolved.evidence,
                        lock_sha256=diagnosis.lock_sha256,
                        environment_checked=True,
                    ),
                )

            journal_store = JournalStore(coordinator.layout, "repair")
            transaction = journal_store.create()
            journal_store.save(transaction)
            if venv.exists():
                if venv.is_symlink() or not venv.is_dir():
                    raise ToolingError(
                        ResultCode.TOOLING_PRECONDITION_FAILED,
                        "Повреждённая .venv имеет небезопасный тип.",
                        operation_id=operation_id,
                    )
                transaction_dir = (
                    coordinator.layout.transactions_directory
                    / transaction.transaction_id
                )
                backup_path = transaction_dir / "venv-backup"
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(venv), str(backup_path))
                transaction = transaction.model_copy(
                    update={"phase": "backup_ready", "backup_present": True}
                )
                journal_store.save(transaction)
            self._mark_owned_venv(venv, transaction.transaction_id)
            transaction = transaction.model_copy(update={"phase": "rebuild_started"})
            journal_store.save(transaction)
            uv, _ = self.bootstrap.resolve_uv(root)
            self.bootstrap.sync(root, uv, timeout_seconds)
            final_diagnosis = self._diagnose(root)
            if not final_diagnosis.healthy:
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "После Repair environment не прошёл проверку.",
                    operation_id=operation_id,
                )
            transaction = transaction.model_copy(update={"phase": "completed"})
            journal_store.save(transaction)
            return ToolingResult[RepairDetails, RepairEvidence](
                ok=True,
                code=ResultCode.OK,
                state=OperationState.READY,
                message="Project environment восстановлен и проверен; исходная `.venv` сохранена во внешнем backup.",
                operation_id=operation_id,
                details=RepairDetails(
                    status=OperationState.READY,
                    diagnostic_only=False,
                    issues=diagnosis.issues,
                    repaired=True,
                ),
                evidence=RepairEvidence(
                    repository=resolved.evidence,
                    lock_sha256=final_diagnosis.lock_sha256,
                    environment_checked=True,
                    transaction_id=transaction.transaction_id,
                ),
            )
        except Exception as error:
            if transaction is None:
                raise
            rollback_confirmed = True
            try:
                if venv.exists():
                    if not self._is_owned_venv(
                        venv, transaction.transaction_id
                    ):
                        rollback_confirmed = False
                    else:
                        shutil.rmtree(venv)
                if backup_path is not None and backup_path.exists():
                    venv.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(backup_path), str(venv))
                journal_store = JournalStore(coordinator.layout, "repair")
                phase = "rolled_back" if rollback_confirmed else "rollback_unknown"
                journal_store.save(
                    transaction.model_copy(
                        update={
                            "phase": phase,
                            "backup_present": bool(
                                backup_path and backup_path.exists()
                            ),
                        }
                    )
                )
            except OSError, ToolingError:
                rollback_confirmed = False
            if rollback_confirmed:
                return ToolingResult[RepairDetails, RepairEvidence](
                    ok=False,
                    code=ResultCode.TOOLING_APPLY_FAILED_ROLLED_BACK,
                    state=OperationState.ROLLED_BACK,
                    message="Repair не завершён; исходная environment восстановлена.",
                    operation_id=operation_id,
                    details=RepairDetails(
                        status=OperationState.ROLLED_BACK,
                        diagnostic_only=False,
                        issues=("операция Repair завершилась ошибкой",),
                        repaired=False,
                    ),
                    evidence=RepairEvidence(
                        repository=resolved.evidence,
                        lock_sha256=None,
                        environment_checked=False,
                        transaction_id=transaction.transaction_id,
                    ),
                )
            raise ToolingError(
                ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                "Repair завершился с неоднозначным rollback; автоматическое продолжение запрещено.",
                operation_id=operation_id,
            ) from error
        finally:
            lock.release()


__all__ = ["RepairService"]
