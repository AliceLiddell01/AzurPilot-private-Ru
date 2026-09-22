from __future__ import annotations

import re
from pathlib import Path

import pytest

import dev_tools.integration_contract_gate as gate
from azurpilot.integrations.adapters import DOCKER_HUB_BLOCKED_TOOLS
from tests.support.paths import REPOSITORY_ROOT


def test_permanent_direct_integration_contract_is_ready():
    payload = gate.check(REPOSITORY_ROOT)

    assert payload["ok"] is True
    assert payload["code"] == "INTEGRATION_CONTRACT_READY"
    assert tuple(payload["families"]) == gate.EXPECTED_FAMILIES
    assert all(value == "ready" for value in payload["checks"].values())
    assert payload["errors"] == []


def test_permanent_contract_has_no_retired_profile_paths():
    assert gate.RETIRED_PROFILE_PATHS
    assert all(
        not (REPOSITORY_ROOT / relative).exists()
        for relative in gate.RETIRED_PROFILE_PATHS
    )


def test_contract_rejects_empty_docker_hub_denylist(tmp_path: Path):
    source = (REPOSITORY_ROOT / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert DOCKER_HUB_BLOCKED_TOOLS

    mutated, replacements = re.subn(
        r"(?m)^disabled_tools = \[[^\n]*\]$",
        "disabled_tools = []",
        source,
        count=1,
    )
    assert replacements == 1

    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(mutated, encoding="utf-8")

    payload = gate.check(tmp_path)

    assert payload["checks"]["codex_config"] == "drift"
    assert (
        ".codex/config.toml: dockerhub_direct denylist расходится с adapter contract"
        in payload["errors"]
    )


def test_contract_rejects_raw_grafana_container_registration(tmp_path: Path):
    source = (REPOSITORY_ROOT / ".codex" / "config.toml").read_text(encoding="utf-8")
    launcher = """command = \"uv\"
args = [
    \"run\",
    \"--locked\",
    \"--no-sync\",
    \"python\",
    \"-m\",
    \"azurpilot.integrations.grafana_stdio\",
]"""
    mutated, replacements = re.subn(
        re.escape(launcher),
        'command = "docker"\nargs = ["run", "mcp/grafana"]',
        source,
        count=1,
    )
    assert replacements == 1

    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(mutated, encoding="utf-8")

    payload = gate.check(tmp_path)

    assert payload["checks"]["codex_config"] == "drift"
    assert (
        ".codex/config.toml: grafana_direct обязан указывать "
        "repository-owned stdio launcher"
    ) in payload["errors"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Канонический launcher: UV RUN python -m azurpilot", True),
        ("UV RUN и python -m azurpilot запрещены как обход", False),
        ("Используй azur mcp; uv run разрешён для тестов", False),
    ],
)
def test_operator_launcher_detection_uses_tokens_and_negation(
    text: str, expected: bool
) -> None:
    assert gate._contains_prohibited_operator_launcher(text) is expected
