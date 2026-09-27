"""Типизированный Python-владелец инфраструктурной предварительной проверки для Start.

Операции Docker выполняются только через явные аргументы Compose и ограниченный
``StructuredProcessRunner``. Учётные данные PostgreSQL остаются в ``.env``/
Docker secrets и не передаются в argv или evidence результата.

При сбое project module оператору сообщается ограниченная очищенная причина: она
позволяет отличить сбой импорта или выполнения от отказа инфраструктуры, не
публикуя сырой вывод процесса и не раскрывая секреты.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, replace
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


SHARED_MCP_PROFILE = "external-mcp"
SHARED_MCP_SERVICES: tuple[str, ...] = ("grafana-mcp", "dockerhub-mcp", "github-mcp")
# Сервисы без shell в image не могут выполнить healthcheck внутри container,
# поэтому их readiness подтверждает repository-owned loopback probe.
SHARED_MCP_EXTERNAL_READINESS: dict[str, tuple[str, int, str]] = {
    "github-mcp": ("127.0.0.1", 8779, "/mcp"),
}
SHARED_MCP_CALLER_TOKEN_ENVIRONMENT_KEYS: tuple[str, ...] = (
    "AZURPILOT_GRAFANA_MCP_CALLER_TOKEN",
    "AZURPILOT_DOCKER_HUB_MCP_CALLER_TOKEN",
)
DOCKERHUB_MCP_IMAGE_TAG_ENVIRONMENT_KEY = "AZURPILOT_DOCKERHUB_MCP_IMAGE_TAG"
DOCKERHUB_MCP_BUILD_LABEL = "azurpilot.dockerhub-mcp.build-tag"
# Все файлы, влияющие на результат сборки Docker Hub MCP из репозитория.
# Compose включён, чтобы изменение build args или runtime label не могло
# незаметно повторно использовать образ с прежним происхождением.
DOCKERHUB_MCP_BUILD_INPUTS: tuple[Path, ...] = (
    Path("infrastructure/observability/compose.yaml"),
    Path("infrastructure/observability/mcp/dockerhub-mcp/Dockerfile"),
    Path("infrastructure/observability/mcp/dockerhub-mcp/.npmrc"),
    Path("infrastructure/observability/mcp/dockerhub-mcp/healthcheck.mjs"),
    Path("infrastructure/observability/mcp/dockerhub-mcp/package.json"),
    Path("infrastructure/observability/mcp/dockerhub-mcp/package-lock.json"),
)


@dataclass(frozen=True)
class SharedMcpOutcome:
    """Состояние общих внешних HTTP-служб MCP одной машины."""

    state: CapabilityStatus
    message: str
    services: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    # `status` подтверждает только running/health/auth. Это поле становится
    # истинным лишь после canonical `start`, который завершил Compose build.
    build_confirmed: bool = False


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
        compose_environment: Mapping[str, str] | None = None,
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
                    **(compose_environment or {}),
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

    @staticmethod
    def _dockerhub_mcp_image_tag(root: Path) -> str:
        """Получить неперсональный тег из точного набора входов сборки."""

        digest = hashlib.sha256()
        for relative in DOCKERHUB_MCP_BUILD_INPUTS:
            path = root / relative
            if path_has_link(path) or not path.is_file():
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    "Вход сборки Docker Hub MCP отсутствует или является ссылкой: "
                    + relative.as_posix(),
                )
            try:
                content = path.read_bytes()
            except OSError as exc:
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    "Не удалось прочитать вход сборки Docker Hub MCP: "
                    + relative.as_posix(),
                ) from exc
            if len(content) > 16 * 1024 * 1024:
                raise ToolingError(
                    ResultCode.TOOLING_PRECONDITION_FAILED,
                    "Вход сборки Docker Hub MCP слишком велик: "
                    + relative.as_posix(),
                )
            digest.update(relative.as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
            digest.update(b"\0")
        return digest.hexdigest()

    @staticmethod
    def _record_has_label(
        record: Mapping[str, object] | None, *, key: str, value: str
    ) -> bool:
        if record is None:
            return False
        labels = record.get("Labels")
        if isinstance(labels, Mapping):
            return labels.get(key) == value
        if isinstance(labels, str):
            return any(item == f"{key}={value}" for item in labels.split(","))
        return False

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

    @staticmethod
    def _missing_caller_tokens(env_file: Path) -> tuple[str, ...]:
        """Вернуть имена незаданных caller tokens общих MCP services.

        Значения не читаются и не публикуются: проверяется только наличие, чтобы
        общий HTTP endpoint никогда не открывался без caller auth.
        """

        configured = {key: False for key in SHARED_MCP_CALLER_TOKEN_ENVIRONMENT_KEYS}
        try:
            for raw_line in bounded_read_text(
                env_file, max_bytes=256 * 1024
            ).splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key in configured and value.strip().strip("'\""):
                    configured[key] = True
        except (OSError, UnicodeError, ToolingError) as exc:
            raise ToolingError(
                ResultCode.TOOLING_INFRASTRUCTURE_FAILED,
                "Не удалось прочитать локальную конфигурацию общих MCP services.",
            ) from exc
        # Docker CLI получает ограниченный набор переменных окружения, поэтому
        # env-файл — единственный источник, из которого Compose соберёт caller token.
        return tuple(sorted(key for key, present in configured.items() if not present))

    @staticmethod
    def _caller_auth_probe(
        target: tuple[str, int, str], *, timeout_seconds: float = 5.0
    ) -> bool:
        """Подтвердить loopback readiness общего сервиса без caller credential.

        Проверка идёт извне container: endpoint обязан слушать loopback и
        отклонять запрос без bearer. Любой ответ без отказа означает fail-open и
        не подтверждает readiness.
        """

        host, port, path = target
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "azurpilot-readiness", "version": "1"},
                },
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"http://{host}:{port}{path}",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds):
                return False
        except urllib.error.HTTPError as error:
            return error.code in {401, 403}
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def shared_mcp_status(
        self, root: Path, *, timeout_seconds: float = 60.0
    ) -> SharedMcpOutcome:
        """Прочитать состояние общих служб MCP, ничего не запуская."""

        compose, env_file = self._paths(root)
        missing = self._missing_caller_tokens(env_file)
        image_tag = self._dockerhub_mcp_image_tag(root)
        raw = self._run_docker(
            root,
            compose,
            env_file,
            "ps",
            "--all",
            "--format",
            "json",
            timeout_seconds=timeout_seconds,
            profiles=(SHARED_MCP_PROFILE,),
            compose_environment={DOCKERHUB_MCP_IMAGE_TAG_ENVIRONMENT_KEY: image_tag},
        )
        records = {
            str(item.get("Service", "")): item for item in self._records(raw)
        }
        running: list[str] = []
        probe_failures: list[str] = []
        provenance_failures: list[str] = []
        for service in SHARED_MCP_SERVICES:
            target = SHARED_MCP_EXTERNAL_READINESS.get(service)
            if target is None:
                record = records.get(service)
                if not self._record_ready(record, require_health=True):
                    continue
                if service == "dockerhub-mcp" and not self._record_has_label(
                    record,
                    key=DOCKERHUB_MCP_BUILD_LABEL,
                    value=image_tag,
                ):
                    provenance_failures.append(
                        "dockerhub-mcp: происхождение образа не подтверждено "
                        "для текущей рабочей копии Compose"
                    )
                    continue
                if record is not None:
                    running.append(service)
                continue
            if not self._record_ready(records.get(service), require_health=False):
                continue
            if self._caller_auth_probe(target):
                running.append(service)
            else:
                probe_failures.append(
                    f"{service}: проверка аутентификации вызывающего клиента "
                    "по loopback не подтверждена на "
                    f"{target[0]}:{target[1]}{target[2]}"
                )
        diagnostics = (
            *((
                f"токен вызывающего клиента не задан: {', '.join(missing)}",
            ) if missing else ()),
            *probe_failures,
            *provenance_failures,
        )
        if len(running) == len(SHARED_MCP_SERVICES):
            return SharedMcpOutcome(
                CapabilityStatus.READY,
                "Общие внешние HTTP-службы MCP запущены и исправны.",
                tuple(running),
                diagnostics,
            )
        if not running:
            return SharedMcpOutcome(
                CapabilityStatus.NOT_CONFIGURED,
                "Общие внешние HTTP-службы MCP не запущены.",
                (),
                diagnostics,
            )
        return SharedMcpOutcome(
            CapabilityStatus.FAILED,
            "Запущена только часть общих служб MCP: "
            + ", ".join(running)
            + ".",
            tuple(running),
            diagnostics,
        )

    def ensure_shared_mcp_started(
        self, root: Path, *, timeout_seconds: float = 600.0
    ) -> SharedMcpOutcome:
        """Собрать и запустить общие службы MCP после проверки токенов клиента.

        Docker Hub MCP принадлежит текущей рабочей копии Compose, поэтому
        `start` обязан выполнить сборку из репозитория до запуска контейнеров.
        Это не позволяет существующему образу с прежними Dockerfile/lockfile
        тихо удовлетворить тот же тег upstream.
        """

        compose, env_file = self._paths(root)
        deadline = time.monotonic() + timeout_seconds

        def budget(limit: float) -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ToolingError(
                    ResultCode.TOOLING_TIMEOUT,
                    "Запуск общих служб MCP превысил общий срок.",
                )
            return min(limit, remaining)

        missing = self._missing_caller_tokens(env_file)
        if missing:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Токены вызывающего клиента общих HTTP-служб MCP не заданы: "
                + ", ".join(missing)
                + ".",
            )
        image_tag = self._dockerhub_mcp_image_tag(root)
        compose_environment = {
            DOCKERHUB_MCP_IMAGE_TAG_ENVIRONMENT_KEY: image_tag
        }
        self._run_docker(
            root,
            compose,
            env_file,
            "config",
            "--quiet",
            timeout_seconds=budget(60.0),
            profiles=(SHARED_MCP_PROFILE,),
            compose_environment=compose_environment,
        )
        self._run_docker(
            root,
            compose,
            env_file,
            "build",
            "--pull",
            "dockerhub-mcp",
            timeout_seconds=budget(timeout_seconds),
            profiles=(SHARED_MCP_PROFILE,),
            compose_environment=compose_environment,
        )
        self._run_docker(
            root,
            compose,
            env_file,
            "up",
            "--detach",
            "--wait",
            "dockerhub-mcp",
            timeout_seconds=budget(timeout_seconds),
            profiles=(SHARED_MCP_PROFILE,),
            compose_environment=compose_environment,
        )
        # Остальные services не менялись: Compose должен сохранить их
        # существующие контейнеры и только подтвердить их readiness.
        self._run_docker(
            root,
            compose,
            env_file,
            "up",
            "--detach",
            "--wait",
            "grafana-mcp",
            "github-mcp",
            timeout_seconds=budget(timeout_seconds),
            profiles=(SHARED_MCP_PROFILE,),
            compose_environment=compose_environment,
        )
        status = self.shared_mcp_status(root, timeout_seconds=budget(60.0))
        return replace(
            status,
            build_confirmed=status.state is CapabilityStatus.READY,
            message=(
                "Общие внешние HTTP-службы MCP собраны из текущей рабочей "
                "копии Compose, запущены и исправны."
                if status.state is CapabilityStatus.READY
                else status.message
            ),
        )

    def stop_shared_mcp(
        self, root: Path, *, timeout_seconds: float = 180.0
    ) -> SharedMcpOutcome:
        """Остановить общие MCP services, сохранив их состояние."""

        compose, env_file = self._paths(root)
        self._run_docker(
            root,
            compose,
            env_file,
            "stop",
            *SHARED_MCP_SERVICES,
            timeout_seconds=timeout_seconds,
            profiles=(SHARED_MCP_PROFILE,),
        )
        return self.shared_mcp_status(root)

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
    "DOCKERHUB_MCP_BUILD_INPUTS",
    "DOCKERHUB_MCP_BUILD_LABEL",
    "DOCKERHUB_MCP_IMAGE_TAG_ENVIRONMENT_KEY",
    "SHARED_MCP_PROFILE",
    "SHARED_MCP_SERVICES",
    "InfrastructureInspection",
    "InfrastructureOutcome",
    "InfrastructureService",
    "SharedMcpOutcome",
]
