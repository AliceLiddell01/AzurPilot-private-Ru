from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/docker-publish.yml"


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
    assert "packages: write" in source
    assert "contents: read" in source
    assert "id-token: write" not in source
    assert "@v2" not in source
    assert "@v3" not in source
    assert "@v4" not in source
