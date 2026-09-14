"""Диагностика фундаментальных возможностей проекта без изменений."""

from __future__ import annotations

import platform
import shutil
import sys
from pathlib import Path

from .config import load_deploy_settings, project_adb, project_python, project_uv
from .contracts import (
    CapabilityCheck,
    CapabilityStatus,
    DoctorDetails,
    DoctorEvidence,
    OperationState,
    ResultCode,
    ToolingResult,
    ToolingWarning,
    WarningCode,
)
from .errors import ToolingError
from .git import GitClient, canonical_remote_identity
from .infrastructure import InfrastructureService
from .lifecycle import LifecycleService
from .path import inspect_console_path
from .process import StructuredProcessRunner
from .repository import RepositoryResolver


def _check(name: str, status: CapabilityStatus, message: str) -> CapabilityCheck:
    return CapabilityCheck(name=name, status=status, message=message)


class DoctorService:
    """Диагностика проекта, Git, среды выполнения и инфраструктуры без изменений."""

    def __init__(
        self,
        resolver: RepositoryResolver | None = None,
        runner: StructuredProcessRunner | None = None,
        infrastructure: InfrastructureService | None = None,
    ) -> None:
        self.resolver = resolver or RepositoryResolver()
        self.runner = runner or self.resolver.runner
        self.infrastructure = infrastructure or InfrastructureService(self.runner)

    def _git_check(self, root: Path, settings) -> tuple[CapabilityStatus, str]:
        try:
            git = GitClient(root, self.runner)
            branch = git.branch()
            git.head()
            tracking = git.upstream()
            if not tracking:
                return CapabilityStatus.FAILED, "Ветка отслеживания Git не настроена."
            if git.active_operation():
                return (
                    CapabilityStatus.FAILED,
                    "В Git обнаружена незавершённая операция.",
                )
            remote_url = git.remote_url(settings.git_remote)
            if not settings.repository_url:
                return (
                    CapabilityStatus.NOT_CONFIGURED,
                    "Каноническая идентичность репозитория не настроена.",
                )
            if canonical_remote_identity(remote_url) != canonical_remote_identity(
                settings.repository_url
            ):
                return (
                    CapabilityStatus.FAILED,
                    "Идентичность Git remote не совпадает с каноническим репозиторием.",
                )
            dirty = bool(git.status_porcelain())
            policy_suffix = (
                f" Политика развертывания ожидает {settings.git_branch}."
                if branch != settings.git_branch
                else ""
            )
            suffix = " Рабочее дерево содержит изменения." if dirty else ""
            return (
                CapabilityStatus.READY,
                f"Корень Git, ветка {branch}, отслеживание {tracking} и идентичность remote подтверждены.{policy_suffix}{suffix}",
            )
        except ToolingError as error:
            return CapabilityStatus.UNAVAILABLE, error.message

    def _runtime_check(self, root: Path) -> tuple[CapabilityStatus, str]:
        try:
            result = LifecycleService(
                resolver=self.resolver,
                runner=self.runner,
                require_infrastructure=False,
            ).inspect(root)
        except ToolingError as error:
            return CapabilityStatus.UNAVAILABLE, error.message
        if result.ok:
            return CapabilityStatus.READY, result.message
        return CapabilityStatus.FAILED, result.message

    def run(
        self, repository_root: str | Path | None = None
    ) -> ToolingResult[DoctorDetails, DoctorEvidence]:
        resolved = self.resolver.resolve(repository_root)
        root = resolved.path
        checks: list[CapabilityCheck] = []
        settings = load_deploy_settings(root)
        checks.append(
            _check("repository", CapabilityStatus.READY, "Корень репозитория подтверждён.")
        )
        checks.append(
            _check(
                "project_markers",
                CapabilityStatus.READY,
                "Маркеры проекта подтверждены.",
            )
        )

        git_status, git_message = self._git_check(root, settings)
        checks.append(_check("git", git_status, git_message))

        version = sys.version_info
        python_ready = (version.major, version.minor, version.micro) >= (
            3,
            14,
            6,
        ) and version < (3, 15)
        checks.append(
            _check(
                "python",
                CapabilityStatus.READY
                if python_ready
                else CapabilityStatus.UNAVAILABLE,
                "Текущий Python соответствует контракту 3.14.6–3.14.x."
                if python_ready
                else "Текущий Python не соответствует контракту >=3.14.6,<3.15.",
            )
        )

        uv_ready = bool(shutil.which("uv") or project_uv(root, settings).is_file())
        checks.append(
            _check(
                "uv",
                CapabilityStatus.READY if uv_ready else CapabilityStatus.UNAVAILABLE,
                "uv доступен для операций проекта." if uv_ready else "uv не найден.",
            )
        )

        project_python_path = project_python(root, settings)
        python_env_status = (
            CapabilityStatus.READY
            if project_python_path.is_file()
            else CapabilityStatus.NOT_CONFIGURED
        )
        checks.append(
            _check(
                "project_environment",
                python_env_status,
                ".venv проекта и Python найдены."
                if project_python_path.is_file()
                else ".venv проекта ещё не подготовлена.",
            )
        )

        config_status = (
            CapabilityStatus.READY
            if settings.source_path
            else CapabilityStatus.NOT_CONFIGURED
        )
        checks.append(
            _check(
                "deploy_config",
                config_status,
                "deploy.yaml найден."
                if settings.source_path
                else "config/deploy.yaml отсутствует; требуется build.",
            )
        )

        runtime_status, runtime_message = self._runtime_check(root)
        checks.append(_check("runtime", runtime_status, runtime_message))

        console_path = inspect_console_path(project_python_path)
        checks.append(
            _check(
                "console_script",
                CapabilityStatus.READY
                if console_path.installed
                else CapabilityStatus.NOT_CONFIGURED,
                "Консольная команда установлена в окружении проекта."
                if console_path.installed
                else console_path.message,
            )
        )
        checks.append(
            _check(
                "console_path",
                console_path.status,
                console_path.message,
            )
        )

        adb = project_adb(root, settings)
        checks.append(
            _check(
                "adb",
                CapabilityStatus.READY
                if adb.is_file()
                else CapabilityStatus.NOT_CONFIGURED,
                "ADB найден."
                if adb.is_file()
                else "ADB не настроен; doctor не выполняет действий с устройством.",
            )
        )
        if shutil.which("docker.exe") or shutil.which("docker"):
            infrastructure = self.infrastructure.inspect(root, settings)
            docker_status = infrastructure.compose
            docker_message = infrastructure.message
        else:
            docker_status = CapabilityStatus.UNAVAILABLE
            docker_message = "Docker CLI не найден в текущей среде."
        checks.append(
            _check(
                "docker",
                docker_status,
                docker_message,
            )
        )

        required_names = {
            "repository",
            "project_markers",
            "python",
            "uv",
            "project_environment",
        }
        healthy = all(
            item.status is CapabilityStatus.READY
            for item in checks
            if item.name in required_names
        )
        warnings: list[ToolingWarning] = []
        if not adb.is_file():
            warnings.append(
                ToolingWarning(
                    code=WarningCode.TOOLING_ADB_NOT_CONFIGURED,
                    message="ADB не настроен.",
                )
            )
        if console_path.status is not CapabilityStatus.READY:
            warnings.append(
                ToolingWarning(
                    code=WarningCode.TOOLING_CLI_NOT_ON_PATH,
                    message=console_path.message,
                )
            )
        if docker_status is not CapabilityStatus.READY:
            warnings.append(
                ToolingWarning(
                    code=WarningCode.TOOLING_POSTGRES_UNAVAILABLE,
                    message="Docker/PostgreSQL не подтверждены; doctor оставил среду без изменений.",
                )
            )
        return ToolingResult[DoctorDetails, DoctorEvidence](
            ok=healthy,
            code=ResultCode.OK if healthy else ResultCode.TOOLING_PRECONDITION_FAILED,
            state=OperationState.READY if healthy else OperationState.NOT_CONFIGURED,
            message="Фундаментальные проверки AzurPilot пройдены."
            if healthy
            else "Фундаментальные проверки AzurPilot требуют подготовки среды.",
            details=DoctorDetails(checks=tuple(checks), healthy=healthy),
            warnings=tuple(warnings),
            evidence=DoctorEvidence(
                repository=resolved.evidence,
                python_version=platform.python_version(),
                platform=sys.platform,
            ),
        )


__all__ = ["DoctorService"]
