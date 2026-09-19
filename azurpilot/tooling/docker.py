"""Явное типизированное развёртывание Docker без устаревшего shell-оркестратора."""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
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
        candidate = canonical_path(Path(value) if value is not None else root)
        if not candidate.is_dir() or path_has_link(candidate) or not _within(candidate, root):
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                "Каталог source должен существовать внутри repository root.",
            )
        return candidate

    @staticmethod
    def _wait_readiness(host: str, port: int, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, min(timeout_seconds, 300.0))
        url = f"http://{host}:{port}/"
        while time.monotonic() < deadline:
            try:
                with urlopen(Request(url, method="GET"), timeout=2.0) as response:
                    if 200 <= int(response.status) < 500:
                        return True
            except (TimeoutError, OSError, URLError):
                time.sleep(0.5)
        return False

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
        readiness_timeout_seconds: float = 30.0,
    ) -> ToolingResult[DockerDeploymentDetails, DockerDeploymentEvidence]:
        root = self.resolver.resolve(repository_root).path
        settings = load_deploy_settings(root)
        image_name = self._validate_name(image or "azurpilot-private-ru:local")
        container_name = self._validate_name(container or "azurpilot-private-ru", container=True)
        host_port = port if port is not None else settings.webui_port
        if isinstance(host_port, bool) or not isinstance(host_port, int) or not 1 <= host_port <= 65535:
            raise ToolingError(ResultCode.TOOLING_INVALID_INVOCATION, "Docker host port имеет неверное значение.")
        source_path = self._validate_source(root, source)
        docker = self._docker()
        capability = self._run(docker, root, "info", timeout_seconds=30, allow_nonzero=True)
        if capability.returncode != 0:
            raise ToolingError(
                ResultCode.TOOLING_CAPABILITY_UNAVAILABLE,
                "Docker daemon недоступен; установщик host packages не запускается.",
            )
        dockerfile = root / "deploy" / "docker" / "Dockerfile"
        if not dockerfile.is_file() or path_has_link(dockerfile):
            raise ToolingError(ResultCode.TOOLING_PRECONDITION_FAILED, "Канонический Dockerfile отсутствует или небезопасен.")
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
        existing = self._run(
            docker,
            root,
            "inspect",
            "--type",
            "container",
            container_name,
            timeout_seconds=30,
            allow_nonzero=True,
        )
        if existing.returncode == 0 and not replace:
            raise ToolingError(
                ResultCode.TOOLING_OPERATION_CONFLICT,
                "Контейнер уже существует; повторите с явным --replace.",
            )
        replacement = False
        rollback_name: str | None = None
        existing_running = False
        if existing.returncode == 0 and replace:
            rollback_name = self._rollback_container_name(container_name)
            rollback_existing = self._run(
                docker,
                root,
                "inspect",
                "--type",
                "container",
                rollback_name,
                timeout_seconds=30,
                allow_nonzero=True,
            )
            if rollback_existing.returncode == 0:
                raise ToolingError(
                    ResultCode.TOOLING_OPERATION_CONFLICT,
                    "Для безопасного Docker rollback уже существует резервный container.",
                )
            if rollback_existing.returncode != 1:
                raise ToolingError(
                    ResultCode.TOOLING_INFRASTRUCTURE_FAILED,
                    "Docker не смог проверить резервный container.",
                )
            running = self._run(
                docker,
                root,
                "inspect",
                "--format={{.State.Running}}",
                container_name,
                timeout_seconds=30,
            )
            existing_running = running.stdout.strip().casefold() == "true"
            if existing_running:
                self._run(docker, root, "stop", container_name, timeout_seconds=120)
            self._run(
                docker,
                root,
                "rename",
                container_name,
                rollback_name,
                timeout_seconds=120,
            )
            replacement = True
        elif existing.returncode not in {0, 1}:
            raise ToolingError(
                ResultCode.TOOLING_INFRASTRUCTURE_FAILED,
                "Docker не смог проверить существующий container.",
            )
        new_container_attempted = False
        try:
            self._run(
                docker,
                root,
                "run",
                "--detach",
                "--name",
                container_name,
                "--restart",
                "unless-stopped",
                "--publish",
                f"{host_port}:{host_port}",
                "--workdir",
                "/app/AzurPilot",
                image_name,
                timeout_seconds=120,
            )
            new_container_attempted = True
            readiness = self._wait_readiness("127.0.0.1", host_port, readiness_timeout_seconds)
            if not readiness:
                raise ToolingError(
                    ResultCode.TOOLING_VERIFICATION_UNKNOWN,
                    "Docker container запущен, но локальная WebUI readiness не подтверждена.",
                )
        except ToolingError:
            try:
                if new_container_attempted:
                    self._run(docker, root, "rm", "--force", container_name, timeout_seconds=120)
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
                    "Новый Docker container не прошёл readiness, а прежний container не восстановлен.",
                ) from restore_error
            raise
        if rollback_name is not None:
            self._run(docker, root, "rm", "--force", rollback_name, timeout_seconds=120)
        return ToolingResult(
            ok=True,
            code=ResultCode.OK,
            state=OperationState.READY,
            message="Docker image собран, container запущен и локальная readiness подтверждена.",
            details=DockerDeploymentDetails(
                image=image_name,
                container=container_name,
                port=host_port,
                source="repository",
                build_confirmed=True,
                container_started=True,
                readiness_confirmed=True,
                replace_performed=replacement,
            ),
            evidence=DockerDeploymentEvidence(
                docker_cli="docker",
                capability=CapabilityStatus.READY,
                image=image_name,
                container=container_name,
                readiness_probe="loopback_http",
            ),
        )


__all__ = ["DockerDeploymentService"]
