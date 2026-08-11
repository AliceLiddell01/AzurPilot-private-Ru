from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/docker-publish.yml"


def _all_job_permissions(source: str) -> dict[str, dict[str, str] | None]:
    lines = source.splitlines()
    jobs_start = lines.index("jobs:") + 1
    jobs: dict[str, dict[str, str] | None] = {}
    current_job: str | None = None
    index = jobs_start

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        if stripped and indent == 2 and stripped.endswith(":"):
            current_job = stripped[:-1]
            jobs[current_job] = None
        elif current_job is not None and line == "    permissions:":
            permissions: dict[str, str] = {}
            index += 1
            while index < len(lines):
                permission_line = lines[index]
                if not permission_line.strip():
                    index += 1
                    continue
                permission_indent = len(permission_line) - len(permission_line.lstrip())
                if permission_indent <= 4:
                    index -= 1
                    break
                key, value = permission_line.strip().split(":", 1)
                permissions[key] = value.strip()
                index += 1
            jobs[current_job] = permissions

        index += 1

    return jobs


def test_docker_publish_uses_pinned_node24_actions_and_minimal_permissions():
    source = WORKFLOW.read_text(encoding="utf-8")

    expected_actions = (
        "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803",  # v6
        "docker/setup-buildx-action@bb05f3f5519dd87d3ba754cc423b652a5edd6d2c",  # v4
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
    assert "@v2" not in source
    assert "@v3" not in source
    assert "@v4" not in source
