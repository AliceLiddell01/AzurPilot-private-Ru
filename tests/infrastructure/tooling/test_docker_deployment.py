from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from azurpilot.tooling.contracts import ResultCode
from azurpilot.tooling.docker import DockerDeploymentService
from azurpilot.tooling.errors import ToolingError
from tests.support.contracts import (
    DOCKER_FORBIDDEN_SOURCE_TOKENS,
    assert_source_excludes,
)
from tests.support.paths import REPOSITORY_ROOT


def _process_result(
    *, returncode: int | None = 0, stdout: str = "", timed_out: bool = False
) -> SimpleNamespace:
    return SimpleNamespace(
        returncode=returncode,
        stdout=stdout,
        stderr="",
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


def test_docker_source_is_confined_to_repository(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ToolingError) as error:
        DockerDeploymentService._validate_source(root, outside)
    assert error.value.code is ResultCode.TOOLING_PRECONDITION_FAILED


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
    assert '"rm"' in source  # замена ограничена явно названным container.


def test_docker_deploy_binds_only_loopback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    root = tmp_path / "repository"
    (root / "deploy" / "docker").mkdir(parents=True)
    (root / "deploy" / "docker" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    runner = _FakeDockerRunner(
        [
            _process_result(),  # docker info
            _process_result(),  # docker build
            _process_result(returncode=1),  # container does not exist
            _process_result(),  # docker run
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
    (root / "deploy" / "docker").mkdir(parents=True)
    (root / "deploy" / "docker" / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    runner = _FakeDockerRunner(
        [
            _process_result(),  # docker info
            _process_result(),  # docker build
            _process_result(),  # existing container
            _process_result(returncode=1),  # rollback name is free
            _process_result(stdout="true\n"),  # existing container is running
            _process_result(),  # stop existing
            _process_result(),  # rename existing to rollback name
            _process_result(timed_out=True),  # run: daemon may have created it
            _process_result(),  # inspect new container
            _process_result(),  # remove new container
            _process_result(returncode=1),  # prove new container is absent
            _process_result(),  # restore rollback name
            _process_result(),  # start restored container
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
