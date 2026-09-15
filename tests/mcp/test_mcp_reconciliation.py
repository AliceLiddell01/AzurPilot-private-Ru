from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

import azurpilot.tooling.mcp as mcp_tooling
from azurpilot.tooling.errors import ToolingError
from dev_tools.mcp_status import first_party_source_registration
from module.mcp_shared.catalog import tool_catalog_sha256_from_tools
from module.mcp_shared.versioning import (
    VersioningError,
    load_mcp_bundle,
)
from tests.support.paths import REPOSITORY_ROOT


def test_canonical_bundle_is_strict_and_reconciled() -> None:
    bundle = load_mcp_bundle(REPOSITORY_ROOT)

    assert bundle.schema_version == 2
    assert set(bundle.servers) == {"azurpilot-dev", "azurpilot-game"}
    assert bundle.plugin_version
    assert set(bundle.source_digests) == set(mcp_tooling.SOURCE_SET_NAMES)
    assert mcp_tooling.McpSourceReconciler().check(REPOSITORY_ROOT).bundle == bundle
    plugin_manifest = (REPOSITORY_ROOT / mcp_tooling.PLUGIN_MANIFEST_PATH).read_text(
        encoding="utf-8"
    )
    assert "\\u" not in plugin_manifest


def test_legacy_bundle_schema_is_rejected(tmp_path: Path) -> None:
    manifest = tmp_path / "config" / "mcp-versions.toml"
    manifest.parent.mkdir()
    content = (REPOSITORY_ROOT / "config" / "mcp-versions.toml").read_text(
        encoding="utf-8"
    )
    manifest.write_text(content.replace("schema_version = 2", "schema_version = 1", 1))

    with pytest.raises(VersioningError):
        load_mcp_bundle(tmp_path)


def test_source_digest_catalog_is_strict(tmp_path: Path) -> None:
    manifest = tmp_path / "config" / "mcp-versions.toml"
    manifest.parent.mkdir()
    content = (REPOSITORY_ROOT / "config" / "mcp-versions.toml").read_text(
        encoding="utf-8"
    )
    manifest.write_text(
        content.replace("DEV_MCP_SOURCE_SET =", "UNKNOWN_SOURCE_SET =", 1),
        encoding="utf-8",
    )

    with pytest.raises(VersioningError):
        load_mcp_bundle(tmp_path)


def test_source_classification_maps_shared_and_plugin_changes() -> None:
    classification = mcp_tooling.classify_source_changes(
        (
            "module/mcp_shared/local_http.py",
            "plugins/azurpilot/skills/azurpilot-development/SKILL.md",
            "plugins/azurpilot/references/mcp-routing.md",
        )
    )

    assert classification.changed_components == (
        "PLUGIN_BUNDLE_SOURCE_SET",
        "SHARED_MCP_SOURCE_SET",
        "SKILL_BUNDLE_SOURCE_SET",
    )
    assert classification.affected_servers == (
        "azurpilot-dev",
        "azurpilot-game",
    )
    assert classification.plugin_changed is True
    assert classification.skill_changed is True


def test_shared_registration_model_reports_stdio_and_loopback_routes() -> None:
    registration = first_party_source_registration(REPOSITORY_ROOT)

    assert registration["status"] == "ready"
    servers = registration["servers"]
    assert isinstance(servers, dict)
    for name in ("azurpilot-dev", "azurpilot-game"):
        assert servers[name]["source_config"]["status"] == "configured"
        assert servers[name]["local_http_source_config"]["status"] == "configured"


def test_catalog_hash_is_deterministic_and_includes_schema() -> None:
    first = {
        "name": "alpha",
        "description": "read",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
    }
    second = {
        "name": "beta",
        "description": "read",
        "inputSchema": {"type": "object", "properties": {}},
    }
    changed_schema = {
        **first,
        "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}}},
    }

    assert tool_catalog_sha256_from_tools((first, second)) == tool_catalog_sha256_from_tools(
        (second, first)
    )
    assert tool_catalog_sha256_from_tools((first, second)) != tool_catalog_sha256_from_tools(
        (changed_schema, second)
    )


