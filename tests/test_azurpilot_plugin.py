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
_ABSOLUTE_LOCAL_PATH = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\|(?<![A-Za-z0-9/:.`])/(?!/))")
_URL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s`]+")


def _find_absolute_local_path(value: str) -> re.Match[str] | None:
    return _ABSOLUTE_LOCAL_PATH.search(_URL.sub("", value))


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_plugin_manifest_and_generated_marketplace_are_canonical() -> None:
    manifest = _json(_MANIFEST_PATH)
    interface = manifest["interface"]
    assert isinstance(interface, dict)

    assert manifest["name"] == "azurpilot"
    version = manifest["version"]
    assert isinstance(version, str)
    assert re.fullmatch(r"0\.1\.0\+codex\.\d{14}", version)
    assert manifest["skills"] == "./skills/"
    assert "apps" not in manifest
    assert "mcpServers" not in manifest
    assert interface["displayName"] == "AzurPilot"
    assert isinstance(interface["capabilities"], list)
    assert "Development" in interface["capabilities"]
    assert isinstance(interface["defaultPrompt"], list)
    assert interface["defaultPrompt"]
    assert all(isinstance(prompt, str) and prompt for prompt in interface["defaultPrompt"])

    marketplace = _json(_REPOSITORY_ROOT / ".agents" / "plugins" / "marketplace.json")
    assert marketplace["name"] == "personal"
    plugins = marketplace["plugins"]
    assert isinstance(plugins, list)
    entry = next(
        (item for item in plugins if isinstance(item, dict) and item.get("name") == "azurpilot"),
        None,
    )
    assert isinstance(entry, dict)
    assert entry["name"] == "azurpilot"
    assert entry["source"] == {"source": "local", "path": "./plugins/azurpilot"}


def test_plugin_compatibility_matches_runtime_contract() -> None:
    manifest = _json(_MANIFEST_PATH)
    compatibility = _json(_COMPATIBILITY_PATH)
    runtime = contract_payload()

    assert compatibility["product_family"] == runtime["product_family"] == "AzurPilot"
    assert compatibility["plugin_version"] == manifest["version"]
    assert compatibility["dev_mcp_api_version"] == runtime["dev_mcp_api_version"] == 2
    assert compatibility["smoke_spec_schema_version"] == runtime["smoke_spec_schema_version"] == SMOKE_SCHEMA_VERSION
    assert compatibility["smoke_result_schema_version"] == runtime["smoke_result_schema_version"] == SMOKE_STATE_SCHEMA_VERSION
    assert "profile" not in compatibility
    assert "profile" not in runtime
    assert set(compatibility["required_feature_flags"]).issubset(runtime["feature_flags"])
    assert set(compatibility["required_capability_families"]).issubset(runtime["capability_families"])
    assert set(compatibility["result_outcomes"]).issubset(runtime["result_outcomes"])
    assert contract_compatibility_issues(compatibility, runtime) == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("contract_schema_version", 2),
        ("product_family", "OtherProduct"),
        ("dev_mcp_api_version", 0),
        ("smoke_spec_schema_version", 2),
        ("smoke_result_schema_version", 2),
    ],
)
def test_incompatible_contract_values_fail_closed(field: str, value: object) -> None:
    compatibility = _json(_COMPATIBILITY_PATH)
    runtime = contract_payload()
    runtime[field] = value

    assert contract_compatibility_issues(compatibility, runtime)


def test_missing_required_contract_values_fail_closed() -> None:
    compatibility = _json(_COMPATIBILITY_PATH)

    missing_feature = contract_payload()
    del missing_feature["feature_flags"]["universal_smoke_harness"]
    assert "feature_flags.universal_smoke_harness" in contract_compatibility_issues(
        compatibility, missing_feature
    )

    missing_family = contract_payload()
    missing_family["capability_families"] = ["diagnostics", "evidence", "lifecycle"]
    assert "capability_families" in contract_compatibility_issues(compatibility, missing_family)

    missing_outcome = contract_payload()
    missing_outcome["result_outcomes"].remove("CANCELLED")
    assert "result_outcomes" in contract_compatibility_issues(compatibility, missing_outcome)


def test_compatibility_allows_additive_runtime_contract_values() -> None:
    compatibility = _json(_COMPATIBILITY_PATH)
    runtime = contract_payload()
    runtime["feature_flags"]["future_flag"] = True
    runtime["capability_families"].append("future")
    runtime["result_outcomes"].append("FUTURE")

    assert contract_compatibility_issues(compatibility, runtime) == ()


def test_required_development_skill_has_fail_closed_workflow() -> None:
    skill_dirs = [
        path
        for path in (_PLUGIN_ROOT / "skills").iterdir()
        if path.is_dir() and not path.name.startswith(".")
    ]
    assert _SKILL_PATH.parent in skill_dirs
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
        "dev_get_runtime_status",
        "dev_start_game",
        "dev_stop_game",
        "dev_restart_game",
        "dev_start_emulator",
        "dev_stop_emulator",
        "dev_restart_emulator",
        "dev_restart_adb",
        "dev_get_control_operation",
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


@pytest.mark.parametrize(
    "path",
    (
        "/root/.config/azurpilot",
        "/var/lib/azurpilot",
        "/tmp/azurpilot.sock",
        "/etc/azurpilot/config",
        r"C:\\AzurPilot\\config",
        r"\\server\share\azurpilot",
    ),
)
def test_absolute_local_path_pattern_detects_local_paths(path: str) -> None:
    assert _find_absolute_local_path(path)


@pytest.mark.parametrize(
    "url",
    (
        "https://example.invalid/mcp",
        "http://localhost:8765/mcp",
        "file:///tmp/azurpilot.sock",
    ),
)
def test_absolute_local_path_pattern_ignores_urls(url: str) -> None:
    assert _find_absolute_local_path(url) is None


def test_plugin_sources_contain_no_local_paths_or_credentials() -> None:
    forbidden_tokens = (
        "Bearer ",
        "CONTROL_PLANE_API_KEY=",
        "config/ap.json",
    )
    for path in _PLUGIN_ROOT.rglob("*"):
        if not path.is_file():
            continue
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # Сохраняем видимость ASCII-паттернов секретов даже в бинарных и не-UTF-8 файлах.
            text = raw.decode("latin-1")
        assert not _find_absolute_local_path(text), path
        assert not re.search(
            r"(?i)(?:\b(?:sk|rk|xox[baprs])-[A-Za-z0-9_-]{12,}|"
            r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{12,})",
            text,
        ), path
        for token in forbidden_tokens:
            assert token not in text, (path, token)

    assert not (_PLUGIN_ROOT / ".app.json").exists()
    assert not (_PLUGIN_ROOT / ".mcp.json").exists()
