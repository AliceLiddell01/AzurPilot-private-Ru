from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from module.dev_mcp.contract import (
    contract_compatibility_issues,
    contract_payload,
)
from module.dev_runtime.smoke import SMOKE_SCHEMA_VERSION, SMOKE_STATE_SCHEMA_VERSION

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_PLUGIN_ROOT = _REPOSITORY_ROOT / "plugins" / "azurpilot"
_MANIFEST_PATH = _PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
_COMPATIBILITY_PATH = _PLUGIN_ROOT / "compatibility.json"
_SKILL_PATH = _PLUGIN_ROOT / "skills" / "azurpilot-development" / "SKILL.md"


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_plugin_manifest_and_generated_marketplace_are_canonical() -> None:
    manifest = _json(_MANIFEST_PATH)
    interface = manifest["interface"]
    assert isinstance(interface, dict)

    assert manifest["name"] == "azurpilot"
    assert manifest["version"] == "0.1.0"
    assert manifest["skills"] == "./skills/"
    assert "apps" not in manifest
    assert "mcpServers" not in manifest
    assert interface["displayName"] == "AzurPilot"
    assert interface["capabilities"] == ["Development"]
    assert isinstance(interface["defaultPrompt"], list)
    assert len(interface["defaultPrompt"]) == 3

    marketplace = _json(_REPOSITORY_ROOT / ".agents" / "plugins" / "marketplace.json")
    assert marketplace["name"] == "personal"
    plugins = marketplace["plugins"]
    assert isinstance(plugins, list)
    assert len(plugins) == 1
    entry = plugins[0]
    assert isinstance(entry, dict)
    assert entry["name"] == "azurpilot"
    assert entry["source"] == {"source": "local", "path": "./plugins/azurpilot"}


def test_plugin_compatibility_matches_runtime_contract() -> None:
    manifest = _json(_MANIFEST_PATH)
    compatibility = _json(_COMPATIBILITY_PATH)
    runtime = contract_payload()

    assert compatibility["product_family"] == runtime["product_family"] == "AzurPilot"
    assert compatibility["plugin_version"] == manifest["version"]
    assert compatibility["dev_mcp_api_version"] == runtime["dev_mcp_api_version"] == 1
    assert compatibility["smoke_spec_schema_version"] == runtime["smoke_spec_schema_version"] == SMOKE_SCHEMA_VERSION
    assert compatibility["smoke_result_schema_version"] == runtime["smoke_result_schema_version"] == SMOKE_STATE_SCHEMA_VERSION
    assert compatibility["profile"] == runtime["profile"] == "ap"
    assert compatibility["required_feature_flags"] == runtime["feature_flags"]
    assert compatibility["required_capability_families"] == runtime["capability_families"]
    assert contract_compatibility_issues(compatibility, runtime) == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dev_mcp_api_version", 0),
        ("smoke_result_schema_version", 2),
    ],
)
def test_incompatible_api_versions_fail_closed(field: str, value: int) -> None:
    compatibility = _json(_COMPATIBILITY_PATH)
    runtime = contract_payload()
    runtime[field] = value

    assert contract_compatibility_issues(compatibility, runtime)


def test_missing_feature_or_capability_fails_closed() -> None:
    compatibility = _json(_COMPATIBILITY_PATH)

    missing_feature = contract_payload()
    del missing_feature["feature_flags"]["universal_smoke_harness"]
    assert "feature_flags.universal_smoke_harness" in contract_compatibility_issues(
        compatibility, missing_feature
    )

    missing_family = contract_payload()
    missing_family["capability_families"] = ["diagnostics", "evidence", "lifecycle"]
    assert "capability_families" in contract_compatibility_issues(compatibility, missing_family)


def test_skill_is_single_development_skill_with_fail_closed_workflow() -> None:
    skill_dirs = [
        path
        for path in (_PLUGIN_ROOT / "skills").iterdir()
        if path.is_dir() and not path.name.startswith(".")
    ]
    assert [path.name for path in skill_dirs] == ["azurpilot-development"]
    assert _SKILL_PATH.is_file()

    skill = _SKILL_PATH.read_text(encoding="utf-8")
    assert skill.startswith("---\nname: azurpilot-development\n")
    for required in (
        "dev_get_contract",
        "PLUGIN_RUNTIME_INCOMPATIBLE",
        "dev_list_smoke_capabilities",
        "dev_validate_smoke",
        "dev_start_smoke",
        "dev_get_smoke",
        "dev_get_smoke_evaluation",
        "dev_submit_smoke_evaluation",
        "PRODUCT_FAILED",
        "HARNESS_FAILED",
        "EVIDENCE_INCOMPLETE",
        "TIMEOUT",
        "INVALIDATED",
        "CANCELLED",
        "PRECONDITION_FAILED",
        "CHATGPT_WRITE_UNAVAILABLE_PRODUCT_LIMITATION",
    ):
        assert required in skill
    assert "capability `Game`" in skill


def test_plugin_sources_contain_no_local_paths_or_credentials() -> None:
    absolute_path = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]|\\\\")
    forbidden_tokens = (
        "Bearer ",
        "CONTROL_PLANE_API_KEY=",
        "config/ap.json",
    )
    for path in _PLUGIN_ROOT.rglob("*"):
        if not path.is_file() or path.name.startswith("."):
            continue
        text = path.read_text(encoding="utf-8")
        assert not absolute_path.search(text), path
        assert not re.search(r"(?i)\b(?:sk|rk|ghp|github_pat|xox[baprs])-[A-Za-z0-9_-]{12,}", text), path
        for token in forbidden_tokens:
            assert token not in text, (path, token)

    assert not (_PLUGIN_ROOT / ".app.json").exists()
    assert not (_PLUGIN_ROOT / ".mcp.json").exists()
