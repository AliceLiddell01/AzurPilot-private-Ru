"""Диагностика фундаментальных возможностей проекта без изменений."""

from __future__ import annotations

import platform
import shutil
import sys
from pathlib import Path

from azurpilot.integrations import IntegrationService
from azurpilot.integrations.contracts import IntegrationState

from .config import load_deploy_settings, project_adb, project_python, project_uv
from .contracts import (
    CapabilityCheck,
    CapabilityStatus,
    DoctorDetails,
    DoctorEvidence,
    IntegrationSummary,
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
    return CapabilityCheck(name=name, status=status, message=message[:240])


_INTEGRATION_CAPABILITY_STATUS: dict[IntegrationState, CapabilityStatus] = {
    IntegrationState.READY: CapabilityStatus.READY,
    IntegrationState.NOT_CONFIGURED: CapabilityStatus.NOT_CONFIGURED,
    IntegrationState.UNAUTHENTICATED: CapabilityStatus.UNAVAILABLE,
    IntegrationState.UNAVAILABLE: CapabilityStatus.UNAVAILABLE,
    IntegrationState.INCOMPATIBLE: CapabilityStatus.FAILED,
    IntegrationState.RATE_LIMITED: CapabilityStatus.UNAVAILABLE,
    IntegrationState.DEGRADED: CapabilityStatus.UNKNOWN,
    IntegrationState.UNKNOWN: CapabilityStatus.UNKNOWN,
}


class DoctorService:
    """Диагностика проекта, Git, среды выполнения и инфраструктуры без изменений."""

    def __init__(
        self,
        resolver: RepositoryResolver | None = None,
        runner: StructuredProcessRunner | None = None,
        infrastructure: InfrastructureService | None = None,
        integrations: IntegrationService | None = None,
    ) -> None:
        self.resolver = resolver or RepositoryResolver()
        self.runner = runner or self.resolver.runner
        self.infrastructure = infrastructure or InfrastructureService(self.runner)
        self.integrations = integrations or IntegrationService(resolver=self.resolver)

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
            if error.code in {
                ResultCode.TOOLING_REMOTE_IDENTITY_UNVERIFIED,
                ResultCode.TOOLING_PRECONDITION_FAILED,
            }:
                return CapabilityStatus.FAILED, error.message
            if error.code in {
                ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                ResultCode.TOOLING_CLEANUP_UNKNOWN,
                ResultCode.TOOLING_UNEXPECTED,
            }:
                return CapabilityStatus.UNKNOWN, error.message
            return CapabilityStatus.UNAVAILABLE, error.message

    def _runtime_check(self, root: Path) -> tuple[CapabilityStatus, str]:
        try:
            result = LifecycleService(
                resolver=self.resolver,
                runner=self.runner,
                require_infrastructure=False,
            ).inspect(root)
        except ToolingError as error:
            if error.code in {
                ResultCode.TOOLING_ROLLBACK_UNKNOWN,
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                ResultCode.TOOLING_CLEANUP_UNKNOWN,
                ResultCode.TOOLING_UNEXPECTED,
            }:
                return CapabilityStatus.UNKNOWN, error.message
            return CapabilityStatus.UNAVAILABLE, error.message
        if result.ok and result.state in {OperationState.READY, OperationState.STOPPED}:
            return CapabilityStatus.READY, result.message
        if result.state is OperationState.RUNNING:
            return CapabilityStatus.UNKNOWN, result.message
        if result.state is OperationState.UNKNOWN:
            return CapabilityStatus.UNKNOWN, result.message
        return CapabilityStatus.FAILED, result.message

    def run(
        self,
        repository_root: str | Path | None = None,
        *,
        include_external_integrations: bool = False,
    ) -> ToolingResult[DoctorDetails, DoctorEvidence]:
        resolved = self.resolver.resolve(repository_root)
        root = resolved.path
        checks: list[CapabilityCheck] = []
        settings = load_deploy_settings(root, allow_template=True)
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

        config_path = root / "config" / "deploy.yaml"
        config_status = (
            CapabilityStatus.READY
            if config_path.is_file()
            else CapabilityStatus.NOT_CONFIGURED
        )
        checks.append(
            _check(
                "deploy_config",
                config_status,
                "deploy.yaml найден."
                if config_path.is_file()
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
            redis_status = infrastructure.redis
            redis_message = (
                "Канонический Redis healthy и принадлежит Compose project."
                if redis_status is CapabilityStatus.READY
                else "Канонический Redis не подтверждён; runtime cache недоступен."
            )
            redisinsight_status = infrastructure.redisinsight
            redisinsight_message = (
                "RedisInsight healthy и доступен только на loopback."
                if redisinsight_status is CapabilityStatus.READY
                else "RedisInsight не подтверждён; Redis runtime от этого не зависит."
            )
        else:
            docker_status = CapabilityStatus.UNAVAILABLE
            docker_message = "Docker CLI не найден в текущей среде."
            redis_status = CapabilityStatus.UNAVAILABLE
            redis_message = "Docker CLI не найден; Redis runtime cache не подтверждён."
            redisinsight_status = CapabilityStatus.UNAVAILABLE
            redisinsight_message = "Docker CLI не найден; RedisInsight не подтверждён."
        checks.append(
            _check(
                "docker",
                docker_status,
                docker_message,
            )
        )
        checks.append(_check("redis", redis_status, redis_message))
        checks.append(_check("redisinsight", redisinsight_status, redisinsight_message))

        external_integrations: tuple[IntegrationSummary, ...] = ()
        if include_external_integrations:
            try:
                integration_result = self.integrations.status(root)
                integration_details = integration_result.details
                records = integration_details.integrations
                summaries: list[IntegrationSummary] = []
                for record in records:
                    status_text = record.state.value
                    capability_status = _INTEGRATION_CAPABILITY_STATUS.get(
                        record.state, CapabilityStatus.UNKNOWN
                    )
                    name = record.name.value
                    message = record.message
                    route = record.evidence.route
                    reason_code = record.reason_code
                    summaries.append(
                        IntegrationSummary(
                            name=name,
                            status=status_text,
                            reason_code=reason_code,
                            route=route,
                            message=message[:300],
                        )
                    )
                    checks.append(
                        _check(
                            f"external_{name}",
                            capability_status,
                            message[:240],
                        )
                    )
                external_integrations = tuple(summaries)
            except (ToolingError, OSError, ValueError, TypeError):
                checks.append(
                    _check(
                        "external_integrations",
                        CapabilityStatus.UNKNOWN,
                        "Сводку внешних интеграций не удалось получить.",
                    )
                )
        else:
            checks.append(
                _check(
                    "external_integrations",
                    CapabilityStatus.NOT_CHECKED,
                    "Полная проверка внешних интеграций не выполнялась; используйте azur doctor --full.",
                )
            )

        required_names = {
            "repository",
            "project_markers",
            "git",
            "python",
            "uv",
            "project_environment",
            "runtime",
        }
        required_checks = tuple(item for item in checks if item.name in required_names)
        healthy = all(item.status is CapabilityStatus.READY for item in required_checks)
        if healthy:
            result_code = ResultCode.OK
            result_state = OperationState.READY
            result_message = "Фундаментальные проверки AzurPilot пройдены."
        elif any(item.status is CapabilityStatus.UNKNOWN for item in required_checks):
            result_code = ResultCode.TOOLING_VERIFICATION_UNKNOWN
            result_state = OperationState.UNKNOWN
            result_message = "Фундаментальные проверки AzurPilot не удалось подтвердить."
        elif any(item.status is CapabilityStatus.FAILED for item in required_checks):
            result_code = ResultCode.TOOLING_PRECONDITION_FAILED
            result_state = OperationState.FAILED
            result_message = "Фундаментальные проверки AzurPilot завершились ошибкой."
        elif any(item.status is CapabilityStatus.UNAVAILABLE for item in required_checks):
            result_code = ResultCode.TOOLING_CAPABILITY_UNAVAILABLE
            result_state = OperationState.FAILED
            result_message = "Фундаментальные возможности AzurPilot недоступны."
        elif any(item.status is CapabilityStatus.UNSUPPORTED for item in required_checks):
            result_code = ResultCode.TOOLING_CAPABILITY_UNSUPPORTED
            result_state = OperationState.FAILED
            result_message = "Фундаментальные возможности AzurPilot не поддерживаются."
        else:
            result_code = ResultCode.TOOLING_PRECONDITION_FAILED
            result_state = OperationState.NOT_CONFIGURED
            result_message = "Фундаментальные проверки AzurPilot требуют подготовки среды."
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
        if redis_status is not CapabilityStatus.READY:
            warnings.append(
                ToolingWarning(
                    code=WarningCode.TOOLING_REDIS_UNAVAILABLE,
                    message="Docker/Redis не подтверждены; runtime cache оставлен без fallback.",
                )
            )
        return ToolingResult[DoctorDetails, DoctorEvidence](
            ok=healthy,
            code=result_code,
            state=result_state,
            message=result_message,
            details=DoctorDetails(
                checks=tuple(checks),
                healthy=healthy,
                external_integrations=external_integrations,
            ),
            warnings=tuple(warnings),
            evidence=DoctorEvidence(
                repository=resolved.evidence,
                python_version=platform.python_version(),
                platform=sys.platform,
            ),
        )


__all__ = ["DoctorService"]
