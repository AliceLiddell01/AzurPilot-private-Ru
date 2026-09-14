"""Сервис Build/bootstrap поверх существующей канонической границы `deploy.uv`."""

from __future__ import annotations

import contextlib
import io
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from secrets import token_hex

from .adb import install_adb, resolve_adb
from .config import load_deploy_settings, project_adb, project_python, project_uv
from .contracts import (
    BuildDetails,
    BuildEvidence,
    CapabilityStatus,
    OperationState,
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
    bounded_read_bytes,
    bounded_read_text,
    is_unsafe_path,
    sha256_file,
)
from .path import register_console_path
from .process import ProcessSpec, StructuredProcessRunner, safe_environment
from .repository import RepositoryResolver
from .shortcut import ensure_shortcut

_UV_VERSION_RE = re.compile(r"\buv\s+(?P<version>\d+\.\d+\.\d+)", re.IGNORECASE)


def _expected_uv_version(root: Path) -> str | None:
    try:
        document = tomllib.loads(
            bounded_read_bytes(root / "pyproject.toml").decode("utf-8-sig")
        )
    except (OSError, UnicodeError, TypeError, ValueError, ToolingError):
        return None
    for dependency in document.get("project", {}).get("dependencies", ()):
        if isinstance(dependency, str) and dependency.lower().startswith("uv=="):
            return dependency.split("==", 1)[1].strip()
    return None


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    parts = value.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        return None
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def _uv_version_is_compatible(
    actual: str, expected: str | None, source: str
) -> bool:
    actual_tuple = _version_tuple(actual)
    if actual_tuple is None:
        return False
    if expected is None:
        return True
    expected_tuple = _version_tuple(expected)
    if expected_tuple is None:
        return False
    return actual_tuple == expected_tuple or (
        source == "PATH" and actual_tuple[:2] == expected_tuple[:2]
    )


def _safe_marker_text(transaction_id: str) -> str:
    return f"schema_version=1\ntransaction_id={transaction_id}\n"


