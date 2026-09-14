import re
from pathlib import Path

import yaml

from tests.support.paths import REPOSITORY_ROOT

ROOT = REPOSITORY_ROOT
WORKFLOW = ROOT / ".github/workflows/docker-publish.yml"


def _all_job_permissions(source: str) -> dict[str, dict[str, str] | None]:
    workflow = yaml.safe_load(source)
    return {
        name: job.get("permissions")
        for name, job in workflow["jobs"].items()
    }


def test_docker_publish_uses_pinned_node24_actions_and_minimal_permissions():
    source = WORKFLOW.read_text(encoding="utf-8")

    expected_actions = (
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",  # v7
        "docker/setup-buildx-action@37fe631027851001ddb9b187196cc803df7f5f0e",  # v4
        "docker/login-action@dbcb813823bdd20940b903addbd779551569679f",  # v4
        "docker/metadata-action@dc802804100637a589fabce1cb79ff13a1411302",  # v6
        "docker/build-push-action@53b7df96c91f9c12dcc8a07bcb9ccacbed38856a",  # v7
    )
    for action in expected_actions:
        assert action in source

    assert "persist-credentials: false" in source
    assert _all_job_permissions(source) == {
        "build": {
            "contents": "read",
            "packages": "write",
        }
    }
    assert not re.search(r"uses:\s*\S+@v\d", source)
