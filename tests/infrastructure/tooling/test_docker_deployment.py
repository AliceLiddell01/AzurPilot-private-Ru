from __future__ import annotations

from pathlib import Path

import pytest

from azurpilot.tooling.contracts import ResultCode
from azurpilot.tooling.docker import DockerDeploymentService
from azurpilot.tooling.errors import ToolingError


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
    source = Path("azurpilot/tooling/docker.py").read_text(encoding="utf-8")
    assert "apt-get" not in source
    assert "download.docker.com" not in source
    assert "ifconfig.me" not in source
    assert '"rm"' in source  # replacement is explicit and scoped to one named container.
