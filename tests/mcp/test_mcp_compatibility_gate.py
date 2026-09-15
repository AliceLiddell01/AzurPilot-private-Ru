from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import dev_tools.mcp_compatibility_gate as gate
import azurpilot.tooling.mcp as mcp_tooling
from azurpilot.tooling.git import GitClient
from azurpilot.tooling.errors import ToolingError
from module.mcp_shared.versioning import load_mcp_bundle
from tests.support.paths import REPOSITORY_ROOT


def _bundle_with_server(bundle, name: str, server):
    servers = dict(bundle.servers)
    servers[name] = server
    return replace(bundle, servers=servers)


def _install_gate_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    head_bundle,
    base_bundle,
    changed_paths: tuple[str, ...],
) -> None:
    monkeypatch.setattr(
        mcp_tooling.McpSourceReconciler,
        "check",
        lambda _self, _root: SimpleNamespace(bundle=head_bundle),
    )
    monkeypatch.setattr(
        mcp_tooling.McpSourceReconciler,
        "_baseline",
        staticmethod(
            lambda _root, _base_commit: mcp_tooling._McpBaseline(
                versions={
                    name: server.version
                    for name, server in base_bundle.servers.items()
                },
                bundle=base_bundle,
            )
        ),
    )
    monkeypatch.setattr(GitClient, "head", lambda _self: "c" * 40)
    monkeypatch.setattr(GitClient, "is_ancestor", lambda _self, _a, _b: True)
    monkeypatch.setattr(
        GitClient,
        "changed_paths",
        lambda _self, _start, _end: changed_paths,
    )


def _run_gate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    head_bundle,
    base_bundle,
    changed_paths: tuple[str, ...],
) -> tuple[int, dict[str, object]]:
    _install_gate_fakes(
        monkeypatch,
        head_bundle=head_bundle,
        base_bundle=base_bundle,
        changed_paths=changed_paths,
    )
    code = gate.main(
        [
            "--repository-root",
            str(REPOSITORY_ROOT),
            "--base-commit",
            "a" * 40,
            "--json",
        ]
    )
    return code, json.loads(capsys.readouterr().out)


def test_gate_requires_explicit_base_commit() -> None:
    with pytest.raises(SystemExit) as error:
        gate._parser().parse_args(["--json"])

    assert error.value.code == 2


def test_gate_rejects_breaking_change_with_patch_bump(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = load_mcp_bundle(REPOSITORY_ROOT)
    game = bundle.servers["azurpilot-game"]
    base_game = replace(
        game,
        tool_names=("game_renamed",) + game.tool_names[1:],
        tool_catalog_sha256="1" * 64,
        capability_catalog_sha256="2" * 64,
        contract_revision="3" * 64,
    )
    code, payload = _run_gate(
        monkeypatch,
        capsys,
        head_bundle=bundle,
        base_bundle=_bundle_with_server(bundle, "azurpilot-game", base_game),
        changed_paths=("module/game_mcp/server.py",),
    )

    assert code != 0
    assert payload["code"] == "MCP_VERSION_BUMP_REQUIRED"


def test_gate_accepts_breaking_change_with_explicit_major(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = load_mcp_bundle(REPOSITORY_ROOT)
    game = bundle.servers["azurpilot-game"]
    base_game = replace(
        game,
        version="1.0.0",
        tool_names=("game_renamed",) + game.tool_names[1:],
        tool_catalog_sha256="1" * 64,
        capability_catalog_sha256="2" * 64,
        contract_revision="3" * 64,
    )
    head_game = replace(game, version="2.0.0")
    head = _bundle_with_server(bundle, "azurpilot-game", head_game)
    base = _bundle_with_server(bundle, "azurpilot-game", base_game)
    code, payload = _run_gate(
        monkeypatch,
        capsys,
        head_bundle=head,
        base_bundle=base,
        changed_paths=("module/game_mcp/server.py",),
    )

    assert code == 0
    assert payload["base_commit"] == "a" * 40


def test_gate_accepts_additive_change_only_with_minor_bump(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = load_mcp_bundle(REPOSITORY_ROOT)
    game = bundle.servers["azurpilot-game"]
    added_name = game.tool_names[-1]
    base_game = replace(
        game,
        version="1.0.0",
        tool_names=game.tool_names[:-1],
        tool_descriptor_hashes={
            name: value
            for name, value in game.tool_descriptor_hashes.items()
            if name != added_name
        },
        tool_catalog_sha256="1" * 64,
        capability_catalog_sha256="2" * 64,
        contract_revision="3" * 64,
    )
    head_game = replace(game, version="1.1.0")
    code, payload = _run_gate(
        monkeypatch,
        capsys,
        head_bundle=_bundle_with_server(bundle, "azurpilot-game", head_game),
        base_bundle=_bundle_with_server(bundle, "azurpilot-game", base_game),
        changed_paths=("module/game_mcp/server.py",),
    )

    assert code == 0
    assert payload["affected_servers"] == ["azurpilot-game"]


def test_gate_rejects_additive_change_without_bump(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = load_mcp_bundle(REPOSITORY_ROOT)
    game = bundle.servers["azurpilot-game"]
    base_game = replace(
        game,
        tool_names=game.tool_names[:-1],
        tool_descriptor_hashes={
            name: value
            for name, value in game.tool_descriptor_hashes.items()
            if name != game.tool_names[-1]
        },
        tool_catalog_sha256="1" * 64,
        capability_catalog_sha256="2" * 64,
        contract_revision="3" * 64,
    )
    code, payload = _run_gate(
        monkeypatch,
        capsys,
        head_bundle=bundle,
        base_bundle=_bundle_with_server(bundle, "azurpilot-game", base_game),
        changed_paths=("module/game_mcp/server.py",),
    )

    assert code != 0
    assert payload["code"] == "MCP_VERSION_BUMP_REQUIRED"


def test_gate_rejects_implementation_change_without_patch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = load_mcp_bundle(REPOSITORY_ROOT)
    dev = bundle.servers["azurpilot-dev"]
    base_dev = replace(dev, source_set_digest="a" * 64)
    code, payload = _run_gate(
        monkeypatch,
        capsys,
        head_bundle=bundle,
        base_bundle=_bundle_with_server(bundle, "azurpilot-dev", base_dev),
        changed_paths=("module/dev_runtime/control.py",),
    )

    assert code != 0
    assert payload["code"] == "MCP_VERSION_BUMP_REQUIRED"


def test_gate_accepts_unchanged_base_and_head(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = load_mcp_bundle(REPOSITORY_ROOT)
    code, payload = _run_gate(
        monkeypatch,
        capsys,
        head_bundle=bundle,
        base_bundle=bundle,
        changed_paths=(),
    )

    assert code == 0
    assert payload["changed_components"] == []
    assert payload["affected_servers"] == []


def test_gate_rejects_stale_current_generated_metadata(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    error = ToolingError(
        mcp_tooling.ResultCode.MCP_SOURCE_BUNDLE_DRIFT,
        "stale generated metadata",
    )

    def fail_current_check(_self, _root):
        raise error

    monkeypatch.setattr(mcp_tooling.McpSourceReconciler, "check", fail_current_check)
    code = gate.main(
        [
            "--repository-root",
            str(REPOSITORY_ROOT),
            "--base-commit",
            "a" * 40,
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code != 0
    assert payload["code"] == "MCP_SOURCE_BUNDLE_DRIFT"
