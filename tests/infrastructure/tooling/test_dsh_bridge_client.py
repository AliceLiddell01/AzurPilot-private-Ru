from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import azurpilot.tooling.dsh as dsh_tooling
from azurpilot.cli import build_parser
from azurpilot.dsh import (
    AZUR_EXECUTABLE_ENV_VAR,
    BRIDGE_GUARD_PATH,
    BRIDGE_OVERLAY_PATH,
    CHECKOUT_ROOT_ENV_VAR,
    DSH_PACKAGE_ENV_VAR,
    DSH_PROFILE_ENV_VAR,
    IDENTITY_ENV_VARS,
    SOURCE_REVISION_ENV_VAR,
)
from azurpilot.tooling.contracts import (
    DshBridgeFamilyCheck,
    DshBridgeFamilyRecord,
    DshBridgeGeneration,
    DshVerificationDetails,
)
from azurpilot.tooling.dsh import (
    DEFAULT_DSH_PACKAGE,
    DEFAULT_DSH_PROFILE,
    DshBridgeService,
    load_generation,
)
from azurpilot.tooling.errors import ToolingError
from azurpilot.tooling.result import ResultCode
from module.mcp_shared.windows_mcp_bridge_contract import (
    BRIDGE_CALLER_TOKEN_ENV_VAR,
    BRIDGE_IDENTITY_PROTOCOL,
    BRIDGE_ROUTES,
    BridgeSourceIdentity,
    bridge_endpoint,
    serialize_identity,
)

REVISION = "1" * 40
OTHER_REVISION = "2" * 40


class _JavascriptLoader(yaml.SafeLoader):
    """Загрузчик overlay-файла: выражение `!!js` читается как обычная строка."""


_JavascriptLoader.add_constructor(
    "tag:yaml.org,2002:js",
    lambda loader, node: f"!!js {loader.construct_scalar(node)}",
)


def _identity(
    server_name: str,
    *,
    revision: str = REVISION,
    digest_character: str = "a",
) -> BridgeSourceIdentity:
    return BridgeSourceIdentity(
        identity_protocol=BRIDGE_IDENTITY_PROTOCOL,
        server_name=server_name,
        server_version="1.0.0",
        source_revision=revision,
        source_set_digest=digest_character * 64,
        contract_revision="c" * 64,
        tool_catalog_sha256="d" * 64,
        capability_catalog_sha256="e" * 64,
    )


def _generation(
    *,
    revision: str = REVISION,
    dev_digest: str = "a",
    game_digest: str = "b",
) -> DshBridgeGeneration:
    routes = {route.server_name: route for route in BRIDGE_ROUTES.values()}
    return DshBridgeGeneration(
        checkout_root="/tmp/azurpilot-checkout",
        source_revision=revision,
        source_state="clean",
        profile=DEFAULT_DSH_PROFILE,
        dsh_package=DEFAULT_DSH_PACKAGE,
        overlay_path=str(BRIDGE_OVERLAY_PATH),
        guard_path=str(BRIDGE_GUARD_PATH),
        bridge_endpoint=bridge_endpoint(),
        families=(
            DshBridgeFamilyRecord(
                server_name="azurpilot-dev",
                endpoint=routes["azurpilot-dev"].bridge_url,
                identity=_identity("azurpilot-dev", revision=revision, digest_character=dev_digest),
            ),
            DshBridgeFamilyRecord(
                server_name="azurpilot-game",
                endpoint=routes["azurpilot-game"].bridge_url,
                identity=_identity("azurpilot-game", revision=revision, digest_character=game_digest),
            ),
        ),
    )