def test_source_digest_is_stable_across_text_checkout_line_endings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        mcp_tooling,
        "SOURCE_SET_PATHS",
        {"DEV_MCP_SOURCE_SET": (Path("source.txt"),)},
    )
    source = tmp_path / "source.txt"
    source.write_bytes(b"first\r\nsecond\r\n")
    crlf_digest = mcp_tooling.source_set_digest(tmp_path, "DEV_MCP_SOURCE_SET")

    source.write_bytes(b"first\nsecond\n")
    lf_digest = mcp_tooling.source_set_digest(tmp_path, "DEV_MCP_SOURCE_SET")

    assert crlf_digest == lf_digest


def test_semver_classifier_requires_explicit_major_for_breaking_change() -> None:
    server = load_mcp_bundle(REPOSITORY_ROOT).servers["azurpilot-game"]
    implementation_only = replace(server, source_set_digest="0" * 64)
    additive = replace(
        server,
        tool_names=server.tool_names + ("game_future_read",),
        tool_descriptor_hashes={
            **server.tool_descriptor_hashes,
            "game_future_read": "a" * 64,
        },
        capability_families=server.capability_families + ("future_read",),
        tool_catalog_sha256="1" * 64,
        capability_catalog_sha256="2" * 64,
        contract_revision="3" * 64,
    )
    breaking = replace(
        server,
        tool_names=("game_renamed",) + server.tool_names[1:],
        tool_catalog_sha256="4" * 64,
        capability_catalog_sha256="5" * 64,
        contract_revision="6" * 64,
    )

    assert mcp_tooling._public_change_kind(server, implementation_only) == "patch"
    assert mcp_tooling._public_change_kind(server, additive) == "minor"
    changed_existing_schema = replace(
        additive,
        tool_descriptor_hashes={
            **additive.tool_descriptor_hashes,
            server.tool_names[0]: "b" * 64,
        },
    )
    assert mcp_tooling._public_change_kind(server, changed_existing_schema) == "major"
    assert mcp_tooling._public_change_kind(server, breaking) == "major"


def test_explicit_bump_can_raise_a_proven_change_without_auto_major_guess() -> None:
    server = load_mcp_bundle(REPOSITORY_ROOT).servers["azurpilot-game"]

    assert mcp_tooling._server_version(server, bump="patch") != server.version
    assert mcp_tooling._server_version(server, bump="minor").endswith(".0")
    assert mcp_tooling._server_version(server, bump="major").startswith("2.")


def test_auth_readiness_is_scoped_to_servers_being_started(monkeypatch) -> None:
    monkeypatch.setenv("AZURPILOT_DEV_LOCAL_MCP_TOKEN", "dev-token")
    monkeypatch.delenv("AZURPILOT_GAME_LOCAL_MCP_TOKEN", raising=False)

    assert mcp_tooling.McpService._auth_ready(("azurpilot-dev",))
    assert not mcp_tooling.McpService._auth_ready()


def test_runtime_source_revision_is_sanitized_before_status_model() -> None:
    server = load_mcp_bundle(REPOSITORY_ROOT).servers["azurpilot-game"]

    status = mcp_tooling._server_status_from_model(
        server,
        status="ready",
        observed_source_revision="not-a-git-revision",
    )

    assert status.source_revision is None


def test_reconciler_detects_unreconciled_source_without_mutating_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_paths = {
        name: (Path(f"{name.lower()}.txt"),)
        for name in mcp_tooling.SOURCE_SET_NAMES
    }
    monkeypatch.setattr(mcp_tooling, "SOURCE_SET_PATHS", source_paths)
    for path in source_paths.values():
        (tmp_path / path[0]).write_text(path[0].stem, encoding="utf-8")

    manifest = tmp_path / "config" / "mcp-versions.toml"
    manifest.parent.mkdir()
    shutil.copy2(REPOSITORY_ROOT / "config" / "mcp-versions.toml", manifest)
    plugin_manifest = tmp_path / mcp_tooling.PLUGIN_MANIFEST_PATH
    plugin_manifest.parent.mkdir(parents=True)
    shutil.copy2(REPOSITORY_ROOT / mcp_tooling.PLUGIN_MANIFEST_PATH, plugin_manifest)

    reconciler = mcp_tooling.McpSourceReconciler()
    reconciler.reconcile(tmp_path)
    assert reconciler.check(tmp_path).changed_components == ()

    (tmp_path / source_paths["SKILL_BUNDLE_SOURCE_SET"][0]).write_text(
        "changed", encoding="utf-8"
    )
    with pytest.raises(ToolingError) as error:
        reconciler.check(tmp_path)
    assert error.value.code.value == "MCP_SOURCE_BUNDLE_DRIFT"
