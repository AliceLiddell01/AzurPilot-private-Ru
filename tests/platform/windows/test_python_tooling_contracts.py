from __future__ import annotations

from azurpilot.cli import build_parser
from azurpilot.tooling.docker import DockerDeploymentService
from tests.support.contracts import (
    DOCKER_FORBIDDEN_SOURCE_TOKENS,
    assert_no_legacy_operator_surfaces,
    assert_source_excludes,
)
from tests.support.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT


def test_windows_operator_surface_is_python_cli_owned():
    parser = build_parser()
    for command in ("build", "repair", "update", "start", "stop"):
        parsed = parser.parse_args([command])
        assert parsed.command == command
    docker = parser.parse_args(["deploy", "docker", "--replace"])
    assert docker.command == "deploy"
    assert docker.deploy_command == "docker"
    assert docker.replace is True


def test_project_has_no_tracked_legacy_operator_surfaces():
    assert_no_legacy_operator_surfaces(ROOT)


def test_docker_service_has_no_host_package_install_path():
    source = (ROOT / "azurpilot" / "tooling" / "docker.py").read_text(encoding="utf-8")
    assert_source_excludes(source, DOCKER_FORBIDDEN_SOURCE_TOKENS)
    assert "dnf install" not in source
    assert "sudo " not in source
    assert callable(DockerDeploymentService.deploy)