class _FakeResolver:
    """Подменить разрешение корня репозитория без обращения к реальному git."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def resolve(self, repository_root: str | Path | None) -> SimpleNamespace:
        return SimpleNamespace(path=self.root)


def _patch_boundary(
    monkeypatch: pytest.MonkeyPatch,
    *,
    root: Path,
    revision: str,
    dev_digest: str,
    game_digest: str,
) -> DshBridgeService:
    live = {
        "azurpilot-dev": _identity(
            "azurpilot-dev", revision=revision, digest_character=dev_digest
        ),
        "azurpilot-game": _identity(
            "azurpilot-game", revision=revision, digest_character=game_digest
        ),
    }
    monkeypatch.setattr(dsh_tooling, "RepositoryResolver", lambda: _FakeResolver(root))
    monkeypatch.setattr(
        dsh_tooling, "source_snapshot", lambda resolved: (revision, "clean")
    )
    monkeypatch.setattr(dsh_tooling, "load_bridge_bundle", lambda resolved: object())
    monkeypatch.setattr(
        dsh_tooling,
        "McpSourceReconciler",
        lambda: SimpleNamespace(
            build=lambda resolved, requested_bump=None: SimpleNamespace(
                bundle=object()
            )
        ),
    )
    monkeypatch.setattr(
        dsh_tooling,
        "expected_bridge_identity",
        lambda bundle, server_name, current: live[server_name],
    )
    return DshBridgeService()


def test_cli_exposes_dsh_prepare_launch_and_verify_routes() -> None:
    parser = build_parser()

    prepare = parser.parse_args(["dsh", "prepare", "--json"])
    assert prepare.dsh_command == "prepare"
    assert prepare.profile == DEFAULT_DSH_PROFILE
    assert prepare.package == DEFAULT_DSH_PACKAGE

    launch = parser.parse_args(
        ["dsh", "launch", "--dsh-arg", "headless", "--dsh-arg", "задача"]
    )
    assert launch.dsh_arguments == ["headless", "задача"]
    assert not hasattr(launch, "generation")

    verify = parser.parse_args(["dsh", "verify", "--generation", "/tmp/ent.json"])
    assert verify.generation == "/tmp/ent.json"


def test_tracked_overlay_registers_exactly_two_bridge_routes() -> None:
    text = BRIDGE_OVERLAY_PATH.read_text(encoding="utf-8")
    overlay = yaml.load(text, Loader=_JavascriptLoader)
    rows = [row for entry in overlay for row in (entry.get("insert") or [])]
    clients = [
        row for row in rows if row.get("name") == "@deepseek-ai/dsh-mcp-client"
    ]
    routes = {route.server_name: route for route in BRIDGE_ROUTES.values()}

    assert len(rows) == 3
    assert len(clients) == 2
    assert rows[0]["name"] == "./azurpilot-bridge-guard.mjs"
    assert BRIDGE_GUARD_PATH.exists()
    for row in clients:
        config = row["config"]
        route = routes[config["serverName"]]
        assert config["transport"] == "streamable-http"
        assert config["url"] == route.bridge_url
        assert config["failOnStartupError"] is True
        headers = config["headers"]
        assert BRIDGE_CALLER_TOKEN_ENV_VAR in headers["Authorization"]
        assert IDENTITY_ENV_VARS[config["serverName"]] in headers[
            "x-azurpilot-expected-source-identity"
        ]
    assert "module.dev_mcp" not in text
    assert "module.game_mcp" not in text
    assert re.search(r"\b[0-9a-f]{40,64}\b", text) is None


def test_guard_uses_the_python_owned_environment_contract() -> None:
    text = BRIDGE_GUARD_PATH.read_text(encoding="utf-8")

    for variable in (
        CHECKOUT_ROOT_ENV_VAR,
        SOURCE_REVISION_ENV_VAR,
        AZUR_EXECUTABLE_ENV_VAR,
        *IDENTITY_ENV_VARS.values(),
    ):
        assert variable in text
    assert BRIDGE_CALLER_TOKEN_ENV_VAR not in text
    assert "tools/pre-execute" in text
    assert "AZURPILOT_DSH_SOURCE_DRIFT" in text
    assert "AZURPILOT_DSH_GUARD_NOT_PREPARED" in text


def test_load_generation_reads_prepare_envelope(tmp_path: Path) -> None:
    generation = _generation()
    envelope = tmp_path / "generation.json"
    envelope.write_text(
        json.dumps(
            {
                "ok": True,
                "code": "OK",
                "state": "ready",
                "message": "генерация",
                "details": generation.model_dump(mode="json"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert load_generation(envelope) == generation
    assert (
        load_generation(_write(tmp_path, "raw.json", generation.model_dump(mode="json")))
        == generation
    )

    broken = _write(tmp_path, "broken.json", {"ok": True})
    with pytest.raises(ToolingError) as failure:
        load_generation(broken)
    assert failure.value.code is ResultCode.TOOLING_PRECONDITION_FAILED

    with pytest.raises(ToolingError):
        load_generation(tmp_path / "absent.json")


def _families(result: object) -> dict[str, DshBridgeFamilyCheck]:
    details = getattr(result, "details", None)
    assert isinstance(details, DshVerificationDetails)
    return {item.server_name: item for item in details.families}


def _write(tmp_path: Path, name: str, payload: object) -> Path:
    target = tmp_path / name
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return target


def test_verify_fails_closed_without_prepared_environment(tmp_path: Path) -> None:
    service = DshBridgeService(resolver=_FakeResolver(tmp_path))

    with pytest.raises(ToolingError) as failure:
        service.verify(environ={})
    assert failure.value.code is ResultCode.TOOLING_PRECONDITION_FAILED

    incomplete = {SOURCE_REVISION_ENV_VAR: REVISION}
    with pytest.raises(ToolingError) as missing_identity:
        service.verify(environ=incomplete)
    assert missing_identity.value.code is ResultCode.TOOLING_PRECONDITION_FAILED

    broken = {
        SOURCE_REVISION_ENV_VAR: REVISION,
        IDENTITY_ENV_VARS["azurpilot-dev"]: "не идентичность",
        IDENTITY_ENV_VARS["azurpilot-game"]: "не идентичность",
    }
    with pytest.raises(ToolingError) as damaged:
        service.verify(environ=broken)
    assert damaged.value.code is ResultCode.TOOLING_PRECONDITION_FAILED


def test_verify_accepts_matching_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service = _patch_boundary(
        monkeypatch, root=tmp_path, revision=REVISION, dev_digest="a", game_digest="b"
    )

    result = service.verify_generation(_generation())

    assert result.ok is True
    assert result.code is ResultCode.OK
    assert [item.status for item in _families(result).values()] == ["ready", "ready"]


def test_verify_blocks_only_the_family_with_changed_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service = _patch_boundary(
        monkeypatch, root=tmp_path, revision=REVISION, dev_digest="f", game_digest="b"
    )

    result = service.verify_generation(_generation())
    families = _families(result)

    assert result.ok is False
    assert result.code is ResultCode.MCP_SOURCE_BUNDLE_DRIFT
    assert families["azurpilot-dev"].status == "drift"
    assert families["azurpilot-dev"].mismatched_fields == ("source_set_digest",)
    assert families["azurpilot-game"].status == "ready"


def test_verify_blocks_both_families_when_revision_changed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service = _patch_boundary(
        monkeypatch,
        root=tmp_path,
        revision=OTHER_REVISION,
        dev_digest="a",
        game_digest="b",
    )

    result = service.verify_generation(_generation())
    families = _families(result)

    assert result.ok is False
    assert families["azurpilot-dev"].mismatched_fields == ("source_revision",)
    assert families["azurpilot-game"].mismatched_fields == ("source_revision",)
    assert "Git HEAD изменился" in families["azurpilot-dev"].message


def test_generation_environment_carries_only_client_contract_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service = _patch_boundary(
        monkeypatch, root=tmp_path, revision=REVISION, dev_digest="a", game_digest="b"
    )
    monkeypatch.setattr(
        dsh_tooling.shutil, "which", lambda command: f"/usr/bin/{command}"
    )
    generation = _generation()

    environment = service._generation_environment(
        tmp_path,
        generation,
        "caller-token",
        {"PATH": "/usr/bin"},
    )

    assert environment["PATH"] == "/usr/bin"
    assert environment[CHECKOUT_ROOT_ENV_VAR] == str(tmp_path)
    assert environment[SOURCE_REVISION_ENV_VAR] == REVISION
    assert environment[DSH_PROFILE_ENV_VAR] == DEFAULT_DSH_PROFILE
    assert environment[DSH_PACKAGE_ENV_VAR] == DEFAULT_DSH_PACKAGE
    assert environment[AZUR_EXECUTABLE_ENV_VAR] == "/usr/bin/azur"
    assert environment[BRIDGE_CALLER_TOKEN_ENV_VAR] == "caller-token"
    for record in generation.families:
        assert environment[IDENTITY_ENV_VARS[record.server_name]] == (
            serialize_identity(record.identity)
        )
    assert "AZURPILOT_DEV_LOCAL_MCP_TOKEN" not in environment
    assert "AZURPILOT_GAME_LOCAL_MCP_TOKEN" not in environment
