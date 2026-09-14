"""Build/bootstrap service поверх существующего canonical `deploy.uv` seam."""

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

_UV_VERSION_RE = re.compile(r"\buv\s+(?P<version>\d+\.\d+\.\d+)", re.IGNORECASE)


def _expected_uv_version(root: Path) -> str | None:
    try:
        document = tomllib.loads(
            bounded_read_bytes(root / "pyproject.toml").decode("utf-8-sig")
        )
    except OSError, UnicodeError, TypeError, ValueError, ToolingError:
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
    """Проверка uv и вызов только разрешённого project sync seam."""

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
        except OSError, ValueError, ToolingError:
            return None

    def sync(self, root: Path, uv: Path, timeout_seconds: float) -> str:
        """Использовать `deploy.uv` как canonical policy seam, не shell wrapper."""

        output = io.StringIO()
        try:
            from deploy.uv import sync_project_venv

            environment = safe_environment(
                {"PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}
            )
            with contextlib.redirect_stdout(output):
                sync_project_venv(
                    root=root,
                    bootstrap_uv=uv,
                    capture_output=True,
                    timeout=timeout_seconds,
                    environment=environment,
                )
        except TimeoutError as exc:
            raise ToolingError(
                ResultCode.TOOLING_TIMEOUT, "Синхронизация uv превысила deadline."
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
                "Синхронизация project environment завершилась ошибкой.",
            ) from exc
        return output.getvalue()[: 64 * 1024]


class BuildService:
    """Подготовка checkout без Git update и без удаления здоровой `.venv`."""

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
        except OSError, ValueError, ToolingError:
            return False
        return result.ok

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
                "Build не выполняется при занятом WebUI port.",
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
        create_shortcut: bool = False,
    ) -> ToolingResult[BuildDetails, BuildEvidence]:
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
        marker_path = venv / ".azurpilot-build-owned"
        venv_was_present = venv.exists() or is_unsafe_path(venv)
        try:
            self._assert_stopped(root)
            if (
                not (root / "pyproject.toml").is_file()
                or not (root / "uv.lock").is_file()
                or not (root / "gui.py").is_file()
            ):
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    "Checkout не содержит обязательные Build markers.",
                    operation_id=operation_id,
                )
            if venv_was_present and (is_unsafe_path(venv) or not venv.is_dir()):
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    ".venv имеет небезопасный тип.",
                    operation_id=operation_id,
                )
            update_journal = JournalStore(
                RepositoryCoordinator.for_root(root).layout, "update"
            ).active()
            repair_journal = JournalStore(
                RepositoryCoordinator.for_root(root).layout, "repair"
            ).active()
            build_journal = JournalStore(
                RepositoryCoordinator.for_root(root).layout, "build"
            ).active()
            if (
                update_journal is not None
                or repair_journal is not None
                or build_journal is not None
            ):
                raise ToolingError(
                    ResultCode.TOOLING_OPERATION_CONFLICT,
                    "Незавершённая transaction блокирует Build.",
                    operation_id=operation_id,
                )
            lock_hash = sha256_file(root / "uv.lock")
            template_hash = sha256_file(root / "config" / "deploy.template.yaml")
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
                        "Существующая .venv неисправна; используйте repair.",
                        operation_id=operation_id,
                    )
                if not uv.is_file() or not self._run_health(uv, root, "--version"):
                    raise ToolingError(
                        ResultCode.TOOLING_PRECONDITION_FAILED,
                        "Существующий uv в .venv не запускается; используйте repair.",
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
                        "Существующая .venv не согласована с uv.lock; используйте repair.",
                        operation_id=operation_id,
                    )
                bootstrap_source = "existing_environment"
            else:
                transaction = JournalStore(
                    RepositoryCoordinator.for_root(root).layout, "build"
                ).create()
                JournalStore(RepositoryCoordinator.for_root(root).layout, "build").save(
                    transaction
                )
                venv.mkdir(parents=True, exist_ok=True)
                ScopedPath(venv).atomic_write_text(
                    ".azurpilot-build-owned",
                    _safe_marker_text(transaction.transaction_id),
                )
                uv, bootstrap_source = self.bootstrap.resolve_uv(root)
                self.bootstrap.sync(root, uv, timeout_seconds)
                transaction = transaction.model_copy(
                    update={"phase": "environment_built", "backup_present": False}
                )
                JournalStore(RepositoryCoordinator.for_root(root).layout, "build").save(
                    transaction
                )
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
                        "После bootstrap Python environment не подтверждён.",
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
            adb_status = (
                CapabilityStatus.READY
                if adb_path.is_file() and self._run_health(adb_path, root, "version")
                else CapabilityStatus.NOT_CONFIGURED
            )
            warnings: list[ToolingWarning] = []
            if adb_status is not CapabilityStatus.READY:
                warnings.append(
                    ToolingWarning(
                        code=WarningCode.TOOLING_ADB_NOT_CONFIGURED,
                        message="ADB не настроен; Build не выполнял device action.",
                    )
                )
            if create_shortcut:
                warnings.append(
                    ToolingWarning(
                        code=WarningCode.TOOLING_SHORTCUT_UNSUPPORTED,
                        message="Shortcut adapter не входит в этот cross-platform core increment.",
                    )
                )
            if transaction is not None:
                JournalStore(RepositoryCoordinator.for_root(root).layout, "build").save(
                    transaction.model_copy(update={"phase": "completed"})
                )
            console_path = register_console_path(python)
            if console_path.status is not CapabilityStatus.READY:
                warnings.append(
                    ToolingWarning(
                        code=WarningCode.TOOLING_CLI_NOT_ON_PATH,
                        message=console_path.message,
                    )
                )
            return ToolingResult[BuildDetails, BuildEvidence](
                ok=True,
                code=ResultCode.OK,
                state=OperationState.READY,
                message="Checkout подготовлен; здоровая `.venv` сохранена или создана с проверенным bootstrap.",
                operation_id=operation_id,
                details=BuildDetails(
                    status=OperationState.READY,
                    venv_created=venv_created,
                    config_created=config_created,
                    bootstrap_source=bootstrap_source,
                    adb_status=adb_status,
                    console_script=(
                        CapabilityStatus.READY
                        if console_path.installed
                        else CapabilityStatus.NOT_CONFIGURED
                    ),
                    path_registration=console_path.status,
                ),
                warnings=tuple(warnings),
                evidence=BuildEvidence(
                    repository=resolved.evidence,
                    lock_sha256=lock_hash,
                    template_sha256=template_hash,
                    transaction_id=transaction.transaction_id if transaction else None,
                ),
            )
        except Exception:
            if config_created:
                config = root / "config" / "deploy.yaml"
                if config.exists() and not is_unsafe_path(config):
                    try:
                        config.unlink()
                    except OSError:
                        pass
            owned_partial = False
            try:
                owned_partial = (
                    not venv_was_present
                    and not is_unsafe_path(venv)
                    and not is_unsafe_path(marker_path)
                    and marker_path.is_file()
                    and bounded_read_text(marker_path, max_bytes=4096).startswith(
                        "schema_version=1"
                    )
                )
            except (OSError, ToolingError):
                owned_partial = False
            if owned_partial:
                try:
                    shutil.rmtree(venv)
                except OSError:
                    pass
            raise
        finally:
            lock.release()


__all__ = ["BootstrapService", "BuildService"]
