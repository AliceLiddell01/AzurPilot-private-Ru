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
_APP_MANIFEST_PATH = _PLUGIN_ROOT / ".app.json"
_COMPATIBILITY_PATH = _PLUGIN_ROOT / "compatibility.json"
_SKILL_PATH = _PLUGIN_ROOT / "skills" / "azurpilot-development" / "SKILL.md"
_GAME_SKILL_PATH = _PLUGIN_ROOT / "skills" / "azurpilot-game-control" / "SKILL.md"
_TROUBLESHOOTING_SKILL_PATH = (
    _PLUGIN_ROOT / "skills" / "azurpilot-troubleshooting" / "SKILL.md"
)
_TROUBLESHOOTING_MATRIX_PATH = (
    _PLUGIN_ROOT
    / "skills"
    / "azurpilot-troubleshooting"
    / "references"
    / "diagnostic-matrix.md"
)
_ABSOLUTE_LOCAL_PATH = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\|(?<![A-Za-z0-9/:.`])/(?!/))")
_URL = re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s`]+")
_SAFE_CONTAINER_PATH = re.compile(r"(?<![A-Za-z0-9])(/etc/caddy/Caddyfile)(?![A-Za-z0-9])")


def _find_absolute_local_path(value: str) -> re.Match[str] | None:
    value_without_urls = _URL.sub("", value)
    return _ABSOLUTE_LOCAL_PATH.search(_SAFE_CONTAINER_PATH.sub("", value_without_urls))


def _markdown_subsection(content: str, heading: str) -> str:
    match = re.search(
        rf"^### {re.escape(heading)}\n(?P<section>.*?)(?=^### |^## |\Z)",
        content,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, heading
    return match.group("section")


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
    assert manifest["apps"] == "./.app.json"
    assert "mcpServers" not in manifest
    assert interface["displayName"] == "AzurPilot"
    assert isinstance(interface["capabilities"], list)
    assert {"Development", "Game", "Diagnostics"} <= set(interface["capabilities"])
    assert isinstance(interface["defaultPrompt"], list)
    assert interface["defaultPrompt"]
    assert all(isinstance(prompt, str) and prompt for prompt in interface["defaultPrompt"])
    assert len(interface["defaultPrompt"]) <= 3
    assert all(len(prompt) <= 128 for prompt in interface["defaultPrompt"])
    assert "Game capability в этом пакете отсутствует" not in interface["longDescription"]

    app_manifest = _json(_APP_MANIFEST_PATH)
    apps = app_manifest["apps"]
    assert isinstance(apps, dict)
    assert set(apps) == {"azurpilot-development-verified", "azurpilot-game"}
    app_ids = [app["id"] for app in apps.values() if isinstance(app, dict)]
    assert len(app_ids) == 2
    assert all(
        isinstance(app_id, str) and re.fullmatch(r"asdk_app_[a-z0-9]+", app_id)
        for app_id in app_ids
    )
    assert len(set(app_ids)) == len(app_ids)

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
    assert {path.name for path in skill_dirs} == {
        "azurpilot-development",
        "azurpilot-game-control",
        "azurpilot-troubleshooting",
    }
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
    assert "azurpilot-game-control" in skill
    assert "azurpilot-troubleshooting" in skill


def test_game_and_troubleshooting_skills_have_distinct_fail_closed_routes() -> None:
    game_skill = _GAME_SKILL_PATH.read_text(encoding="utf-8")
    troubleshooting_skill = _TROUBLESHOOTING_SKILL_PATH.read_text(encoding="utf-8")
    troubleshooting_matrix = _TROUBLESHOOTING_MATRIX_PATH.read_text(encoding="utf-8")

    assert game_skill.startswith("---\nname: azurpilot-game-control\n")
    assert troubleshooting_skill.startswith("---\nname: azurpilot-troubleshooting\n")
    for required in (
        "AzurPilot Game",
        "game_get_contract",
        "game_list_profiles",
        "game_list_tasks",
        "game_get_task_help",
        "game_get_profile_status",
        "game_trigger_task",
        "game_update_config",
        "game_restart_emulator",
        "game_restart_adb",
        "game_restart_runtime",
        "game_login_runtime",
        "automatic",
        "postcondition",
        "STOP WRITES",
    ):
        assert required in game_skill
    for required in (
        "AzurPilot Development Verified",
        "AzurPilot Game",
        "dev_get_contract",
        "game_get_contract",
        "tool_count",
        "tool_catalog_sha256",
        "PER_SESSION_CALLABLE_SNAPSHOT_DRIFT",
        "STOP WRITES",
        "postcondition",
        "fork",
        "Reconnect",
        "chat/task",
        "НЕ изменять backend/source code ради появления tool в этой session",
        "client/plugin/session refresh layer",
        "browser/UI automation",
        "AX/DOM",
        "unavailable",
        "transcript",
        "tool marker",
        "machine-readable result",
        "новый browser tab",
        "same-directory fork",
        "azurpilot-game-control",
        "azurpilot-development",
    ):
        assert required in troubleshooting_skill
    for required in (
        "`game_get_profile_status` никогда не является доказательством",
        "ровно один game_restart_runtime(<profile>)",
        "ровно один game_login_runtime(<profile>)",
        "emulator ready",
        "ADB ready",
        "game running",
        "game foreground",
        "login/main-ready",
    ):
        assert required in game_skill

    game_control_match = re.search(
        r"^## Control и lifecycle\n(?P<section>.*?)(?=^## |\Z)",
        game_skill,
        re.MULTILINE | re.DOTALL,
    )
    assert game_control_match is not None
    game_control_section = re.sub(r"\s+", " ", game_control_match.group("section"))
    for required in (
        "game_get_contract",
        "любой Game mutation",
        "обязательная предварительная проверка",
        "STOP WRITES",
        "azurpilot-troubleshooting",
        "scopes",
        "preconditions",
        "game_trigger_task",
        "generated task из catalog",
        "обратимость подтверждена contract",
        "необратимым игровым эффектом",
        "расходованием ресурсов",
    ):
        assert required in game_control_section

    combined = f"{game_skill}\n{troubleshooting_skill}"
    assert not re.search(r"\b[a-f0-9]{64}\b", combined, re.IGNORECASE)
    assert not re.search(r"tool_count\s*=\s*\d+", combined)
    assert not re.search(r"profile\s*[=:]\s*[\"']alas[\"']", combined)
    assert not re.search(r"game_trigger_task\([^\n]*Login", combined, re.IGNORECASE)
    assert (_GAME_SKILL_PATH.parent / "references" / "architecture.md").is_file()
    assert _TROUBLESHOOTING_MATRIX_PATH.is_file()

    contract_section = _markdown_subsection(
        troubleshooting_skill, "Подтверждение contract перед возвратом к mutation"
    )
    for required in (
        "dev_get_contract",
        "compatibility.json",
        "PLUGIN_RUNTIME_INCOMPATIBLE",
        "game_get_contract",
        "backend contract unavailable",
        "capability gap не доказан",
        "callable surface",
        "STOP WRITES",
    ):
        assert required in contract_section

    scenario_c = _markdown_subsection(
        troubleshooting_skill, "Сценарий C: Ошибка exit/postcondition"
    )
    for required in (
        "product state",
        "authoritative product-postcondition failure",
        "STOP WRITES",
        "read-only recovery",
        "Last Confirmed State",
        "automatic retry запрещён",
    ):
        assert required in scenario_c

    browser_section = _markdown_subsection(
        troubleshooting_skill, "Browser automation и fallback через Computer Use"
    )
    for required in (
        "browser automation = unavailable",
        "Computer Use",
        "уже открытое активное окно браузера",
        "мышь и клавиатуру",
        "authoritative read-only verification",
        "Game MCP",
        "game_restart_runtime",
        "game_login_runtime",
        "retry loop не создавать",
    ):
        assert required in browser_section

    matrix_scenario_c = next(
        line
        for line in troubleshooting_matrix.splitlines()
        if "| C. Exit/postcondition |" in line
    )
    for required in (
        "STOP WRITES",
        "read-only recovery",
        "Last Confirmed State",
        "automatic retry запрещён",
        "нового подтверждённого решения",
    ):
        assert required in matrix_scenario_c

    matrix_browser = next(
        line
        for line in troubleshooting_matrix.splitlines()
        if "| H. Browser refresh unavailable |" in line
    )
    for required in (
        "Computer Use",
        "authoritative read-only verification",
        "без retry loop",
        "без обхода Game/Dev MCP",
    ):
        assert required in matrix_browser


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

    assert (_PLUGIN_ROOT / ".app.json").is_file()
    assert not (_PLUGIN_ROOT / ".mcp.json").exists()
