"""Read-only диагностика фундаментальных project-bound capabilities."""

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
from .path import inspect_console_path
from .repository import RepositoryResolver


def _check(name: str, status: CapabilityStatus, message: str) -> CapabilityCheck:
    return CapabilityCheck(name=name, status=status, message=message)


class DoctorService:
    """Диагностика без запуска WebUI, MCP, устройства или Docker."""

    def __init__(self, resolver: RepositoryResolver | None = None) -> None:
        self.resolver = resolver or RepositoryResolver()

    def run(
        self, repository_root: str | Path | None = None
    ) -> ToolingResult[DoctorDetails, DoctorEvidence]:
        resolved = self.resolver.resolve(repository_root)
        root = resolved.path
        checks: list[CapabilityCheck] = []
        settings = load_deploy_settings(root)
        checks.append(
            _check("repository", CapabilityStatus.READY, "Repository root подтверждён.")
        )
        checks.append(
            _check(
                "project_markers",
                CapabilityStatus.READY,
                "Маркеры проекта подтверждены.",
            )
        )

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
                "uv доступен для project operations." if uv_ready else "uv не найден.",
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
                "Project .venv и Python найдены."
                if project_python_path.is_file()
                else "Project .venv ещё не подготовлена.",
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

        console_path = inspect_console_path(project_python_path)
        checks.append(
            _check(
                "console_script",
                CapabilityStatus.READY
                if console_path.installed
                else CapabilityStatus.NOT_CONFIGURED,
                "Console script установлен в project environment."
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
                else "ADB не настроен; doctor не выполняет device action.",
            )
        )
        compose = root / "infrastructure" / "observability" / "compose.yaml"
        env_file = root / ".env"
        docker_ready = bool(
            shutil.which("docker") and compose.is_file() and env_file.is_file()
        )
        checks.append(
            _check(
                "docker",
                CapabilityStatus.READY
                if docker_ready
                else CapabilityStatus.UNAVAILABLE,
                "Docker Compose contract доступен."
                if docker_ready
                else "Docker Compose capability недоступна в текущей среде.",
            )
        )

        required_names = {
            "repository",
            "project_markers",
            "python",
            "uv",
            "project_environment",
            "deploy_config",
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
        if not docker_ready:
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
