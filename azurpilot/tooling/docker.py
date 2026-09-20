"""Явное типизированное развёртывание Docker без устаревшего shell-оркестратора."""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.error import URLError
from urllib.request import Request, urlopen

from .config import load_deploy_settings
from .contracts import (
    CapabilityStatus,
    DockerDeploymentDetails,
    DockerDeploymentEvidence,
    OperationState,
    ResultCode,
    ToolingResult,
)
from .errors import ToolingError
from .filesystem import canonical_path, path_has_link
from .process import ProcessSpec, StructuredProcessRunner, docker_environment
from .repository import RepositoryResolver

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MAX_TIMEOUT = 30 * 60
_SOURCE_REQUIRED_PATHS = (
    Path("pyproject.toml"),
    Path("uv.lock"),
    Path("gui.py"),
    Path("deploy/docker/Dockerfile"),
)
_BACKEND_MARKER_PATH = Path("config/state/storage_backend.json")
_COMPOSE_FILE_PATH = Path("infrastructure/observability/compose.yaml")
_COMPOSE_PROJECT = "azurpilot-infrastructure"
_COMPOSE_POSTGRES_SERVICE = "postgres"
_DOCKER_POSTGRES_HOST = "postgres"
_DOCKER_POSTGRES_PORT = 5432
_DOCKER_WEBUI_BIND_HOST = "0.0.0.0"
_DOCKER_RUNTIME_PYTHON = "/app/AzurPilot/.venv/bin/python"
_DOCKER_POSTGRES_HOST_ENV = "AZURPILOT_DOCKER_POSTGRES_HOST"
_DOCKER_POSTGRES_PORT_ENV = "AZURPILOT_DOCKER_POSTGRES_PORT"
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{12,64}$")
_ContainerState = Literal["found", "not_found", "unknown"]


@dataclass(frozen=True, slots=True)
class _DockerPostgresRuntime:
    """Проверенный ephemeral transport для канонического Compose PostgreSQL."""

    network: str
    host: Literal["postgres"] = _DOCKER_POSTGRES_HOST
    port: Literal[5432] = _DOCKER_POSTGRES_PORT


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


