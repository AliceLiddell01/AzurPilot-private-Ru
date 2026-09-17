from __future__ import annotations

import re
from pathlib import Path

from azurpilot.integrations.adapters import DOCKER_HUB_BLOCKED_TOOLS
from dev_tools.integration_contract_gate import check


def test_integration_contract_gate_rejects_empty_docker_hub_denylist(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    source = (repository_root / ".codex" / "config.toml").read_text(encoding="utf-8")
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

    payload = check(tmp_path)

    assert payload["checks"]["codex_config"] == "drift"
    assert (
        ".codex/config.toml: dockerhub_direct denylist расходится с adapter contract"
        in payload["errors"]
    )
