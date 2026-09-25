"""Типизированный Python-владелец инфраструктурной предварительной проверки для Start.

Операции Docker выполняются только через явные аргументы Compose и ограниченный
``StructuredProcessRunner``. Учётные данные PostgreSQL остаются в ``.env``/
Docker secrets и не передаются в argv или evidence результата.

При сбое project module оператору сообщается ограниченная очищенная причина: она
позволяет отличить сбой импорта или выполнения от отказа инфраструктуры, не
публикуя сырой вывод процесса и не раскрывая секреты.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .config import DeploySettings, project_python
from .contracts import CapabilityStatus, ResultCode
from .errors import ToolingError
from .filesystem import bounded_read_text, path_has_link
from .process import ProcessSpec, StructuredProcessRunner, docker_environment

_PROJECT_MODULE_CAUSE_LIMIT = 160
_PROJECT_MODULE_CAUSE_UNAVAILABLE = "причина недоступна"
_PROJECT_MODULE_FAILURE_SUMMARY = re.compile(
    r"^[A-Za-z_][\w.]*(?:Error|Exception|Warning|Interrupt)\b"
)


def _project_module_failure_line(value: object) -> str:
    """Выбрать информативную строку ограниченного вывода процесса.

    Для сбоя project module важнее строка-итог интерпретатора
    (``ModuleNotFoundError: ...``), чем произвольный последующий лог. Если такой
    строки нет, используется последняя непустая строка.
    """

    if not isinstance(value, str):
        return ""
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    for line in reversed(lines):
        if _PROJECT_MODULE_FAILURE_SUMMARY.match(line):
            return line
    return lines[-1] if lines else ""


def _project_module_failure_cause(result: object) -> str:
    """Вернуть ограниченную очищенную причину сбоя project module.

    Сырой stderr/stdout не публикуется: причина проходит через общий очиститель
    диагностики, который скрывает учётные данные и локальные пути и ограничивает
    размер. Если очиститель недоступен, сообщается только о недоступности причины.
    """

    cause = _project_module_failure_line(
        getattr(result, "stderr", "")
    ) or _project_module_failure_line(getattr(result, "stdout", ""))
    if not cause:
        return ""
    try:
        from module.dev_runtime.sanitizer import redact_text
    except Exception:  # pragma: no cover - очиститель есть в любой установке проекта
        return _PROJECT_MODULE_CAUSE_UNAVAILABLE
    return redact_text(cause, max_length=_PROJECT_MODULE_CAUSE_LIMIT).strip()


def _project_module_failure_suffix(result: object) -> str:
    """Собрать безопасный диагностический суффикс для сообщения о сбое модуля."""

    details: list[str] = []
    returncode = getattr(result, "returncode", None)
    if isinstance(returncode, int):
        details.append(f"код возврата {returncode}")
    cause = _project_module_failure_cause(result)
    if cause:
        details.append(f"причина: {cause}")
    if not details:
        return ""
    return f" ({'; '.join(details)})"


@dataclass(frozen=True)
class InfrastructureOutcome:
    postgres: CapabilityStatus
    caddy: CapabilityStatus
    migration: str
    redis: CapabilityStatus = CapabilityStatus.NOT_CONFIGURED
    redisinsight: CapabilityStatus = CapabilityStatus.NOT_CONFIGURED


@dataclass(frozen=True)
class InfrastructureInspection:
    postgres: CapabilityStatus
    caddy: CapabilityStatus
    compose: CapabilityStatus
    message: str
    redis: CapabilityStatus = CapabilityStatus.NOT_CONFIGURED
    redisinsight: CapabilityStatus = CapabilityStatus.NOT_CONFIGURED


class InfrastructureService:
    """Запуск и диагностика текущего контракта Docker Compose/PostgreSQL."""

    def __init__(self, runner: StructuredProcessRunner | None = None) -> None:
        self.runner = runner or StructuredProcessRunner()

    @staticmethod
    def _paths(root: Path) -> tuple[Path, Path]:
        compose = root / "infrastructure" / "observability" / "compose.yaml"
        env_file = root / ".env"
        if (
            path_has_link(compose)
            or path_has_link(env_file)
            or not compose.is_file()
            or not env_file.is_file()
        ):
            raise ToolingError(
                ResultCode.TOOLING_CAPABILITY_UNAVAILABLE,
                "Канонические Docker Compose и .env недоступны.",
            )
        return compose, env_file

    @staticmethod
    def _endpoint_configured(env_file: Path) -> bool:
        """Проверить необязательную политику Caddy без чтения лишних значений env."""

        values: dict[str, list[str]] = {
            "AZURPILOT_CADDY_HOST": [],
            "AZURPILOT_GAME_MCP_PUBLIC_HOST": [],
        }
        try:
            for raw_line in bounded_read_text(env_file, max_bytes=256 * 1024).splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key in values:
                    values[key].append(value.strip().strip("'\""))
        except (OSError, UnicodeError, ToolingError) as exc:
            raise ToolingError(
                ResultCode.TOOLING_INFRASTRUCTURE_FAILED,
                "Не удалось прочитать локальную необязательную конфигурацию Caddy.",
            ) from exc
        caddy = values["AZURPILOT_CADDY_HOST"]
        game = values["AZURPILOT_GAME_MCP_PUBLIC_HOST"]
        if len(caddy) > 1 or len(game) > 1:
            raise ToolingError(
                ResultCode.TOOLING_INFRASTRUCTURE_FAILED,
                "Локальный .env содержит дублирующую конфигурацию Caddy.",
            )
        if not caddy:
            return False
        if not caddy[0] or len(game) != 1 or not game[0]:
            raise ToolingError(
                ResultCode.TOOLING_INFRASTRUCTURE_FAILED,
                "Для Caddy нужны по одному непустому public host.",
            )
        return True

    @staticmethod
    def _docker() -> Path:
        executable = shutil.which("docker.exe") or shutil.which("docker")
        if executable is None:
            raise ToolingError(
                ResultCode.TOOLING_CAPABILITY_UNAVAILABLE,
                "Docker CLI не найден; инфраструктуру PostgreSQL и Redis нельзя проверить.",
            )
        return Path(executable)

    def _run_docker(
        self,
        root: Path,
        compose: Path,
        env_file: Path,
        *arguments: str,
        timeout_seconds: float,
        profiles: tuple[str, ...] = (),
    ) -> str:
        docker = self._docker()
        profile_arguments = tuple(
            item for profile in profiles for item in ("--profile", profile)
        )
        result = self.runner.run(
            ProcessSpec(
                executable=docker,
                argv=(
                    "compose",
                    "--env-file",
                    str(env_file),
                    "--file",
                    str(compose),
                    *profile_arguments,
                    *arguments,
                ),
                cwd=root,
                timeout_seconds=max(1.0, timeout_seconds),
                max_output_bytes=128 * 1024,
                env={
                    "PYTHONUTF8": "1",
                    "PYTHONUNBUFFERED": "1",
                    **docker_environment(),
                },
                no_window=True,
            )
        )
        if not result.ok:
            if result.timed_out:
                raise ToolingError(
                    ResultCode.TOOLING_TIMEOUT,
                    "Операция Docker Compose превысила установленный срок.",
                )
            raise ToolingError(
                ResultCode.TOOLING_INFRASTRUCTURE_FAILED,
                "Операция Docker Compose завершилась ошибкой; проверьте Docker Desktop и .env.",
            )
        return result.stdout

    def _run_project_module(
        self, root: Path, settings: DeploySettings, module: str, *arguments: str,
        timeout_seconds: float,
    ) -> str:
        python = project_python(root, settings)
        if not python.is_file():
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Python проекта отсутствует; сначала выполните Build.",
            )
        result = self.runner.run(
            ProcessSpec(
                executable=python,
                argv=("-X", "utf8", "-m", module, *arguments),
                cwd=root,
                timeout_seconds=max(1.0, timeout_seconds),
                max_output_bytes=128 * 1024,
                env={"PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"},
                no_window=True,
            )
        )
        if not result.ok:
            if result.timed_out:
                raise ToolingError(
                    ResultCode.TOOLING_TIMEOUT,
                    f"{module} превысил установленный срок.",
                )
            raise ToolingError(
                ResultCode.TOOLING_INFRASTRUCTURE_FAILED,
                f"{module} не подтвердил инфраструктурное постусловие"
                f"{_project_module_failure_suffix(result)}.",
            )
        return result.stdout

    @staticmethod
    def _records(raw: str) -> list[dict[str, object]]:
        if not raw.strip():
            return []
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            records: list[dict[str, object]] = []
            for line in raw.splitlines():
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ToolingError(
                        ResultCode.TOOLING_INFRASTRUCTURE_FAILED,
                        "Вывод Docker Compose не является корректным JSON.",
                    ) from error
                if isinstance(value, dict):
                    records.append(value)
            return records
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _record_ready(
        record: dict[str, object] | None, *, require_health: bool
    ) -> bool:
        if record is None or str(record.get("State", "")).casefold() not in {
            "running",
            "up",
        }:
            return False
        health = str(record.get("Health", "")).casefold()
        return health == "healthy" if require_health else health in {"", "healthy"}

    def ensure_started(
        self,
        root: Path,
        settings: DeploySettings,
        *,
        timeout_seconds: float = 300.0,
    ) -> InfrastructureOutcome:
        compose, env_file = self._paths(root)
        caddy_configured = self._endpoint_configured(env_file)
        deadline = time.monotonic() + timeout_seconds

        def budget(limit: float) -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ToolingError(
                    ResultCode.TOOLING_TIMEOUT,
                    "Подготовка инфраструктуры превысила общий срок.",
                )
            return min(limit, remaining)

        if not caddy_configured:
            self._run_docker(
                root,
                compose,
                env_file,
                "--profile",
                "remote-ingress",
                "stop",
                "caddy",
                timeout_seconds=budget(60.0),
            )

        # Это существующая Python-граница миграции observability: она сохраняет
        # постоянные тома и не передаёт оркестрацию PowerShell.
        self._run_project_module(
            root,
            settings,
            "dev_tools.observability_compose_migration",
            "--repository-root",
            str(root),
            "migrate",
            timeout_seconds=budget(390.0),
        )
        self._run_docker(
            root,
            compose,
            env_file,
            "config",
            "--quiet",
            timeout_seconds=budget(60.0),
        )
        self._run_docker(
            root,
            compose,
            env_file,
            "up",
            "--detach",
            "--wait",
            "postgres",
            timeout_seconds=budget(240.0),
        )
        self._run_docker(
            root,
            compose,
            env_file,
            "up",
            "--detach",
            "--wait",
            "redis",
            timeout_seconds=budget(240.0),
        )
        self._run_docker(
            root,
            compose,
            env_file,
            "run",
            "--rm",
            "--no-deps",
            "postgres-bootstrap",
            timeout_seconds=budget(240.0),
            profiles=("bootstrap",),
        )
        self._run_project_module(
            root,
            settings,
            "dev_tools.postgresql_runtime",
            "prepare",
            timeout_seconds=budget(240.0),
        )
        caddy_status = CapabilityStatus.NOT_CONFIGURED
        if caddy_configured:
            self._run_docker(
                root,
                compose,
                env_file,
                "--profile",
                "remote-ingress",
                "config",
                "--quiet",
                timeout_seconds=budget(60.0),
            )
            self._run_docker(
                root,
                compose,
                env_file,
                "--profile",
                "remote-ingress",
                "up",
                "--detach",
                "--wait",
                "caddy",
                timeout_seconds=budget(240.0),
            )
            caddy_status = CapabilityStatus.READY
        raw = self._run_docker(
            root,
            compose,
            env_file,
            "ps",
            "--all",
            "--format",
            "json",
            timeout_seconds=budget(60.0),
        )
        records = self._records(raw)
        redisinsight = next(
            (item for item in records if item.get("Service") == "redisinsight"),
            None,
        )
        redisinsight_status = (
            CapabilityStatus.READY
            if self._record_ready(redisinsight, require_health=True)
            else CapabilityStatus.UNAVAILABLE
        )
        return InfrastructureOutcome(
            postgres=CapabilityStatus.READY,
            caddy=caddy_status,
            migration="canonical_compose_verified",
            redis=CapabilityStatus.READY,
            redisinsight=redisinsight_status,
        )

    def inspect(
        self, root: Path, settings: DeploySettings
    ) -> InfrastructureInspection:
        try:
            compose, env_file = self._paths(root)
            self._run_docker(
                root,
                compose,
                env_file,
                "config",
                "--quiet",
                timeout_seconds=60.0,
            )
            raw = self._run_docker(
                root,
                compose,
                env_file,
                "ps",
                "--all",
                "--format",
                "json",
                timeout_seconds=60.0,
            )
            records = self._records(raw)
            postgres = next(
                (item for item in records if item.get("Service") == "postgres"),
                None,
            )
            postgres_ready = self._record_ready(postgres, require_health=False)
            caddy_configured = self._endpoint_configured(env_file)
            caddy = next(
                (item for item in records if item.get("Service") == "caddy"),
                None,
            )
            caddy_ready = self._record_ready(caddy, require_health=False)
            redis = next(
                (item for item in records if item.get("Service") == "redis"),
                None,
            )
            redis_ready = self._record_ready(redis, require_health=True)
            redisinsight = next(
                (item for item in records if item.get("Service") == "redisinsight"),
                None,
            )
            redisinsight_ready = self._record_ready(
                redisinsight, require_health=True
            )
            return InfrastructureInspection(
                postgres=CapabilityStatus.READY
                if postgres_ready
                else CapabilityStatus.UNAVAILABLE,
                caddy=(
                    CapabilityStatus.READY
                    if caddy_ready
                    else CapabilityStatus.UNAVAILABLE
                    if caddy_configured
                    else CapabilityStatus.NOT_CONFIGURED
                ),
                compose=CapabilityStatus.READY,
                message="Конфигурация Docker Compose и состояние служб прочитаны.",
                redis=(
                    CapabilityStatus.READY
                    if redis_ready
                    else CapabilityStatus.UNAVAILABLE
                ),
                redisinsight=(
                    CapabilityStatus.READY
                    if redisinsight_ready
                    else CapabilityStatus.UNAVAILABLE
                ),
            )
        except ToolingError as error:
            return InfrastructureInspection(
                postgres=CapabilityStatus.UNAVAILABLE,
                caddy=CapabilityStatus.UNAVAILABLE,
                compose=CapabilityStatus.FAILED,
                message=error.message,
                redis=CapabilityStatus.UNAVAILABLE,
                redisinsight=CapabilityStatus.UNAVAILABLE,
            )


__all__ = [
    "InfrastructureInspection",
    "InfrastructureOutcome",
    "InfrastructureService",
]