class DockerDeploymentService:
    """Жизненный цикл build/run/readiness через argv-only Docker CLI."""

    def __init__(
        self,
        resolver: RepositoryResolver | None = None,
        runner: StructuredProcessRunner | None = None,
    ) -> None:
        self.resolver = resolver or RepositoryResolver()
        self.runner = runner or StructuredProcessRunner()

    @staticmethod
    def _docker() -> Path:
        executable = shutil.which("docker.exe") or shutil.which("docker")
        if executable is None:
            raise ToolingError(
                ResultCode.TOOLING_CAPABILITY_UNAVAILABLE,
                "Docker CLI не найден; установите Docker отдельно и повторите deploy.",
            )
        return Path(executable)

    def _run(
        self,
        docker: Path,
        root: Path,
        *arguments: str,
        timeout_seconds: float,
        allow_nonzero: bool = False,
    ):
        result = self.runner.run(
            ProcessSpec(
                executable=docker,
                argv=tuple(arguments),
                cwd=root,
                timeout_seconds=max(1.0, min(timeout_seconds, _MAX_TIMEOUT)),
                max_output_bytes=128 * 1024,
                env={
                    "PYTHONUTF8": "1",
                    "PYTHONUNBUFFERED": "1",
                    **docker_environment(),
                },
                no_window=True,
            )
        )
        if result.timed_out:
            raise ToolingError(
                ResultCode.TOOLING_TIMEOUT,
                "Операция Docker превысила установленный срок.",
            )
        if not allow_nonzero and result.returncode != 0:
            raise ToolingError(
                ResultCode.TOOLING_INFRASTRUCTURE_FAILED,
                "Операция Docker завершилась ошибкой; предусловия host не изменялись.",
            )
        return result

    @staticmethod
    def _validate_name(value: str, *, container: bool = False) -> str:
        pattern = _CONTAINER_RE if container else _NAME_RE
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise ToolingError(
                ResultCode.TOOLING_INVALID_INVOCATION,
                "Docker образ или контейнер имеет небезопасное имя.",
            )
        return value

    @staticmethod
    def _rollback_container_name(container: str) -> str:
        return f"{container[:100]}.azurpilot-old"

    @staticmethod
    def _validate_source(root: Path, value: str | Path | None) -> Path:
        raw = Path(value) if value is not None else Path(".")
        candidate_input = raw if raw.is_absolute() else root / raw
        if path_has_link(candidate_input):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Исходный каталог Docker содержит symlink или reparse point.",
            )
        candidate = canonical_path(candidate_input)
        if not candidate.is_dir() or not _within(candidate, root):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Исходный каталог Docker должен находиться внутри корня репозитория.",
            )
        return candidate

    @staticmethod
    def _validate_build_context(source: Path) -> None:
        missing = tuple(
            str(relative)
            for relative in _SOURCE_REQUIRED_PATHS
            if not (source / relative).is_file() or path_has_link(source / relative)
        )
        if missing:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Исходный каталог Docker не является полным контекстом сборки проекта.",
            )

    @staticmethod
    def _source_identity(root: Path, source: Path) -> str:
        return "repository_root" if source == root else "nested_build_context"

    @staticmethod
    def _runtime_passfile_path(env_path: Path) -> Path:
        values: list[str] = []
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw_value = line.split("=", 1)
            if key.strip() in {
                "AZURPILOT_POSTGRES_PGPASSFILE",
                "AZURPILOT_POSTGRES_MIGRATOR_PGPASSFILE",
            }:
                value = raw_value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                    value = value[1:-1]
                values.append(value)
        if not values or any(not value or value != values[0] for value in values):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Локальный Docker env не содержит согласованный PGPASSFILE.",
            )
        return Path(values[0])

    @staticmethod
    def _runtime_secret_mount(root: Path) -> tuple[tuple[str, ...], str]:
        """Подготовить источники только для чтения и tmpfs для запуска."""

        env_path = root / ".env"
        marker_path = root / _BACKEND_MARKER_PATH
        if (
            path_has_link(marker_path)
            or not marker_path.is_file()
            or marker_path.stat().st_size > 65_536
        ):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Маркер боевого backend отсутствует или имеет небезопасный путь.",
            )
        mounts = [
            "--mount",
            "type=bind,source="
            + str(canonical_path(marker_path))
            + ",target=/run/secrets/storage_backend.json,readonly",
            "--tmpfs",
            "/run/azurpilot:noexec,nosuid,nodev",
        ]
        if not env_path.exists() and not env_path.is_symlink():
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Локальный Docker env отсутствует; запуск без источника учётных данных PostgreSQL запрещён.",
            )
        if path_has_link(env_path) or not env_path.is_file() or env_path.stat().st_size > 65_536:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Локальный Docker env отсутствует или имеет небезопасный путь.",
            )
        try:
            passfile_path = DockerDeploymentService._runtime_passfile_path(env_path)
        except (OSError, UnicodeError) as exc:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Локальный Docker env невозможно безопасно прочитать.",
            ) from exc
        if not passfile_path.is_absolute():
            passfile_path = root / passfile_path
        if (
            path_has_link(passfile_path)
            or not passfile_path.is_file()
            or passfile_path.stat().st_size > 65_536
        ):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "PGPASSFILE отсутствует или имеет небезопасный путь.",
            )
        mounts.extend(
            (
                "--mount",
                "type=bind,source="
                + str(canonical_path(env_path))
                + ",target=/run/secrets/azurpilot.env,readonly",
                "--mount",
                "type=bind,source="
                + str(canonical_path(passfile_path))
                + ",target=/run/secrets/azurpilot.pgpass,readonly",
            )
        )
        return tuple(mounts), "readonly_env_and_backend_marker"

    @staticmethod
    def _canonical_compose_paths(root: Path) -> tuple[Path, Path]:
        compose_path = root / _COMPOSE_FILE_PATH
        env_path = root / ".env"
        if (
            path_has_link(compose_path)
            or path_has_link(env_path)
            or not compose_path.is_file()
            or not env_path.is_file()
        ):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Канонические Docker Compose и локальный env недоступны.",
            )
        return canonical_path(compose_path), canonical_path(env_path)

    @staticmethod
    def _compose_records(raw: str) -> list[dict[str, object]]:
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
                    item = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ToolingError(
                        ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                        "Docker Compose вернул некорректное состояние PostgreSQL.",
                    ) from exc
                if not isinstance(item, dict):
                    raise ToolingError(
                        ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                        "Docker Compose вернул неоднозначное состояние PostgreSQL.",
                    )
                records.append(item)
            return records
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            return list(value)
        raise ToolingError(
            ResultCode.TOOLING_VERIFICATION_UNKNOWN,
            "Docker Compose вернул неоднозначное состояние PostgreSQL.",
        )

    @staticmethod
    def _decode_object(raw: str, message: str) -> dict[str, object]:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ToolingError(ResultCode.TOOLING_VERIFICATION_UNKNOWN, message) from exc
        if not isinstance(value, dict):
            raise ToolingError(ResultCode.TOOLING_VERIFICATION_UNKNOWN, message)
        return value

    def _resolve_postgres_runtime(
        self, docker: Path, root: Path
    ) -> _DockerPostgresRuntime:
        """Доказать Compose ownership и вернуть только фактическую сеть сервиса."""

        compose_path, env_path = self._canonical_compose_paths(root)
        compose = self._run(
            docker,
            root,
            "compose",
            "--project-name",
            _COMPOSE_PROJECT,
            "--env-file",
            str(env_path),
            "--file",
            str(compose_path),
            "ps",
            "--all",
            "--format",
            "json",
            _COMPOSE_POSTGRES_SERVICE,
            timeout_seconds=60,
            allow_nonzero=True,
        )
        if (
            compose.returncode != 0
            or compose.stdout_truncated
            or compose.stderr_truncated
        ):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Docker Compose не подтвердил состояние канонического PostgreSQL.",
            )
        records = self._compose_records(compose.stdout)
        if len(records) != 1:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Канонический Compose PostgreSQL отсутствует или неоднозначен.",
            )
        record = records[0]
        project = record.get("Project")
        service = record.get("Service")
        container_id = record.get("ID")
        container_name = record.get("Name")
        state = record.get("State")
        health = record.get("Health")
        if not all(
            isinstance(value, str)
            for value in (project, service, container_id, container_name, state, health)
        ):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Docker Compose не вернул доказуемую identity PostgreSQL.",
            )
        if project != _COMPOSE_PROJECT or service != _COMPOSE_POSTGRES_SERVICE:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Compose service PostgreSQL принадлежит другому project или service.",
            )
        if not _CONTAINER_ID_RE.fullmatch(container_id):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Docker Compose вернул некорректный идентификатор PostgreSQL.",
            )
        if not _CONTAINER_RE.fullmatch(container_name.lstrip("/")):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Docker Compose вернул некорректное имя PostgreSQL.",
            )
        if state.casefold() != "running" or health.casefold() != "healthy":
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Канонический PostgreSQL Compose не находится в состоянии healthy.",
            )

        inspect = self._run(
            docker,
            root,
            "inspect",
            "--type",
            "container",
            "--format",
            "{{.Id}}\t{{.Name}}\t{{.State.Status}}\t{{if .State.Health}}{{.State.Health.Status}}{{end}}\t{{json .Config.Labels}}\t{{json .NetworkSettings.Networks}}",
            container_id,
            timeout_seconds=30,
            allow_nonzero=True,
        )
        if (
            inspect.returncode != 0
            or inspect.stdout_truncated
            or inspect.stderr_truncated
        ):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Docker inspect не подтвердил canonical PostgreSQL.",
            )
        fields = inspect.stdout.strip().split("\t")
        if len(fields) != 6:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Docker inspect вернул неполную identity PostgreSQL.",
            )
        inspected_id, inspected_name, inspected_state, inspected_health = fields[:4]
        labels = self._decode_object(
            fields[4], "Docker inspect вернул некорректные labels PostgreSQL."
        )
        networks = self._decode_object(
            fields[5], "Docker inspect вернул некорректные networks PostgreSQL."
        )
        if (
            not _CONTAINER_ID_RE.fullmatch(inspected_id)
            or not (
                inspected_id.startswith(container_id)
                or container_id.startswith(inspected_id)
            )
            or inspected_name.lstrip("/") != container_name.lstrip("/")
            or inspected_state.casefold() != "running"
            or inspected_health.casefold() != "healthy"
        ):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Docker inspect identity PostgreSQL не совпадает с Compose.",
            )
        if (
            labels.get("com.docker.compose.project") != _COMPOSE_PROJECT
            or labels.get("com.docker.compose.service") != _COMPOSE_POSTGRES_SERVICE
        ):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Docker container PostgreSQL принадлежит другому Compose project или service.",
            )
        if len(networks) != 1:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Сеть канонического PostgreSQL отсутствует или неоднозначна.",
            )
        network_name, network_config = next(iter(networks.items()))
        network_aliases = network_config.get("Aliases") if isinstance(network_config, dict) else None
        if (
            not isinstance(network_name, str)
            or not _NAME_RE.fullmatch(network_name)
            or not isinstance(network_config, dict)
            or not isinstance(network_aliases, list)
            or "postgres" not in network_aliases
        ):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Docker inspect вернул небезопасную сеть PostgreSQL.",
            )

        network = self._run(
            docker,
            root,
            "network",
            "inspect",
            "--format",
            "{{.Id}}\t{{.Name}}\t{{json .Labels}}\t{{json .Containers}}",
            network_name,
            timeout_seconds=30,
            allow_nonzero=True,
        )
        if (
            network.returncode != 0
            or network.stdout_truncated
            or network.stderr_truncated
        ):
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Docker inspect не подтвердил сеть canonical PostgreSQL.",
            )
        network_fields = network.stdout.strip().split("\t")
        if len(network_fields) != 4:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Docker inspect вернул неполную сеть PostgreSQL.",
            )
        network_id, inspected_network_name = network_fields[:2]
        network_labels = self._decode_object(
            network_fields[2], "Docker network inspect вернул некорректные labels."
        )
        network_containers = self._decode_object(
            network_fields[3], "Docker network inspect вернул некорректных участников."
        )
        if (
            not _CONTAINER_ID_RE.fullmatch(network_id)
            or inspected_network_name != network_name
            or network_labels.get("com.docker.compose.project") != _COMPOSE_PROJECT
            or not any(
                isinstance(member_id, str)
                and (
                    member_id.startswith(inspected_id)
                    or inspected_id.startswith(member_id)
                )
                for member_id in network_containers
            )
        ):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Сеть PostgreSQL не доказана как сеть канонического Compose project.",
            )
        return _DockerPostgresRuntime(network=network_name)

    @staticmethod
    def _wait_readiness(host: str, port: int, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, min(timeout_seconds, 300.0))
        url = f"http://{host}:{port}/"
        while time.monotonic() < deadline:
            try:
                with urlopen(Request(url, method="GET"), timeout=2.0) as response:
                    if 200 <= int(response.status) < 300:
                        return True
            except (TimeoutError, OSError, URLError):
                time.sleep(0.5)
        return False

    def _remove_container_if_present(
        self, docker: Path, root: Path, container: str
    ) -> None:
        """Идемпотентно удалить только явно названный контейнер и доказать его отсутствие."""

        state = self._container_state(docker, root, container)
        if state == "not_found":
            return
        if state == "unknown":
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Docker не подтвердил состояние контейнера перед очисткой.",
            )
        removed = self._run(
            docker,
            root,
            "rm",
            "--force",
            container,
            timeout_seconds=120,
            allow_nonzero=True,
        )
        if removed.returncode not in {0, 1}:
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Docker не подтвердил удаление нового контейнера.",
            )
        confirmed = self._container_state(docker, root, container)
        if confirmed != "not_found":
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Docker не подтвердил отсутствие нового контейнера.",
            )

    def _container_state(
        self, docker: Path, root: Path, container: str
    ) -> _ContainerState:
        """Различить найденный, отсутствующий и неоднозначный контейнер."""

        inspected = self._run(
            docker,
            root,
            "inspect",
            "--type",
            "container",
            container,
            timeout_seconds=30,
            allow_nonzero=True,
        )
        if inspected.returncode == 0:
            return "found"
        if inspected.returncode != 1:
            return "unknown"
        if inspected.stdout_truncated or inspected.stderr_truncated:
            return "unknown"
        listed = self._run(
            docker,
            root,
            "container",
            "ls",
            "--all",
            "--filter",
            f"name=^{container}$",
            "--format",
            "{{.Names}}",
            timeout_seconds=30,
            allow_nonzero=True,
        )
        if (
            listed.returncode != 0
            or listed.stdout_truncated
            or listed.stderr_truncated
        ):
            return "unknown"
        names = {line.strip() for line in listed.stdout.splitlines() if line.strip()}
        if container in names:
            return "found"
        return "not_found"

    def deploy(
        self,
        repository_root: str | Path | None = None,
        *,
        image: str | None = None,
        container: str | None = None,
        port: int | None = None,
        source: str | Path | None = None,
        replace: bool = False,
        timeout_seconds: float = 20 * 60,
        readiness_timeout_seconds: float = 180.0,
    ) -> ToolingResult[DockerDeploymentDetails, DockerDeploymentEvidence]:
        root = self.resolver.resolve(repository_root).path
        settings = load_deploy_settings(root)
        image_name = self._validate_name(image or "azurpilot-private-ru:local")
        container_name = self._validate_name(container or "azurpilot-private-ru", container=True)
        host_port = port if port is not None else settings.webui_port
        if isinstance(host_port, bool) or not isinstance(host_port, int) or not 1 <= host_port <= 65535:
            raise ToolingError(ResultCode.TOOLING_INVALID_INVOCATION, "Docker host port имеет неверное значение.")
        source_path = self._validate_source(root, source)
        self._validate_build_context(source_path)
        source_identity = self._source_identity(root, source_path)
        docker = self._docker()
        capability = self._run(docker, root, "info", timeout_seconds=30, allow_nonzero=True)
        if capability.returncode != 0:
            raise ToolingError(
                ResultCode.TOOLING_CAPABILITY_UNAVAILABLE,
                "Docker daemon недоступен; установщик host packages не запускается.",
            )
        dockerfile = source_path / "deploy" / "docker" / "Dockerfile"
        if not dockerfile.is_file() or path_has_link(dockerfile):
            raise ToolingError(ResultCode.TOOLING_PRECONDITION_FAILED, "Канонический Dockerfile отсутствует или небезопасен.")
        postgres_runtime = self._resolve_postgres_runtime(docker, root)
        runtime_mount, runtime_secret_mode = self._runtime_secret_mount(root)
        self._run(
            docker,
            root,
            "build",
            "--pull",
            "--tag",
            image_name,
            "--file",
            str(dockerfile),
            str(source_path),
            timeout_seconds=timeout_seconds,
        )
        existing_state = self._container_state(docker, root, container_name)
        if existing_state == "unknown":
            raise ToolingError(
                ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                "Docker не подтвердил состояние существующего контейнера.",
            )
        if existing_state == "found" and not replace:
            raise ToolingError(
                ResultCode.TOOLING_OPERATION_CONFLICT,
                "Контейнер уже существует; повторите с явным --replace.",
            )
        replacement = False
        rollback_name: str | None = None
        existing_running = False
        if existing_state == "found" and replace:
            rollback_name = self._rollback_container_name(container_name)
            rollback_state = self._container_state(docker, root, rollback_name)
            if rollback_state == "found":
                raise ToolingError(
                    ResultCode.TOOLING_OPERATION_CONFLICT,
                    "Для безопасного отката уже существует резервный контейнер.",
                )
            if rollback_state == "unknown":
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Docker не смог доказать отсутствие резервного контейнера.",
                )
            running = self._run(
                docker,
                root,
                "inspect",
                "--format={{.State.Running}}",
                container_name,
                timeout_seconds=30,
            )
            running_state = running.stdout.strip().casefold()
            if running_state not in {"true", "false"}:
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Docker не подтвердил состояние запуска существующего контейнера.",
                )
            existing_running = running_state == "true"
            if existing_running:
                self._run(docker, root, "stop", container_name, timeout_seconds=120)
                stopped = self._run(
                    docker,
                    root,
                    "inspect",
                    "--format={{.State.Running}}",
                    container_name,
                    timeout_seconds=30,
                )
                if stopped.stdout.strip().casefold() != "false":
                    raise ToolingError(
                        ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                        "Docker не подтвердил остановку существующего контейнера.",
                    )
            self._run(
                docker,
                root,
                "rename",
                container_name,
                rollback_name,
                timeout_seconds=120,
            )
            replacement = True
        new_container_attempted = False
        try:
            # Docker daemon может успеть создать контейнер до тайм-аута клиента.
            new_container_attempted = True
            self._run(
                docker,
                root,
                "run",
                "--detach",
                "--name",
                container_name,
                "--restart",
                "unless-stopped",
                "--network",
                postgres_runtime.network,
                "--publish",
                f"127.0.0.1:{host_port}:{settings.webui_port}",
                "--env",
                f"{_DOCKER_POSTGRES_HOST_ENV}={postgres_runtime.host}",
                "--env",
                f"{_DOCKER_POSTGRES_PORT_ENV}={postgres_runtime.port}",
                "--workdir",
                "/app/AzurPilot",
                *runtime_mount,
                "--entrypoint",
                _DOCKER_RUNTIME_PYTHON,
                image_name,
                "deploy/docker/runtime_entrypoint.py",
                "--host",
                _DOCKER_WEBUI_BIND_HOST,
                timeout_seconds=120,
            )
            readiness = self._wait_readiness("127.0.0.1", host_port, readiness_timeout_seconds)
            if not readiness:
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Контейнер Docker запущен, но локальная готовность WebUI не подтверждена.",
                )
        except ToolingError as primary_error:
            try:
                if new_container_attempted:
                    self._remove_container_if_present(docker, root, container_name)
                if rollback_name is not None:
                    self._run(
                        docker,
                        root,
                        "rename",
                        rollback_name,
                        container_name,
                        timeout_seconds=120,
                    )
                    if existing_running:
                        self._run(docker, root, "start", container_name, timeout_seconds=120)
            except ToolingError as restore_error:
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Операция Docker завершилась кодом "
                    f"{primary_error.code.value}, а прежний контейнер не восстановлен.",
                ) from restore_error
            raise
        if rollback_name is not None:
            self._remove_container_if_present(docker, root, rollback_name)
        return ToolingResult(
            ok=True,
            code=ResultCode.OK,
            state=OperationState.READY,
            message="Образ Docker собран, контейнер запущен и локальная готовность подтверждена.",
            details=DockerDeploymentDetails(
                image=image_name,
                container=container_name,
                port=host_port,
                source=source_identity,
                build_confirmed=True,
                container_started=True,
                readiness_confirmed=True,
                replace_performed=replacement,
                runtime_secret_mode=runtime_secret_mode,
                postgres_network=postgres_runtime.network,
            ),
            evidence=DockerDeploymentEvidence(
                docker_cli=docker.name,
                capability=CapabilityStatus.READY,
                image=image_name,
                container=container_name,
                readiness_probe="loopback_http",
                runtime_secret_mode=runtime_secret_mode,
                postgres_network=postgres_runtime.network,
            ),
        )


__all__ = ["DockerDeploymentService"]
