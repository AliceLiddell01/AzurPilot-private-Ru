from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from azurpilot.tooling.contracts import ResultCode
from azurpilot.tooling.docker import DockerDeploymentService
from azurpilot.tooling.errors import ToolingError
from deploy.docker import runtime_entrypoint
from tests.support.contracts import (
    DOCKER_FORBIDDEN_SOURCE_TOKENS,
    assert_source_excludes,
)
from tests.support.paths import REPOSITORY_ROOT


def _process_result(
    *,
    returncode: int | None = 0,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=False,
        stderr_truncated=False,
        timed_out=timed_out,
    )


class _FakeDockerRunner:
    def __init__(self, results: list[SimpleNamespace]) -> None:
        self.results = results
        self.calls: list[tuple[str, ...]] = []

    def run(self, spec) -> SimpleNamespace:
        self.calls.append(spec.argv)
        return self.results.pop(0)


def _prepare_build_context(root: Path) -> None:
    (root / "deploy" / "docker").mkdir(parents=True)
    (root / "deploy" / "docker" / "Dockerfile").write_text(
        "FROM scratch\n", encoding="utf-8"
    )
    for name in ("pyproject.toml", "uv.lock", "gui.py"):
        (root / name).write_text("placeholder\n", encoding="utf-8")
    (root / "config" / "state").mkdir(parents=True)
    (root / "config" / "state" / "storage_backend.json").write_text(
        "{}\n", encoding="utf-8"
    )
    _prepare_runtime_sources(root)


def _prepare_runtime_sources(root: Path) -> None:
    passfile = root / "pgpass.conf"
    passfile.write_text("test-passfile\n", encoding="utf-8")
    (root / ".env").write_text(
        "\n".join(
            (
                "AZURPILOT_POSTGRES_PGPASSFILE=" + str(passfile),
                "AZURPILOT_POSTGRES_MIGRATOR_PGPASSFILE=" + str(passfile),
            )
        )
        + "\n",
        encoding="utf-8",
    )


def test_docker_source_is_confined_to_repository(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ToolingError) as error:
        DockerDeploymentService._validate_source(root, outside)
    assert error.value.code is ResultCode.TOOLING_PRECONDITION_FAILED


def test_docker_source_relative_path_is_resolved_from_repository_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)

    assert DockerDeploymentService._validate_source(root, ".") == root.resolve()


