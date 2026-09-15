"""Диагностика и fail-closed восстановление окружения проекта."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from secrets import token_hex

from .bootstrap import BootstrapService
from .config import load_deploy_settings, project_python, project_uv
from .contracts import (
    CapabilityStatus,
    OperationState,
    RepairDetails,
    RepairEvidence,
    ResultCode,
    ToolingResult,
    ToolingWarning,
    WarningCode,
)
from .coordination import RepositoryCoordinator, observe_tcp_port
from .errors import ToolingError
from .filesystem import (
    JournalStore,
    ScopedPath,
    bounded_read_text,
    is_unsafe_path,
    path_has_link,
    sha256_file,
)
from .process import ProcessSpec, StructuredProcessRunner
from .repository import RepositoryResolver
from .shortcut import ensure_shortcut


@dataclass(frozen=True)
class _Diagnosis:
    issues: tuple[str, ...]
    python_path: Path | None
    uv_path: Path | None
    lock_sha256: str | None

    @property
    def healthy(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class _RollbackAssessment:
    confirmed: bool
    reason: str


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
            issues.append("Python проекта отсутствует")
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
                    issues.append("Python проекта не запускается")
            except (OSError, ValueError, ToolingError):
                issues.append("Python проекта не запускается")
        if not uv.is_file():
            issues.append("uv проекта отсутствует")
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
                    issues.append("uv проекта не запускается")
            except (OSError, ValueError, ToolingError):
                issues.append("uv проекта не запускается")
        return _Diagnosis(
            tuple(issues[:16]),
            python if python.is_file() else None,
            uv if uv.is_file() else None,
            lock_hash,
        )

    def _assert_stopped(self, root: Path) -> None:
        coordinator = RepositoryCoordinator.for_root(root)
        record = coordinator.read_lifecycle()
        if record is not None:
            if coordinator.identity_from_record(record).matches():
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    "Repair требует остановленного WebUI.",
                )
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Старое состояние lifecycle не подтверждается текущим процессом; Repair остановлен.",
            )
        settings = load_deploy_settings(root)
        port = observe_tcp_port(settings.webui_port)
        if port.inspection_failed or port.listener_present is None:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Нельзя подтвердить свободный порт WebUI.",
            )
        if port.listener_present or port.pids:
            raise ToolingError(
                ResultCode.TOOLING_PORT_CONFLICT,
                "Repair не выполняется при занятом порте WebUI.",
            )

    @staticmethod
    def _configured_path(root: Path, value: str | None) -> Path | None:
        if not value:
            return None
        candidate = Path(value).expanduser()
        return candidate if candidate.is_absolute() else root / candidate

    def _repair_shortcut(
        self, root: Path, settings, coordinator: RepositoryCoordinator
    ) -> tuple[CapabilityStatus, tuple[ToolingWarning, ...]]:
        python = project_python(root, settings)
        if not python.is_file():
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Для восстановления ярлыка отсутствует Python проекта.",
            )
        result = ensure_shortcut(
            root,
            python,
            coordinator.layout,
            shortcut_path=self._configured_path(root, settings.shortcut_path),
            icon_path=self._configured_path(root, settings.shortcut_icon),
        )
        if result.status is CapabilityStatus.UNSUPPORTED:
            return (
                result.status,
                (
                    ToolingWarning(
                        code=WarningCode.TOOLING_SHORTCUT_UNSUPPORTED,
                        message="Ярлык Windows не применяется на POSIX.",
                    ),
                ),
            )
        return result.status, ()

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
        if is_unsafe_path(venv) or is_unsafe_path(marker) or not marker.is_file():
            return False
        try:
            lines = bounded_read_text(marker, max_bytes=4096).splitlines()
        except ToolingError:
            return False
        return any(line.strip() == f"transaction_id={transaction_id}" for line in lines)

    @staticmethod
    def _tree_safe(path: Path) -> bool:
        if is_unsafe_path(path) or path_has_link(path.parent):
            return False
        try:
            for item in path.rglob("*"):
                if item.is_symlink():
                    # Обычный POSIX venv может содержать ссылку bin/python.
                    # Разрешаем только ссылки на обычные файлы и копируем их содержимое.
                    if os.name == "nt":
                        return False
                    try:
                        target = item.resolve(strict=True)
                    except (OSError, RuntimeError):
                        return False
                    if item.is_dir() or not target.is_file():
                        return False
                    continue
                if is_unsafe_path(item):
                    return False
            return True
        except OSError:
            return False

    @classmethod
    def _copy_tree(cls, source: Path, destination: Path) -> None:
        if not source.is_dir() or not cls._tree_safe(source):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Исходная .venv содержит небезопасный объект.",
            )
        if os.path.lexists(str(destination)):
            if not cls._tree_safe(destination):
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Резервная копия `.venv` содержит symlink или reparse point.",
                )
            raise ToolingError(
                ResultCode.TOOLING_OPERATION_CONFLICT,
                "Резервная копия `.venv` уже существует.",
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(source, destination, symlinks=False)
        except (OSError, shutil.Error) as exc:
            raise ToolingError(
                ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                "Не удалось создать проверенную резервную копию .venv.",
            ) from exc
        if not cls._tree_safe(destination):
            raise ToolingError(
                ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                "Созданная резервная копия .venv не прошла проверку безопасности.",
            )

    @classmethod
    def _assess_rollback(
        cls,
        venv: Path,
        backup_path: Path | None,
        transaction_id: str,
        original_existed: bool,
        replacement_started: bool,
    ) -> _RollbackAssessment:
        if not replacement_started:
            return _RollbackAssessment(True, "no_replacement_started")
        if is_unsafe_path(venv) or (venv.exists() and not venv.is_dir()):
            return _RollbackAssessment(False, "venv_ownership_ambiguous")
        if venv.exists() and not cls._is_owned_venv(venv, transaction_id):
            return _RollbackAssessment(False, "venv_ownership_ambiguous")
        if not original_existed:
            return _RollbackAssessment(True, "no_original_venv")
        if backup_path is None:
            return _RollbackAssessment(False, "backup_missing")
        if is_unsafe_path(backup_path) or not backup_path.is_dir():
            return _RollbackAssessment(False, "backup_missing_or_unsafe")
        if not cls._tree_safe(backup_path):
            return _RollbackAssessment(False, "backup_tree_unsafe")
        siblings = tuple(
            item
            for item in backup_path.parent.glob("venv-backup*")
            if item.exists()
        )
        if len(siblings) != 1:
            return _RollbackAssessment(False, "multiple_backups")
        return _RollbackAssessment(True, "ownership_and_backup_confirmed")

    @staticmethod
    def _diagnostic_transaction_result(
        resolved: object,
        operation_id: str,
        reason: str,
    ) -> ToolingResult[RepairDetails, RepairEvidence]:
        return ToolingResult[RepairDetails, RepairEvidence](
            ok=False,
            code=ResultCode.TOOLING_TRANSACTION_RECOVERY_REQUIRED,
            state=OperationState.UNKNOWN,
            message="Обнаружено незавершённое состояние; запись не изменена. Сначала выполните диагностику только для чтения и восстановление.",
            operation_id=operation_id,
            details=RepairDetails(
                status=OperationState.UNKNOWN,
                diagnostic_only=True,
                issues=(reason,),
                repaired=False,
                recovery_reason=reason,
                ownership_state="unknown",
            ),
            evidence=RepairEvidence(
                repository=resolved.evidence,  # type: ignore[attr-defined]
                environment_checked=False,
            ),
        )

    def repair(
        self,
        repository_root: str | Path | None = None,
        *,
        diagnostic_only: bool = False,
        repair_shortcut: bool = False,
        shortcut_only: bool = False,
        timeout_seconds: float = 30 * 60,
    ) -> ToolingResult[RepairDetails, RepairEvidence]:
        if not 0 < timeout_seconds <= 24 * 60 * 60:
            raise ToolingError(
                ResultCode.TOOLING_INVALID_INVOCATION,
                "Срок должен быть положительным и ограниченным числом.",
            )
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
        original_existed = False
        replacement_started = False
        venv = root / ".venv"
        journal_store = JournalStore(coordinator.layout, "repair")
        shortcut_status = CapabilityStatus.UNSUPPORTED
        shortcut_warnings: tuple[ToolingWarning, ...] = ()
        try:
            if diagnostic_only and (repair_shortcut or shortcut_only):
                raise ToolingError(
                    ResultCode.TOOLING_INVALID_INVOCATION,
                    "Режим диагностики только для чтения нельзя совмещать с восстановлением ярлыка.",
                    operation_id=operation_id,
                )
            self._assert_stopped(root)
            update_journal = JournalStore(coordinator.layout, "update").active()
            repair_journal = journal_store.active()
            build_journal = JournalStore(coordinator.layout, "build").active()
            if update_journal or repair_journal or build_journal:
                reason = "active transaction: " + ", ".join(
                    item.operation
                    for item in (update_journal, repair_journal, build_journal)
                    if item is not None
                )
                if diagnostic_only:
                    return self._diagnostic_transaction_result(
                        resolved, operation_id, reason
                    )
                raise ToolingError(
                    ResultCode.TOOLING_OPERATION_CONFLICT,
                    "Незавершённая транзакция блокирует Repair; используйте diagnostic-only или восстановление Update.",
                    operation_id=operation_id,
                )
            if shortcut_only:
                settings = load_deploy_settings(root)
                shortcut_status, shortcut_warnings = self._repair_shortcut(
                    root, settings, coordinator
                )
                return ToolingResult[RepairDetails, RepairEvidence](
                    ok=True,
                    code=ResultCode.OK,
                    state=OperationState.READY,
                    message="Ярлык Windows проверен и восстановлен."
                    if shortcut_status is CapabilityStatus.READY
                    else "Ярлык Windows не применяется на POSIX.",
                    operation_id=operation_id,
                    details=RepairDetails(
                        status=OperationState.READY,
                        diagnostic_only=False,
                        issues=(),
                        repaired=shortcut_status is CapabilityStatus.READY,
                        ownership_state="confirmed",
                        shortcut_status=shortcut_status,
                    ),
                    warnings=shortcut_warnings,
                    evidence=RepairEvidence(
                        repository=resolved.evidence,
                        environment_checked=False,
                    ),
                )
            diagnosis = self._diagnose(root)
            if diagnostic_only or (diagnosis.healthy and not repair_shortcut):
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
                        ownership_state="confirmed"
                        if diagnosis.healthy
                        else "unknown",
                        shortcut_status=shortcut_status,
                    ),
                    evidence=RepairEvidence(
                        repository=resolved.evidence,
                        lock_sha256=diagnosis.lock_sha256,
                        environment_checked=True,
                    ),
                )

            if diagnosis.healthy and repair_shortcut:
                settings = load_deploy_settings(root)
                shortcut_status, shortcut_warnings = self._repair_shortcut(
                    root, settings, coordinator
                )
                return ToolingResult[RepairDetails, RepairEvidence](
                    ok=True,
                    code=ResultCode.OK,
                    state=OperationState.READY,
                    message="Окружение исправно; ярлык Windows проверен и восстановлен."
                    if shortcut_status is CapabilityStatus.READY
                    else "Окружение исправно; ярлык Windows не применяется на POSIX.",
                    operation_id=operation_id,
                    details=RepairDetails(
                        status=OperationState.READY,
                        diagnostic_only=False,
                        issues=(),
                        repaired=shortcut_status is CapabilityStatus.READY,
                        ownership_state="confirmed",
                        shortcut_status=shortcut_status,
                    ),
                    warnings=shortcut_warnings,
                    evidence=RepairEvidence(
                        repository=resolved.evidence,
                        lock_sha256=diagnosis.lock_sha256,
                        environment_checked=True,
                    ),
                )

            if is_unsafe_path(venv) or (venv.exists() and not venv.is_dir()):
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    "Повреждённая .venv имеет небезопасный тип; запись остановлена.",
                    operation_id=operation_id,
                )
            original_existed = venv.exists()
            transaction = journal_store.create()
            transaction = transaction.model_copy(
                update={"venv_path": str(venv), "ownership_confirmed": True}
            )
            journal_store.save(transaction)
            if original_existed:
                transaction_dir = coordinator.layout.transactions_directory / transaction.transaction_id
                backup_path = transaction_dir / "venv-backup"
                self._copy_tree(venv, backup_path)
                transaction = transaction.model_copy(
                    update={
                        "phase": "backup_ready",
                        "backup_present": True,
                        "backup_path": str(backup_path),
                    }
                )
                journal_store.save(transaction)
                replacement_started = True
                shutil.rmtree(venv)
            replacement_started = True
            self._mark_owned_venv(venv, transaction.transaction_id)
            transaction = transaction.model_copy(update={"phase": "rebuild_started"})
            journal_store.save(transaction)
            uv, _ = self.bootstrap.resolve_uv(root)
            self.bootstrap.sync(root, uv, timeout_seconds)
            final_diagnosis = self._diagnose(root)
            if not final_diagnosis.healthy:
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "После Repair среда не прошла проверку.",
                    operation_id=operation_id,
                )
            if repair_shortcut:
                settings = load_deploy_settings(root)
                shortcut_status, shortcut_warnings = self._repair_shortcut(
                    root, settings, coordinator
                )
                transaction = transaction.model_copy(update={"phase": "shortcut_ready"})
                journal_store.save(transaction)
            transaction = transaction.model_copy(update={"phase": "completed"})
            journal_store.save(transaction)
            journal_store.retain_terminal()
            return ToolingResult[RepairDetails, RepairEvidence](
                ok=True,
                code=ResultCode.OK,
                state=OperationState.READY,
                message="Окружение проекта восстановлено и проверено; исходная `.venv` сохранена во внешней резервной копии.",
                operation_id=operation_id,
                details=RepairDetails(
                    status=OperationState.READY,
                    diagnostic_only=False,
                    issues=diagnosis.issues,
                    repaired=True,
                    ownership_state="confirmed",
                    shortcut_status=shortcut_status,
                ),
                warnings=shortcut_warnings,
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
            assessment = self._assess_rollback(
                venv,
                backup_path,
                transaction.transaction_id,
                original_existed,
                replacement_started,
            )
            if assessment.confirmed:
                try:
                    if venv.exists() and replacement_started:
                        shutil.rmtree(venv)
                    if not replacement_started and backup_path is not None and backup_path.exists():
                        if is_unsafe_path(backup_path) or not self._tree_safe(backup_path):
                            raise ToolingError(
                                ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                            "Неполную резервную копию нельзя безопасно удалить.",
                            )
                        shutil.rmtree(backup_path)
                    if replacement_started and original_existed and backup_path is not None:
                        venv.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(backup_path), str(venv))
                    transaction = transaction.model_copy(
                        update={
                            "phase": "rolled_back",
                            "backup_present": bool(backup_path and backup_path.exists()),
                            "failure_reason": assessment.reason,
                        }
                    )
                    journal_store.save(transaction)
                    journal_store.retain_terminal()
                except (OSError, ToolingError) as rollback_error:
                    assessment = _RollbackAssessment(False, "rollback_failed")
                    try:
                        journal_store.save(
                            transaction.model_copy(
                                update={
                                    "phase": "rollback_unknown",
                                    "failure_reason": assessment.reason,
                                }
                            )
                        )
                    except (OSError, ToolingError):
                        pass
                    raise ToolingError(
                        ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                        "Repair завершился с ошибкой отката; запись остановлена до ручного восстановления.",
                        operation_id=operation_id,
                    ) from rollback_error
                return ToolingResult[RepairDetails, RepairEvidence](
                    ok=False,
                    code=ResultCode.TOOLING_APPLY_FAILED_ROLLED_BACK,
                    state=OperationState.ROLLED_BACK,
                    message="Repair не завершён; исходное окружение восстановлено, повторный запуск разрешён.",
                    operation_id=operation_id,
                    details=RepairDetails(
                        status=OperationState.ROLLED_BACK,
                        diagnostic_only=False,
                        issues=("операция Repair завершилась ошибкой",),
                        repaired=False,
                        recovery_reason=assessment.reason,
                        ownership_state="confirmed",
                        shortcut_status=shortcut_status,
                    ),
                    evidence=RepairEvidence(
                        repository=resolved.evidence,
                        environment_checked=False,
                        transaction_id=transaction.transaction_id,
                    ),
                )
            try:
                journal_store.save(
                    transaction.model_copy(
                        update={
                            "phase": "rollback_unknown",
                            "failure_reason": assessment.reason,
                        }
                    )
                )
            except (OSError, ToolingError):
                pass
            raise ToolingError(
                ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                f"Repair обнаружил {assessment.reason}; резервная копия не перемещена, запись остановлена до восстановления.",
                operation_id=operation_id,
            ) from error
        finally:
            lock.release()


__all__ = ["RepairService"]
