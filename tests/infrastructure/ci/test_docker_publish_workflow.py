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

    expected_actions = {
        "actions/checkout": "v7",
        "docker/setup-buildx-action": "v4",
        "docker/login-action": "v4",
        "docker/metadata-action": "v6",
        "docker/build-push-action": "v7",
    }
    for action, major in expected_actions.items():
        assert re.search(
            rf"uses:\s*{re.escape(action)}@[0-9a-f]{{40}}\s+#\s+{re.escape(major)}\s*$",
            source,
            re.MULTILINE,
        )

    assert "persist-credentials: false" in source
    assert _all_job_permissions(source) == {
        "build": {
            "contents": "read",
            "packages": "write",
        }
    }
    assert not re.search(r"uses:\s*\S+@v\d", source)