def test_docker_source_requires_full_build_context(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(ToolingError) as error:
        DockerDeploymentService._validate_build_context(root)
    assert error.value.code is ResultCode.TOOLING_PRECONDITION_FAILED


def test_docker_context_excludes_runtime_credentials_and_persistent_proxy():
    dockerignore = (REPOSITORY_ROOT / ".dockerignore").read_text(encoding="utf-8")
    dockerfile = (REPOSITORY_ROOT / "deploy/docker/Dockerfile").read_text(
        encoding="utf-8"
    )
    assert ".env" in dockerignore
    assert "*.pgpass" in dockerignore
    assert "credentials.*" in dockerignore
    assert "config/state/" in dockerignore
    assert "COPY . ." in dockerfile
    assert "ENV http_proxy" not in dockerfile
    assert "ENV UV_INDEX_URL" not in dockerfile
    assert "chmod 600 .env" not in dockerfile


def test_docker_container_state_requires_bounded_fallback_evidence(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    service = DockerDeploymentService(runner=_FakeDockerRunner([]))

    service.runner = _FakeDockerRunner(
        [_process_result(returncode=1), _process_result()]
    )
    assert service._container_state(Path(sys.executable), root, "missing") == "not_found"

    service.runner = _FakeDockerRunner(
        [_process_result(returncode=1), _process_result(returncode=1)]
    )
    assert service._container_state(Path(sys.executable), root, "unknown") == "unknown"

    service.runner = _FakeDockerRunner(
        [_process_result(returncode=2), _process_result()]
    )
    assert service._container_state(Path(sys.executable), root, "infra") == "unknown"

    service.runner = _FakeDockerRunner(
        [
            _process_result(returncode=1, stderr="permission denied"),
            _process_result(returncode=1),
        ]
    )
    assert service._container_state(Path(sys.executable), root, "permission") == "unknown"


def test_docker_runtime_secret_is_mount_only_and_never_evidence(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    secret_value = "runtime-only-test-secret"
    passfile = root / "pgpass.conf"
    passfile.write_text("runtime-only-passfile\n", encoding="utf-8")
    (root / "config" / "state").mkdir(parents=True)
    (root / "config" / "state" / "storage_backend.json").write_text(
        "{}\n", encoding="utf-8"
    )
    (root / ".env").write_text(
        "\n".join(
            (
                f"AZURPILOT_POSTGRES_PASSWORD={secret_value}",
                f"AZURPILOT_POSTGRES_PGPASSFILE={passfile}",
                f"AZURPILOT_POSTGRES_MIGRATOR_PGPASSFILE={passfile}",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    mount, mode = DockerDeploymentService._runtime_secret_mount(root)

    assert mode == "readonly_env_and_backend_marker"
    assert any("readonly" in argument for argument in mount)
    assert any("/run/secrets/storage_backend.json" in argument for argument in mount)
    assert any("/run/azurpilot:noexec" in argument for argument in mount)
    assert secret_value not in " ".join(mount)


def test_docker_runtime_requires_local_env_for_database_credentials(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "config" / "state").mkdir(parents=True)
    (root / "config" / "state" / "storage_backend.json").write_text(
        "{}\n", encoding="utf-8"
    )

    with pytest.raises(ToolingError) as error:
        DockerDeploymentService._runtime_secret_mount(root)

    assert error.value.code is ResultCode.TOOLING_PRECONDITION_FAILED


def test_docker_runtime_entrypoint_stages_bind_sources_without_exposing_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    secret_value = "entrypoint-runtime-secret"
    (source_dir / "env").write_text(
        "\n".join(
            (
                f"AZURPILOT_POSTGRES_PASSWORD={secret_value}",
                "AZURPILOT_POSTGRES_PGPASSFILE=C:/host/pgpass.conf",
                "AZURPILOT_POSTGRES_MIGRATOR_PGPASSFILE=C:/host/pgpass.conf",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (source_dir / "pgpass").write_text("passfile-secret\n", encoding="utf-8")
    (source_dir / "marker").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(runtime_entrypoint, "_ENV_SOURCE", source_dir / "env")
    monkeypatch.setattr(runtime_entrypoint, "_PASSFILE_SOURCE", source_dir / "pgpass")
    monkeypatch.setattr(runtime_entrypoint, "_MARKER_SOURCE", source_dir / "marker")
    monkeypatch.setattr(runtime_entrypoint, "_RUNTIME_DIRECTORY", runtime_dir)
    monkeypatch.setattr(runtime_entrypoint, "_ENV_TARGET", runtime_dir / ".env")
    monkeypatch.setattr(runtime_entrypoint, "_PASSFILE_TARGET", runtime_dir / "pgpass.conf")
    monkeypatch.setattr(
        runtime_entrypoint,
        "_MARKER_TARGET",
        runtime_dir / "storage_backend.json",
    )
    monkeypatch.delenv("AZURPILOT_LOCAL_ENV_PATH", raising=False)
    monkeypatch.delenv("AZURPILOT_BACKEND_MARKER_PATH", raising=False)

    runtime_entrypoint._prepare_runtime_files()

    staged_env = (runtime_dir / ".env").read_text(encoding="utf-8")
    assert f"AZURPILOT_POSTGRES_PASSWORD={secret_value}" in staged_env
    assert "PGPASSFILE=" + str(runtime_dir / "pgpass.conf") in staged_env
    assert (runtime_dir / "pgpass.conf").read_text(encoding="utf-8") == "passfile-secret\n"
    assert os.environ["AZURPILOT_LOCAL_ENV_PATH"] == str(runtime_dir / ".env")
    assert os.environ["AZURPILOT_BACKEND_MARKER_PATH"] == str(
        runtime_dir / "storage_backend.json"
    )
    os.environ.pop("AZURPILOT_LOCAL_ENV_PATH", None)
    os.environ.pop("AZURPILOT_BACKEND_MARKER_PATH", None)


def test_docker_nested_full_context_reports_category_and_resolved_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    root = tmp_path / "repository"
    context = root / "contexts" / "full"
    _prepare_build_context(context)
    _prepare_runtime_sources(root)
    (root / "config" / "state").mkdir(parents=True)
    (root / "config" / "state" / "storage_backend.json").write_text(
        "{}\n", encoding="utf-8"
    )
    runner = _FakeDockerRunner(
        [
            _process_result(),  # проверка Docker daemon
            _process_result(),  # сборка образа
            _process_result(returncode=1),  # inspect: контейнер отсутствует
            _process_result(),  # container ls: контейнер отсутствует
            _process_result(),  # запуск контейнера
        ]
    )
    service = DockerDeploymentService(
        resolver=SimpleNamespace(resolve=lambda _root: SimpleNamespace(path=root)),
        runner=runner,
    )
    monkeypatch.setattr(service, "_docker", lambda: Path(sys.executable))
    monkeypatch.setattr(service, "_wait_readiness", lambda *_args: True)

    result = service.deploy(
        root,
        image="cutover:local",
        container="cutover",
        port=25549,
        source="contexts/full",
    )

    assert result.details.source == "nested_build_context"
    assert result.evidence.docker_cli == Path(sys.executable).name
    build_call = next(call for call in runner.calls if call and call[0] == "build")
    assert str(context.resolve()) == build_call[-1]


@pytest.mark.parametrize("value", ["", "bad name", "-container", "x\\y"])
def test_docker_names_are_bounded(value: str):
    with pytest.raises(ToolingError) as error:
        DockerDeploymentService._validate_name(value, container=True)
    assert error.value.code is ResultCode.TOOLING_INVALID_INVOCATION


def test_docker_service_contains_no_public_ip_or_implicit_installation():
    source = (REPOSITORY_ROOT / "azurpilot" / "tooling" / "docker.py").read_text(
        encoding="utf-8"
    )
    assert_source_excludes(source, DOCKER_FORBIDDEN_SOURCE_TOKENS)
    assert '"rm"' in source  # замена ограничена явно названным контейнером.


def test_docker_deploy_binds_only_loopback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    root = tmp_path / "repository"
    _prepare_build_context(root)
    runner = _FakeDockerRunner(
        [
            _process_result(),  # проверка Docker daemon
            _process_result(),  # сборка образа
            _process_result(returncode=1),  # inspect: контейнер отсутствует
            _process_result(),  # container ls: контейнер отсутствует
            _process_result(),  # запуск контейнера
        ]
    )
    service = DockerDeploymentService(
        resolver=SimpleNamespace(resolve=lambda _root: SimpleNamespace(path=root)),
        runner=runner,
    )
    monkeypatch.setattr(service, "_docker", lambda: Path(sys.executable))
    monkeypatch.setattr(service, "_wait_readiness", lambda *_args: True)

    result = service.deploy(root, image="cutover:local", container="cutover", port=25549)

    assert result.ok is True
    run_call = next(call for call in runner.calls if call and call[0] == "run")
    assert "127.0.0.1:25549:25548" in run_call


def test_docker_deploy_cleans_container_created_before_run_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    root = tmp_path / "repository"
    _prepare_build_context(root)
    runner = _FakeDockerRunner(
        [
            _process_result(),  # проверка Docker daemon
            _process_result(),  # сборка образа
            _process_result(),  # существующий контейнер
            _process_result(returncode=1),  # свободное rollback-имя: inspect
            _process_result(),  # свободное rollback-имя: container ls
            _process_result(stdout="true\n"),  # существующий контейнер запущен
            _process_result(),  # остановка существующего контейнера
            _process_result(stdout="false\n"),  # подтверждение остановки
            _process_result(),  # переименование в rollback-name
            _process_result(timed_out=True),  # run: daemon мог создать контейнер
            _process_result(),  # inspect нового контейнера
            _process_result(),  # удаление нового контейнера
            _process_result(returncode=1),  # отсутствие нового контейнера: inspect
            _process_result(),  # отсутствие нового контейнера: container ls
            _process_result(),  # восстановление rollback-name
            _process_result(),  # запуск восстановленного контейнера
        ]
    )
    service = DockerDeploymentService(
        resolver=SimpleNamespace(resolve=lambda _root: SimpleNamespace(path=root)),
        runner=runner,
    )
    monkeypatch.setattr(service, "_docker", lambda: Path(sys.executable))

    with pytest.raises(ToolingError) as error:
        service.deploy(
            root,
            image="cutover:local",
            container="cutover",
            port=25549,
            replace=True,
        )

    assert error.value.code is ResultCode.TOOLING_TIMEOUT
    assert ("rm", "--force", "cutover") in runner.calls
    assert ("rename", "cutover.azurpilot-old", "cutover") in runner.calls