class BootstrapService:
    """Проверка uv и вызов только разрешённой границы синхронизации проекта."""

    def __init__(self, runner: StructuredProcessRunner | None = None) -> None:
        self.runner = runner or StructuredProcessRunner()

    def resolve_uv(self, root: Path) -> tuple[Path, str]:
        settings = load_deploy_settings(root)
        candidates: list[tuple[Path, str]] = []
        configured = os.environ.get("AZURPILOT_BOOTSTRAP_UV")
        if configured:
            candidates.append((Path(configured), "environment"))
        if settings.uv_executable:
            candidates.append((project_uv(root, settings), "deploy_config"))
        candidates.append((project_uv(root), "project_venv"))
        system_uv = shutil.which("uv")
        if system_uv:
            candidates.append((Path(system_uv), "PATH"))
        expected = _expected_uv_version(root)
        seen: set[str] = set()
        for candidate, source in candidates:
            candidate = candidate.expanduser()
            if not candidate.is_absolute():
                candidate = root / candidate
            candidate = candidate.resolve(strict=False)
            key = os.path.normcase(str(candidate))
            if key in seen or not candidate.is_file():
                continue
            seen.add(key)
            result = self._probe_uv(candidate, root)
            if result is None:
                continue
            if not result.ok:
                continue
            match = _UV_VERSION_RE.search(result.stdout)
            if match is None or not _uv_version_is_compatible(
                match.group("version"), expected, source
            ):
                continue
            return candidate, source
        raise ToolingError(
            ResultCode.TOOLING_DEPENDENCY_UNAVAILABLE,
            "Не найден проверенный uv для bootstrap.",
        )

    def _probe_uv(self, candidate: Path, root: Path):
        try:
            return self.runner.run(
                ProcessSpec(
                    executable=candidate,
                    argv=("--version",),
                    cwd=root,
                    timeout_seconds=10,
                    max_output_bytes=8 * 1024,
                )
            )
        except (OSError, ValueError, ToolingError):
            return None

    def sync(
        self,
        root: Path,
        uv: Path,
        timeout_seconds: float,
        *,
        project_path: Path | None = None,
        project_environment: Path | None = None,
        python_executable: Path | None = None,
        state_root: Path | None = None,
        install_project: bool = True,
    ) -> str:
        """Использовать `deploy.uv` как каноническую границу политики без shell-обёртки."""

        output = io.StringIO()
        try:
            from deploy.uv import sync_project_venv

            environment = safe_environment(
                {"PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}
            )
            options: dict[str, object] = {}
            if project_path is not None:
                options["project_path"] = project_path
            if project_environment is not None:
                options["project_environment"] = project_environment
            if python_executable is not None:
                options["python_executable"] = python_executable
            if state_root is not None:
                options["state_root"] = state_root
            if project_path is not None or project_environment is not None:
                options["install_project"] = install_project
            with contextlib.redirect_stdout(output):
                sync_project_venv(
                    root=root,
                    bootstrap_uv=uv,
                    capture_output=True,
                    timeout=timeout_seconds,
                    environment=environment,
                    **options,
                )
        except TimeoutError as exc:
            raise ToolingError(
                ResultCode.TOOLING_TIMEOUT, "Синхронизация uv превысила установленный срок."
            ) from exc
        except (
            OSError,
            RuntimeError,
            ValueError,
            ToolingError,
            subprocess.SubprocessError,
        ) as exc:
            raise ToolingError(
                ResultCode.TOOLING_DEPENDENCY_UNAVAILABLE,
                "Синхронизация окружения проекта завершилась ошибкой.",
            ) from exc
        return output.getvalue()[: 64 * 1024]


class BuildService:
    """Подготовка checkout без Git Update и без удаления исправной `.venv`."""

    def __init__(
        self,
        resolver: RepositoryResolver | None = None,
        runner: StructuredProcessRunner | None = None,
        bootstrap: BootstrapService | None = None,
    ) -> None:
        self.resolver = resolver or RepositoryResolver()
        self.runner = runner or StructuredProcessRunner()
        self.bootstrap = bootstrap or BootstrapService(self.runner)

    def _run_health(
        self, executable: Path, root: Path, *args: str, timeout_seconds: float = 30.0
    ) -> bool:
        try:
            result = self.runner.run(
                ProcessSpec(
                    executable=executable,
                    argv=tuple(args),
                    cwd=root,
                    timeout_seconds=timeout_seconds,
                    max_output_bytes=32 * 1024,
                )
            )
        except (OSError, ValueError, ToolingError):
            return False
        return result.ok

    @staticmethod
    def _is_owned_venv(venv: Path, transaction_id: str) -> bool:
        marker = venv / ".azurpilot-build-owned"
        if is_unsafe_path(venv) or is_unsafe_path(marker) or not marker.is_file():
            return False
        try:
            value = bounded_read_text(marker, max_bytes=4096)
        except ToolingError:
            return False
        return any(
            line.strip() == f"transaction_id={transaction_id}"
            for line in value.splitlines()
        )

    def _assert_stopped(self, root: Path) -> None:
        coordinator = RepositoryCoordinator.for_root(root)
        record = coordinator.read_lifecycle()
        if record is not None:
            identity = coordinator.identity_from_record(record)
            if identity.matches():
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    "Build требует остановленного WebUI.",
                )
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Старое состояние lifecycle не подтверждается текущим процессом; Build остановлен.",
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
                "Build не выполняется при занятом порте WebUI.",
            )

    def _copy_template(self, root: Path) -> bool:
        destination = root / "config" / "deploy.yaml"
        if is_unsafe_path(destination):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "config/deploy.yaml имеет небезопасный тип.",
            )
        if destination.exists():
            return False
        template = root / "config" / "deploy.template.yaml"
        if is_unsafe_path(template) or not template.is_file():
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "config/deploy.template.yaml отсутствует.",
            )
        data = bounded_read_bytes(template)
        ScopedPath(root).atomic_write_bytes(destination.relative_to(root), data)
        return True

    def build(
        self,
        repository_root: str | Path | None = None,
        *,
        timeout_seconds: float = 30 * 60,
        create_shortcut: bool | None = None,
    ) -> ToolingResult[BuildDetails, BuildEvidence]:
        if not 0 < timeout_seconds <= 24 * 60 * 60:
            raise ToolingError(
                ResultCode.TOOLING_INVALID_INVOCATION,
                "Срок должен быть положительным и ограниченным числом.",
            )
        resolved = self.resolver.resolve(repository_root)
        root = resolved.path
        operation_id = f"build-{token_hex(8)}"
        lock = RepositoryCoordinator.for_root(root).lock("build")
        if not lock.acquire(timeout_seconds=0):
            raise ToolingError(
                ResultCode.TOOLING_OPERATION_CONFLICT,
                "Build уже выполняется.",
                operation_id=operation_id,
            )
        config_created = False
        transaction = None
        venv = root / ".venv"
        venv_was_present = venv.exists() or is_unsafe_path(venv)
        layout = RepositoryCoordinator.for_root(root).layout
        journal_store = JournalStore(layout, "build")
        shortcut_requested = os.name == "nt" if create_shortcut is None else create_shortcut
        try:
            self._assert_stopped(root)
            if (
                not (root / "pyproject.toml").is_file()
                or not (root / "uv.lock").is_file()
                or not (root / "gui.py").is_file()
            ):
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    "Checkout не содержит обязательные маркеры Build.",
                    operation_id=operation_id,
                )
            if venv_was_present and (is_unsafe_path(venv) or not venv.is_dir()):
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    ".venv имеет небезопасный тип.",
                    operation_id=operation_id,
                )
            update_journal = JournalStore(layout, "update").active()
            repair_journal = JournalStore(layout, "repair").active()
            build_journal = journal_store.active()
            if (
                update_journal is not None
                or repair_journal is not None
                or build_journal is not None
            ):
                raise ToolingError(
                    ResultCode.TOOLING_OPERATION_CONFLICT,
                    "Незавершённая транзакция блокирует Build.",
                    operation_id=operation_id,
                )
            lock_hash = sha256_file(root / "uv.lock")
            template_hash = sha256_file(root / "config" / "deploy.template.yaml")
            transaction = journal_store.create()
            journal_store.save(transaction)
            config_created = self._copy_template(root)
            settings = load_deploy_settings(root)
            venv_created = not venv_was_present
            if venv_was_present:
                python = project_python(root, settings)
                uv = project_uv(root, settings)
                if not python.is_file() or not self._run_health(
                    python, root, "-c", "import sys; raise SystemExit(0)"
                ):
                    raise ToolingError(
                        ResultCode.TOOLING_PRECONDITION_FAILED,
                        "Существующая `.venv` неисправна; используйте Repair.",
                        operation_id=operation_id,
                    )
                if not uv.is_file() or not self._run_health(uv, root, "--version"):
                    raise ToolingError(
                        ResultCode.TOOLING_PRECONDITION_FAILED,
                        "Существующий uv в `.venv` не запускается; используйте Repair.",
                        operation_id=operation_id,
                    )
                if not self._run_health(
                    uv,
                    root,
                    "sync",
                    "--project",
                    str(root),
                    "--python",
                    str(python),
                    "--frozen",
                    "--no-dev",
                    "--dry-run",
                    timeout_seconds=min(180.0, timeout_seconds),
                ):
                    raise ToolingError(
                        ResultCode.TOOLING_PRECONDITION_FAILED,
                        "Существующая `.venv` не согласована с uv.lock; используйте Repair.",
                        operation_id=operation_id,
                    )
                bootstrap_source = "existing_environment"
            else:
                venv.mkdir(parents=True, exist_ok=True)
                ScopedPath(venv).atomic_write_text(
                    ".azurpilot-build-owned",
                    _safe_marker_text(transaction.transaction_id),
                )
                uv, bootstrap_source = self.bootstrap.resolve_uv(root)
                self.bootstrap.sync(root, uv, timeout_seconds)
                transaction = transaction.model_copy(
                    update={
                        "phase": "environment_built",
                        "backup_present": False,
                        "ownership_confirmed": True,
                        "venv_path": str(venv),
                    }
                )
                journal_store.save(transaction)
                settings = load_deploy_settings(root)
                python = project_python(root, settings)
                if not python.is_file() or not self._run_health(
                    python,
                    root,
                    "-c",
                    "import sys; raise SystemExit(0)",
                    timeout_seconds=30,
                ):
                    raise ToolingError(
                        ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                        "После подготовки среда Python не подтверждена.",
                        operation_id=operation_id,
                    )
            if (
                sha256_file(root / "uv.lock") != lock_hash
                or sha256_file(root / "config" / "deploy.template.yaml")
                != template_hash
            ):
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "uv.lock или template изменились во время Build.",
                    operation_id=operation_id,
                )
            adb_path = project_adb(root, settings)
            adb_version: str | None = None
            if os.name == "nt":
                adb_resolution = resolve_adb(root, settings, layout, self.runner)
                adb_status = adb_resolution.status
                adb_version = adb_resolution.version
                if adb_resolution.path is not None:
                    same_path = (
                        adb_resolution.path.resolve(strict=False)
                        == adb_path.resolve(strict=False)
                    )
                    if not same_path:
                        install_adb(adb_resolution.path, adb_path.parent, root, self.runner)
                    if not self._run_health(adb_path, root, "version"):
                        raise ToolingError(
                            ResultCode.TOOLING_ADB_FAILED,
                            "После Build установленный ADB не прошёл проверку постусловия.",
                            operation_id=operation_id,
                        )
            else:
                adb_status = (
                    CapabilityStatus.READY
                    if adb_path.is_file() and self._run_health(adb_path, root, "version")
                    else CapabilityStatus.NOT_CONFIGURED
                )
            warnings: list[ToolingWarning] = []
            if adb_status is CapabilityStatus.NOT_CONFIGURED:
                warnings.append(
                    ToolingWarning(
                        code=WarningCode.TOOLING_ADB_NOT_CONFIGURED,
                        message="ADB не настроен; Build не выполнял действий с устройством.",
                    )
                )
            console_path = register_console_path(python)
            if console_path.status is not CapabilityStatus.READY:
                warnings.append(
                    ToolingWarning(
                        code=WarningCode.TOOLING_CLI_NOT_ON_PATH,
                        message=console_path.message,
                    )
                )
            shortcut_status = CapabilityStatus.UNSUPPORTED
            if shortcut_requested:
                if os.name == "nt":
                    shortcut = ensure_shortcut(
                        root,
                        python,
                        layout,
                        shortcut_path=(
                            (
                                root / settings.shortcut_path
                                if settings.shortcut_path
                                and not Path(settings.shortcut_path).is_absolute()
                                else Path(settings.shortcut_path)
                            )
                            if settings.shortcut_path
                            else None
                        ),
                        icon_path=(
                            (
                                root / settings.shortcut_icon
                                if settings.shortcut_icon
                                and not Path(settings.shortcut_icon).is_absolute()
                                else Path(settings.shortcut_icon)
                            )
                            if settings.shortcut_icon
                            else None
                        ),
                    )
                    shortcut_status = shortcut.status
                    transaction = transaction.model_copy(update={"phase": "shortcut_ready"})
                    journal_store.save(transaction)
                else:
                    shortcut_status = CapabilityStatus.UNSUPPORTED
                    warnings.append(
                        ToolingWarning(
                            code=WarningCode.TOOLING_SHORTCUT_UNSUPPORTED,
                            message="Ярлык Windows не применяется на POSIX; используйте azur start.",
                        )
                    )
            transaction = transaction.model_copy(update={"phase": "path_registered"})
            journal_store.save(transaction)
            transaction = transaction.model_copy(update={"phase": "completed"})
            journal_store.save(transaction)
            journal_store.retain_terminal()
            return ToolingResult[BuildDetails, BuildEvidence](
                ok=True,
                code=ResultCode.OK,
                state=OperationState.READY,
                message="Checkout подготовлен; исправная `.venv` сохранена или создана с проверенной подготовкой.",
                operation_id=operation_id,
                details=BuildDetails(
                    status=OperationState.READY,
                    venv_created=venv_created,
                    config_created=config_created,
                    bootstrap_source=bootstrap_source,
                    adb_status=adb_status,
                    adb_version=adb_version,
                    console_script=(
                        CapabilityStatus.READY
                        if console_path.installed
                        else CapabilityStatus.NOT_CONFIGURED
                    ),
                    path_registration=console_path.status,
                    shortcut_status=shortcut_status,
                ),
                warnings=tuple(warnings),
                evidence=BuildEvidence(
                    repository=resolved.evidence,
                    lock_sha256=lock_hash,
                    template_sha256=template_hash,
                    transaction_id=transaction.transaction_id if transaction else None,
                ),
            )
        except Exception as error:
            rollback_confirmed = True
            if config_created:
                config = root / "config" / "deploy.yaml"
                if config.exists() and not is_unsafe_path(config):
                    try:
                        config.unlink()
                    except OSError:
                        rollback_confirmed = False
                elif config.exists():
                    rollback_confirmed = False
            if transaction is not None and not venv_was_present:
                try:
                    if venv.exists():
                        if not self._is_owned_venv(venv, transaction.transaction_id):
                            rollback_confirmed = False
                        else:
                            shutil.rmtree(venv)
                except (OSError, ToolingError):
                    rollback_confirmed = False
            if transaction is not None:
                try:
                    phase = "rolled_back" if rollback_confirmed else "rollback_unknown"
                    transaction = transaction.model_copy(
                        update={
                            "phase": phase,
                            "failure_reason": type(error).__name__,
                        }
                    )
                    journal_store.save(transaction)
                    if rollback_confirmed:
                        journal_store.retain_terminal()
                except (OSError, ToolingError, ValueError):
                    rollback_confirmed = False
            if rollback_confirmed:
                raise ToolingError(
                    ResultCode.TOOLING_APPLY_FAILED_ROLLED_BACK,
                    "Build не завершён; подтверждённое частичное состояние очищено, повторный Build разрешён.",
                    operation_id=operation_id,
                ) from error
            raise ToolingError(
                ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                "Build завершился с неоднозначной очисткой; повторное изменение запрещено до восстановления только для чтения.",
                operation_id=operation_id,
            ) from error
        finally:
            lock.release()


__all__ = ["BootstrapService", "BuildService"]
