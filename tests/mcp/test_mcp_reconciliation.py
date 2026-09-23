from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import azurpilot.tooling.mcp as mcp_tooling
from azurpilot.tooling.contracts import (
    McpAcceptanceDetails,
    McpImpactDetails,
    McpReconcileDetails,
    OperationState,
    ResultCode,
)
from azurpilot.tooling.errors import ToolingError
from dev_tools import mcp_status
from dev_tools.mcp_status import first_party_source_registration
from module.mcp_shared.catalog import tool_catalog_sha256_from_tools
from module.mcp_shared.local_http_supervisor import (
    LocalHttpSupervisorStopOutcome,
    LocalHttpSupervisorStopResult,
)
from module.mcp_shared.versioning import (
    VersioningError,
    load_mcp_bundle,
)
from tests.support.paths import REPOSITORY_ROOT


def _successful_stop_result() -> LocalHttpSupervisorStopResult:
    return LocalHttpSupervisorStopResult(
        outcome=LocalHttpSupervisorStopOutcome.EXACT_LIVE_OWNER_STOPPED,
        marker_present=True,
        marker_removed=True,
        ownership_confirmed=True,
        postcondition_confirmed=True,
    )


def _stale_recovery_stop_result() -> LocalHttpSupervisorStopResult:
    return LocalHttpSupervisorStopResult(
        outcome=LocalHttpSupervisorStopOutcome.STALE_RECORDED_OWNER_RECOVERED,
        marker_present=True,
        marker_removed=True,
        ownership_confirmed=True,
        postcondition_confirmed=True,
    )


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


def test_base_aware_reconcile_is_idempotent_for_the_same_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_bundle = load_mcp_bundle(REPOSITORY_ROOT)
    for relative in (
        Path("config/mcp-versions.toml"),
        mcp_tooling.PLUGIN_MANIFEST_PATH,
        mcp_tooling.PLUGIN_COMPATIBILITY_PATH,
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPOSITORY_ROOT / relative, target)

    candidate_digests = dict(base_bundle.source_digests)
    candidate_digests["DEV_MCP_SOURCE_SET"] = "d" * 64
    candidate_digests["PLUGIN_BUNDLE_SOURCE_SET"] = "e" * 64
    candidate_digests["SKILL_BUNDLE_SOURCE_SET"] = "f" * 64
    monkeypatch.setattr(
        mcp_tooling,
        "source_set_digests",
        lambda _root: dict(candidate_digests),
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
                plugin_version=base_bundle.plugin_version,
                bundle_revision=base_bundle.bundle_revision,
                skill_bundle_revision=base_bundle.skill_bundle_revision,
            )
        ),
    )

    reconciler = mcp_tooling.McpSourceReconciler()
    first = reconciler.reconcile(tmp_path, base_commit="a" * 40)
    first_versions = {
        name: server.version for name, server in first.bundle.servers.items()
    }
    candidate_digests["DEV_MCP_SOURCE_SET"] = "9" * 64
    second = reconciler.reconcile(tmp_path, base_commit="a" * 40)
    second_artifacts = tuple(
        (tmp_path / path).read_bytes() for path in mcp_tooling.MCP_GENERATED_ARTIFACTS
    )
    third = reconciler.reconcile(tmp_path, base_commit="a" * 40)
    third_artifacts = tuple(
        (tmp_path / path).read_bytes() for path in mcp_tooling.MCP_GENERATED_ARTIFACTS
    )

    assert first_versions == {
        name: server.version for name, server in second.bundle.servers.items()
    }
    assert first_versions == {
        name: server.version for name, server in third.bundle.servers.items()
    }
    assert second.bundle.bundle_revision == third.bundle.bundle_revision
    assert second_artifacts == third_artifacts
    assert first.bundle.bundle_revision != second.bundle.bundle_revision
    assert reconciler.check(tmp_path, build=third).bundle == third.bundle


def test_mcp_sync_returns_terminal_no_changes_without_runtime_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_commit = "a" * 40
    impact = McpImpactDetails(
        base_sha=base_commit,
        head_sha="b" * 40,
        status="NOT_REQUIRED",
        reconciliation_required=False,
    )
    service = mcp_tooling.McpService()
    source_checks: list[Path] = []
    monkeypatch.setattr(service, "_root", lambda _root: tmp_path)
    monkeypatch.setattr(mcp_tooling, "_candidate_mcp_impact", lambda *_a, **_kw: impact)
    monkeypatch.setattr(
        service.source,
        "check",
        lambda root: source_checks.append(root),
    )

    def unexpected(*_args, **_kwargs):
        raise AssertionError("NO_CHANGES не должен менять generated source или runtime")

    monkeypatch.setattr(service.source, "reconcile", unexpected)
    monkeypatch.setattr(service, "reconcile", unexpected)
    monkeypatch.setattr(service, "accept", unexpected)

    result = service.sync(tmp_path, base_commit=base_commit)

    assert result.ok
    assert result.details is not None
    assert result.details.terminal == "NO_CHANGES"
    assert result.details.base_sha == base_commit
    assert source_checks == [tmp_path]


def test_mcp_sync_runs_source_runtime_and_acceptance_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_commit = "a" * 40
    impact = McpImpactDetails(
        base_sha=base_commit,
        head_sha="b" * 40,
        status="REQUIRED",
        candidate_path_count=1,
        candidate_paths=("module/dev_mcp/server.py",),
        changed_components=("DEV_MCP_SOURCE_SET",),
        affected_servers=("azurpilot-dev",),
        generated_artifacts=tuple(
            path.as_posix() for path in mcp_tooling.MCP_GENERATED_ARTIFACTS
        ),
        reconciliation_required=True,
    )
    source_digests = {name: "a" * 64 for name in mcp_tooling.SOURCE_SET_NAMES}
    build = SimpleNamespace(
        bundle=SimpleNamespace(source_digests=source_digests),
        changed_components=("DEV_MCP_SOURCE_SET",),
        affected_servers=("azurpilot-dev",),
    )
    runtime_details = McpReconcileDetails(
        mode="runtime",
        source_state="ready",
        runtime_state="ready",
        source_reconciled=True,
        runtime_ready=True,
        mutation_performed=False,
        changed_components=("DEV_MCP_SOURCE_SET",),
        affected_servers=("azurpilot-dev",),
        session_state="not_observable",
    )
    acceptance_details = McpAcceptanceDetails(
        acceptance_state="READY",
        reason_code="FRESH_CLIENT_READY",
        initialized=True,
    )
    service = mcp_tooling.McpService()
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(service, "_root", lambda _root: tmp_path)
    monkeypatch.setattr(mcp_tooling, "source_set_digests", lambda _root: source_digests)
    monkeypatch.setattr(mcp_tooling, "_candidate_mcp_impact", lambda *_a, **_kw: impact)

    def reconcile_source(_root, *, requested_bump, base_commit):
        calls.append(("source", (requested_bump, base_commit)))
        return build

    def check_source(_root, *, build):
        calls.append(("check", build))
        return build

    def check_base(_root, *, base_commit, build):
        calls.append(("base", (base_commit, build)))
        return None

    def reconcile_runtime(_root):
        calls.append(("runtime", None))
        return SimpleNamespace(
            ok=True,
            code=ResultCode.OK,
            state=OperationState.READY,
            message="runtime ready",
            details=runtime_details,
        )

    def accept(_root, *, allow_dirty):
        calls.append(("accept", allow_dirty))
        return SimpleNamespace(
            ok=True,
            code=ResultCode.OK,
            state=OperationState.READY,
            message="fresh client ready",
            details=acceptance_details,
        )

    monkeypatch.setattr(service.source, "reconcile", reconcile_source)
    monkeypatch.setattr(service.source, "check", check_source)
    monkeypatch.setattr(service.source, "check_base_to_head", check_base)
    monkeypatch.setattr(service, "reconcile", reconcile_runtime)
    monkeypatch.setattr(service, "accept", accept)

    result = service.sync(tmp_path, base_commit=base_commit)

    assert result.ok
    assert result.details is not None
    assert result.details.terminal == "SYNCED"
    assert result.details.runtime == runtime_details
    assert result.details.acceptance == acceptance_details
    assert tuple(name for name, _value in calls) == (
        "source",
        "check",
        "base",
        "runtime",
        "accept",
        "check",
    )
    assert calls[0] == ("source", ("auto", base_commit))
    assert calls[4] == ("accept", True)


def test_mcp_sync_fails_terminally_when_owned_runtime_is_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_commit = "a" * 40
    impact = McpImpactDetails(
        base_sha=base_commit,
        head_sha="b" * 40,
        status="REQUIRED",
        changed_components=("DEV_MCP_SOURCE_SET",),
        affected_servers=("azurpilot-dev",),
        reconciliation_required=True,
    )
    source_digests = {name: "a" * 64 for name in mcp_tooling.SOURCE_SET_NAMES}
    build = SimpleNamespace(
        bundle=SimpleNamespace(source_digests=source_digests),
        changed_components=("DEV_MCP_SOURCE_SET",),
        affected_servers=("azurpilot-dev",),
    )
    runtime_details = McpReconcileDetails(
        mode="runtime",
        source_state="ready",
        runtime_state="unknown",
        source_reconciled=True,
        runtime_ready=False,
        mutation_performed=False,
        changed_components=("DEV_MCP_SOURCE_SET",),
        affected_servers=("azurpilot-dev",),
        session_state="unknown",
    )
    service = mcp_tooling.McpService()
    monkeypatch.setattr(service, "_root", lambda _root: tmp_path)
    monkeypatch.setattr(mcp_tooling, "source_set_digests", lambda _root: source_digests)
    monkeypatch.setattr(mcp_tooling, "_candidate_mcp_impact", lambda *_a, **_kw: impact)
    monkeypatch.setattr(service.source, "reconcile", lambda *_a, **_kw: build)
    monkeypatch.setattr(service.source, "check", lambda *_a, **_kw: build)
    monkeypatch.setattr(service.source, "check_base_to_head", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        service,
        "reconcile",
        lambda _root: SimpleNamespace(
            ok=False,
            code=ResultCode.MCP_RUNTIME_UNAVAILABLE,
            state=OperationState.FAILED,
            message="owned runtime is not ready",
            details=runtime_details,
        ),
    )
    monkeypatch.setattr(
        service,
        "accept",
        lambda *_a, **_kw: pytest.fail("acceptance must not run before runtime is ready"),
    )

    result = service.sync(tmp_path, base_commit=base_commit)

    assert not result.ok
    assert result.code is ResultCode.MCP_RUNTIME_UNAVAILABLE
    assert result.details is not None
    assert result.details.terminal == "FAILED"
    assert result.details.runtime == runtime_details
    assert result.details.acceptance is None


def test_legacy_bundle_schema_is_rejected(tmp_path: Path) -> None:
    manifest = tmp_path / "config" / "mcp-versions.toml"
    manifest.parent.mkdir()
    content = (REPOSITORY_ROOT / "config" / "mcp-versions.toml").read_text(
        encoding="utf-8"
    )
    manifest.write_text(
        content.replace("schema_version = 2", "schema_version = 1", 1),
        encoding="utf-8",
    )

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


def test_source_classification_preserves_dot_prefixed_paths() -> None:
    classification = mcp_tooling.classify_source_changes(
        ("./.codex/config.toml", ".github/workflows/ci.yml")
    )

    assert classification.unknown_paths == (
        ".codex/config.toml",
        ".github/workflows/ci.yml",
    )


def test_source_classification_follows_bounded_backend_dependencies() -> None:
    game = mcp_tooling.classify_source_changes(
        ("module/application/game_control_service.py",)
    )
    assert game.changed_components == ("GAME_MCP_SOURCE_SET",)
    assert game.affected_servers == ("azurpilot-game",)

    dev = mcp_tooling.classify_source_changes(("module/dev_runtime/control.py",))
    assert dev.changed_components == ("DEV_MCP_SOURCE_SET",)
    assert dev.affected_servers == ("azurpilot-dev",)

    shared = mcp_tooling.classify_source_changes(
        ("azurpilot/tooling/process_core.py",)
    )
    assert shared.changed_components == ("SHARED_MCP_SOURCE_SET",)
    assert shared.affected_servers == ("azurpilot-dev", "azurpilot-game")

    management = mcp_tooling.classify_source_changes(("azurpilot/tooling/mcp.py",))
    assert management.changed_components == ()
    assert management.affected_servers == ()
    assert management.unknown_paths == ("azurpilot/tooling/mcp.py",)


def test_management_and_docker_tooling_changes_do_not_affect_mcp_identity() -> None:
    classification = mcp_tooling.classify_source_changes(
        (
            "azurpilot/tooling/contracts.py",
            "azurpilot/tooling/coordination.py",
            "azurpilot/tooling/docker.py",
            "azurpilot/tooling/errors.py",
            "azurpilot/tooling/filesystem.py",
            "azurpilot/tooling/process.py",
        )
    )

    assert classification.changed_components == ()
    assert classification.affected_servers == ()
    assert classification.unknown_paths == (
        "azurpilot/tooling/contracts.py",
        "azurpilot/tooling/coordination.py",
        "azurpilot/tooling/docker.py",
        "azurpilot/tooling/errors.py",
        "azurpilot/tooling/filesystem.py",
        "azurpilot/tooling/process.py",
    )


def test_dedicated_shared_runtime_modules_affect_both_servers() -> None:
    classification = mcp_tooling.classify_source_changes(
        (
            "azurpilot/tooling/mcp_contracts.py",
            "azurpilot/tooling/mcp_coordination.py",
            "azurpilot/tooling/mcp_errors.py",
            "azurpilot/tooling/mcp_filesystem.py",
            "azurpilot/tooling/process_core.py",
            "azurpilot/tooling/result.py",
        )
    )

    assert classification.changed_components == ("SHARED_MCP_SOURCE_SET",)
    assert classification.affected_servers == (
        "azurpilot-dev",
        "azurpilot-game",
    )
    assert classification.unknown_paths == ()


def test_game_application_service_file_is_in_game_backend_identity() -> None:
    classification = mcp_tooling.classify_source_changes(
        ("module/application/game_control_service.py",)
    )

    assert Path("module/application/game_control_service.py") in mcp_tooling.SOURCE_SET_PATHS[
        "GAME_MCP_SOURCE_SET"
    ]
    assert classification.affected_servers == ("azurpilot-game",)


def test_mcp_impact_reports_not_required_for_non_source_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeGit:
        def __init__(self, _root: Path) -> None:
            pass

        def head(self) -> str:
            return "b" * 40

        def is_ancestor(self, _base: str, _head: str) -> bool:
            return True

        def changed_paths(self, _base: str, _head: str) -> tuple[str, ...]:
            return ("docs/notes.md",)

        def status_z(self) -> str:
            return " M docs/working-notes.md\x00"

    monkeypatch.setattr(mcp_tooling, "GitClient", FakeGit)

    details = mcp_tooling._candidate_mcp_impact(tmp_path, base_commit="a" * 40)

    assert details.status == "NOT_REQUIRED"
    assert details.reconciliation_required is False
    assert details.candidate_paths == (
        "docs/notes.md",
        "docs/working-notes.md",
    )
    assert details.candidate_path_count == 2
    assert details.path_impacts == ()
    assert details.generated_artifacts == ()


def test_mcp_impact_includes_uncommitted_persistence_and_both_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeGit:
        def __init__(self, _root: Path) -> None:
            pass

        def head(self) -> str:
            return "b" * 40

        def is_ancestor(self, _base: str, _head: str) -> bool:
            return True

        def changed_paths(self, _base: str, _head: str) -> tuple[str, ...]:
            return ()

        def status_z(self) -> str:
            return " M module/persistence/runtime.py\x00"

    monkeypatch.setattr(mcp_tooling, "GitClient", FakeGit)

    details = mcp_tooling._candidate_mcp_impact(tmp_path, base_commit="a" * 40)

    assert details.status == "REQUIRED"
    assert details.reconciliation_required is True
    assert details.changed_components == (
        "DEV_MCP_SOURCE_SET",
        "GAME_MCP_SOURCE_SET",
    )
    assert details.affected_servers == ("azurpilot-dev", "azurpilot-game")
    assert details.path_impacts[0].affected_servers == (
        "azurpilot-dev",
        "azurpilot-game",
    )
    assert details.generated_artifacts == (
        "config/mcp-versions.toml",
        "plugins/azurpilot/.codex-plugin/plugin.json",
        "plugins/azurpilot/compatibility.json",
    )


def test_mcp_impact_classifies_full_candidate_set_and_returns_bounded_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = "module/persistence/runtime.py"
    committed_paths = tuple(f"docs/candidate-{index:03d}.md" for index in range(300)) + (
        source_path,
    )

    class FakeGit:
        def __init__(self, _root: Path) -> None:
            pass

        def head(self) -> str:
            return "b" * 40

        def is_ancestor(self, _base: str, _head: str) -> bool:
            return True

        def changed_paths(self, _base: str, _head: str) -> tuple[str, ...]:
            return committed_paths

        def status_z(self) -> str:
            return ""

    monkeypatch.setattr(mcp_tooling, "GitClient", FakeGit)

    details = mcp_tooling._candidate_mcp_impact(tmp_path, base_commit="a" * 40)

    assert details.candidate_path_count == 301
    assert len(details.candidate_paths) == mcp_tooling._MCP_IMPACT_SAMPLE_LIMIT
    assert len(details.committed_paths) == mcp_tooling._MCP_IMPACT_SAMPLE_LIMIT
    assert all(item.source_sets for item in details.path_impacts)
    assert tuple(item.path for item in details.path_impacts) == (source_path,)


@pytest.mark.parametrize(
    "changed_path",
    (
        "plugins/azurpilot/README.md",
        "plugins/azurpilot/skills/azurpilot-development/SKILL.md",
    ),
)
def test_plugin_change_requires_session_reload_without_backend_restart(
    monkeypatch: pytest.MonkeyPatch,
    changed_path: str,
) -> None:
    service = mcp_tooling.McpService()
    bundle = load_mcp_bundle(REPOSITORY_ROOT)
    monkeypatch.setattr(service, "_bundle", lambda _root: bundle)
    monkeypatch.setattr(
        service,
        "_runtime_status",
        lambda _root, _bundle: (
            "ready",
            {
                "services": [
                    {"server_name": name, "ready": True}
                    for name in mcp_tooling.MCP_SERVER_NAMES
                ],
                "supervisors": {},
            },
        ),
    )

    result = service.reconcile(
        REPOSITORY_ROOT,
        changed_paths=(changed_path,),
    )

    assert not result.ok
    assert result.code is ResultCode.MCP_RELOAD_REQUIRED
    assert result.details is not None
    assert result.details.restarted_servers == ()
    assert result.details.reload_required is True


def test_source_reconcile_reports_plugin_reload_as_non_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = mcp_tooling.McpService()
    build = SimpleNamespace(
        changed_components=("PLUGIN_BUNDLE_SOURCE_SET",),
        affected_servers=(),
        plugin_changed=True,
        skill_changed=False,
        bundle=load_mcp_bundle(REPOSITORY_ROOT),
    )
    monkeypatch.setattr(service.source, "reconcile", lambda _root, requested_bump: build)

    result = service.reconcile(REPOSITORY_ROOT, source=True)

    assert not result.ok
    assert result.code is ResultCode.MCP_RELOAD_REQUIRED
    assert result.details is not None
    assert result.details.reload_required is True


def test_status_preserves_plugin_drift_when_runtime_is_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = mcp_tooling.McpService()
    bundle = load_mcp_bundle(REPOSITORY_ROOT)
    build = SimpleNamespace(
        bundle=bundle,
        changed_components=(),
        affected_servers=(),
    )
    monkeypatch.setattr(service, "_bundle", lambda _root: bundle)
    monkeypatch.setattr(service.source, "build", lambda _root, requested_bump: build)
    monkeypatch.setattr(service.source, "check", lambda _root, **_kwargs: build)
    monkeypatch.setattr(
        service,
        "_runtime_status",
        lambda _root, _bundle: (
            "stopped",
            {
                "services": [],
                "supervisors": {},
            },
        ),
    )
    monkeypatch.setattr(service, "_registration_state", lambda _root: "ready")
    monkeypatch.setattr(
        service,
        "_plugin_source_state",
        lambda _current, _build: "drift",
    )

    result = service.status(REPOSITORY_ROOT)

    assert not result.ok
    assert result.code is ResultCode.MCP_RELOAD_REQUIRED
    assert result.details is not None
    assert result.details.runtime_state == "stopped"
    assert result.details.source_reconciled is False
    assert result.details.runtime_ready is False
    assert result.details.plugin_source_state == "drift"
    assert result.details.session_state == "reload_required"
    assert result.details.reload_required is True


def test_status_does_not_claim_live_ready_after_source_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = mcp_tooling.McpService()
    bundle = load_mcp_bundle(REPOSITORY_ROOT)
    build = SimpleNamespace(
        bundle=bundle,
        changed_components=(),
        affected_servers=(),
        plugin_changed=False,
        skill_changed=False,
    )
    monkeypatch.setattr(service, "_bundle", lambda _root: bundle)
    monkeypatch.setattr(service.source, "build", lambda _root, requested_bump: build)
    monkeypatch.setattr(service.source, "check", lambda _root, **_kwargs: build)
    monkeypatch.setattr(
        service,
        "_runtime_status",
        lambda _root, _bundle: (
            "stopped",
            {
                "services": [],
                "supervisors": {
                    name: {"code": "LOCAL_MCP_SUPERVISOR_STOPPED"}
                    for name in mcp_tooling.MCP_SERVER_NAMES
                },
            },
        ),
    )
    monkeypatch.setattr(service, "_registration_state", lambda _root: "ready")

    result = service.status(REPOSITORY_ROOT)

    assert not result.ok
    assert result.code is ResultCode.MCP_RUNTIME_UNAVAILABLE
    assert result.details is not None
    assert result.details.source_reconciled is True
    assert result.details.runtime_ready is False
    assert result.details.runtime_state == "stopped"


def test_status_preserves_version_bump_required_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = mcp_tooling.McpService()
    bundle = load_mcp_bundle(REPOSITORY_ROOT)

    def fail_build(_root: Path, *, requested_bump: str | None = None):
        del requested_bump
        raise ToolingError(
            ResultCode.MCP_VERSION_BUMP_REQUIRED,
            "MCP version bump required",
        )

    monkeypatch.setattr(service, "_bundle", lambda _root: bundle)
    monkeypatch.setattr(service.source, "build", fail_build)
    monkeypatch.setattr(service.source, "check", lambda _root, **_kwargs: None)
    monkeypatch.setattr(
        service,
        "_runtime_status",
        lambda _root, _bundle: ("stopped", {"services": [], "supervisors": {}}),
    )
    monkeypatch.setattr(service, "_registration_state", lambda _root: "ready")

    result = service.status(REPOSITORY_ROOT)

    assert not result.ok
    assert result.code is ResultCode.MCP_VERSION_BUMP_REQUIRED


def test_reconcile_rejects_unknown_restart_postcondition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = mcp_tooling.McpService()
    bundle = load_mcp_bundle(REPOSITORY_ROOT)
    runtime_states = [
        (
            "stale",
            {
                "services": [
                    {"server_name": name, "ready": False}
                    for name in mcp_tooling.MCP_SERVER_NAMES
                ],
                "supervisors": {
                    name: {"code": "LOCAL_MCP_SUPERVISOR_READY"}
                    for name in mcp_tooling.MCP_SERVER_NAMES
                },
            },
        ),
        ("unknown", {"services": [], "supervisors": {}}),
    ]
    runtime_state_index = 0

    def runtime_status(_root, _bundle):
        nonlocal runtime_state_index
        if runtime_state_index >= len(runtime_states):
            raise AssertionError("runtime status вызван сверх ожидаемого числа раз")
        result = runtime_states[runtime_state_index]
        runtime_state_index += 1
        return result
    monkeypatch.setattr(service, "_bundle", lambda _root: bundle)
    monkeypatch.setattr(service, "_runtime_status", runtime_status)
    monkeypatch.setattr(
        service,
        "_start_owned",
        lambda _root, _bundle, *, server_names: (True, {}),
    )
    monkeypatch.setattr(
        service,
        "_supervisor",
        lambda _root, _server_name: SimpleNamespace(
            stop_result=_successful_stop_result
        ),
    )

    with pytest.raises(ToolingError) as error:
        service.reconcile(REPOSITORY_ROOT)

    assert error.value.code is ResultCode.MCP_RUNTIME_UNAVAILABLE


def test_runtime_reconcile_repairs_only_stale_owned_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = mcp_tooling.McpService()
    bundle = load_mcp_bundle(REPOSITORY_ROOT)
    stale_name = "azurpilot-dev"
    ready_name = "azurpilot-game"
    runtime_results = [
        (
            "stale",
            {
                "services": [
                    {"server_name": stale_name, "ready": False},
                    {"server_name": ready_name, "ready": True},
                ],
                "supervisors": {
                    stale_name: {"code": "LOCAL_MCP_SUPERVISOR_STALE"},
                    ready_name: {"code": "LOCAL_MCP_SUPERVISOR_READY"},
                },
            },
        ),
        (
            "ready",
            {
                "services": [
                    {"server_name": stale_name, "ready": True},
                    {"server_name": ready_name, "ready": True},
                ],
                "supervisors": {
                    stale_name: {"code": "LOCAL_MCP_SUPERVISOR_READY"},
                    ready_name: {"code": "LOCAL_MCP_SUPERVISOR_READY"},
                },
            },
        ),
    ]
    stopped: list[str] = []
    started: list[tuple[str, ...]] = []

    monkeypatch.setattr(service, "_bundle", lambda _root: bundle)
    monkeypatch.setattr(
        service,
        "_runtime_status",
        lambda _root, _bundle: runtime_results.pop(0),
    )
    monkeypatch.setattr(
        service,
        "_supervisor",
        lambda _root, name: SimpleNamespace(
            stop_result=lambda: stopped.append(name)
            or _stale_recovery_stop_result()
        ),
    )

    def start_owned(
        _root: Path,
        _bundle: object,
        *,
        server_names: tuple[str, ...],
    ) -> tuple[bool, dict[str, object]]:
        started.append(server_names)
        return True, {}

    monkeypatch.setattr(service, "_start_owned", start_owned)

    result = service.reconcile(REPOSITORY_ROOT)

    assert result.ok
    assert stopped == [stale_name]
    assert started == [(stale_name,)]
    assert result.details is not None
    assert result.details.runtime_ready is True
    assert result.details.restarted_servers == (stale_name,)
    assert result.details.session_state == "not_observable"


@pytest.mark.parametrize(
    "supervisor_code",
    (
        "LOCAL_MCP_SUPERVISOR_OWNERSHIP_MISMATCH",
        "LOCAL_MCP_SUPERVISOR_MARKER_INVALID",
        "LOCAL_MCP_SUPERVISOR_UNKNOWN",
    ),
)
def test_runtime_reconcile_fails_closed_for_unowned_stale_service(
    monkeypatch: pytest.MonkeyPatch,
    supervisor_code: str,
) -> None:
    service = mcp_tooling.McpService()
    bundle = load_mcp_bundle(REPOSITORY_ROOT)
    stale_name = "azurpilot-dev"
    stopped: list[str] = []
    started = False

    monkeypatch.setattr(service, "_bundle", lambda _root: bundle)
    monkeypatch.setattr(
        service,
        "_runtime_status",
        lambda _root, _bundle: (
            "stale",
            {
                "services": [
                    {"server_name": stale_name, "ready": False},
                    {"server_name": "azurpilot-game", "ready": True},
                ],
                "supervisors": {
                    stale_name: {"code": supervisor_code},
                    "azurpilot-game": {"code": "LOCAL_MCP_SUPERVISOR_READY"},
                },
            },
        ),
    )
    monkeypatch.setattr(
        service,
        "_supervisor",
        lambda _root, name: SimpleNamespace(
            stop_result=lambda: stopped.append(name) or _successful_stop_result()
        ),
    )

    def start_owned(*_args: object, **_kwargs: object) -> tuple[bool, dict[str, object]]:
        nonlocal started
        started = True
        return True, {}

    monkeypatch.setattr(service, "_start_owned", start_owned)

    with pytest.raises(ToolingError) as error:
        service.reconcile(REPOSITORY_ROOT)

    assert error.value.code is ResultCode.MCP_RUNTIME_STALE
    assert stopped == []
    assert started is False


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


def test_shared_digest_ignores_unrelated_management_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        mcp_tooling,
        "SOURCE_SET_PATHS",
        {"SHARED_MCP_SOURCE_SET": (Path("runtime.py"),)},
    )
    runtime = tmp_path / "runtime.py"
    unrelated = tmp_path / "docker_contract.py"
    runtime.write_text("runtime-v1", encoding="utf-8")
    unrelated.write_text("docker-v1", encoding="utf-8")

    before = mcp_tooling.source_set_digest(tmp_path, "SHARED_MCP_SOURCE_SET")
    unrelated.write_text("docker-v2", encoding="utf-8")
    after_unrelated_change = mcp_tooling.source_set_digest(
        tmp_path, "SHARED_MCP_SOURCE_SET"
    )
    runtime.write_text("runtime-v2", encoding="utf-8")
    after_runtime_change = mcp_tooling.source_set_digest(
        tmp_path, "SHARED_MCP_SOURCE_SET"
    )

    assert after_unrelated_change == before
    assert after_runtime_change != before


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
    current_major = int(server.version.split(".", 1)[0])
    assert mcp_tooling._server_version(server, bump="major").startswith(
        f"{current_major + 1}."
    )


def test_auth_readiness_is_scoped_to_servers_being_started(monkeypatch) -> None:
    monkeypatch.setenv("AZURPILOT_DEV_LOCAL_MCP_TOKEN", "dev-token")
    monkeypatch.delenv("AZURPILOT_GAME_LOCAL_MCP_TOKEN", raising=False)

    assert mcp_tooling.McpService._auth_ready(("azurpilot-dev",))
    assert not mcp_tooling.McpService._auth_ready()


@pytest.mark.parametrize(
    ("registration", "expected"),
    (({"status": "partial"}, "invalid"), (OSError("probe failed"), "unknown")),
)
def test_registration_state_distinguishes_unknown_probe(
    registration: object, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if isinstance(registration, BaseException):
        def failed_probe(_root: Path) -> dict[str, object]:
            raise registration

        monkeypatch.setattr(mcp_status, "first_party_source_registration", failed_probe)
    else:
        monkeypatch.setattr(
            mcp_status,
            "first_party_source_registration",
            lambda _root: registration,
        )

    assert mcp_tooling.McpService._registration_state(REPOSITORY_ROOT) == expected


def test_runtime_source_revision_is_sanitized_before_status_model() -> None:
    server = load_mcp_bundle(REPOSITORY_ROOT).servers["azurpilot-game"]

    status = mcp_tooling._server_status_from_model(
        server,
        status="ready",
        observed_source_revision="not-a-git-revision",
    )

    assert status.source_revision is None


@pytest.mark.parametrize(
    "stopped_servers",
    [
        pytest.param(("azurpilot-dev",), id="one-stopped-supervisor"),
        pytest.param(mcp_tooling.MCP_SERVER_NAMES, id="all-stopped-supervisors"),
    ],
)
def test_runtime_reconcile_starts_stopped_owned_supervisors(
    monkeypatch: pytest.MonkeyPatch,
    stopped_servers: tuple[str, ...],
) -> None:
    service = mcp_tooling.McpService()
    bundle = load_mcp_bundle(REPOSITORY_ROOT)
    initial_services = (
        []
        if stopped_servers == mcp_tooling.MCP_SERVER_NAMES
        else [
            {"server_name": name, "ready": name not in stopped_servers}
            for name in mcp_tooling.MCP_SERVER_NAMES
        ]
    )
    initial_supervisors = {
        name: {
            "code": (
                "LOCAL_MCP_SUPERVISOR_STOPPED"
                if name in stopped_servers
                else "LOCAL_MCP_SUPERVISOR_READY"
            )
        }
        for name in mcp_tooling.MCP_SERVER_NAMES
    }
    runtime_results = [
        (
            "stopped" if stopped_servers == mcp_tooling.MCP_SERVER_NAMES else "stale",
            {
                "services": initial_services,
                "supervisors": initial_supervisors,
            },
        ),
        (
            "ready",
            {
                "services": [
                    {"server_name": name, "ready": True}
                    for name in mcp_tooling.MCP_SERVER_NAMES
                ],
                "supervisors": {},
            },
        ),
    ]
    start_calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(service, "_bundle", lambda _root: bundle)
    monkeypatch.setattr(
        service,
        "_runtime_status",
        lambda _root, _bundle: runtime_results.pop(0),
    )
    monkeypatch.setattr(service, "_session_state", lambda *_args, **_kwargs: "not_observable")

    def start_owned(
        _root: Path,
        _bundle: object,
        *,
        server_names: tuple[str, ...],
    ) -> tuple[bool, dict[str, object]]:
        start_calls.append(server_names)
        return True, {}

    monkeypatch.setattr(service, "_start_owned", start_owned)

    result = service.reconcile(REPOSITORY_ROOT)

    assert result.ok
    assert start_calls == [stopped_servers]
    assert result.details is not None
    assert result.details.source_reconciled is True
    assert result.details.runtime_ready is True
    assert result.details.restarted_servers == stopped_servers
    assert result.details.session_state == "not_observable"


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
    assert isinstance(error.value.details, mcp_tooling.McpSourceDriftDetails)
    assert error.value.details.artifact == "config/mcp-versions.toml"
    assert error.value.details.changed_source_sets == ("SKILL_BUNDLE_SOURCE_SET",)
    assert error.value.details.affected_servers == ()
    assert set(error.value.details.expected_source_digests) == {
        "SKILL_BUNDLE_SOURCE_SET"
    }
    assert set(error.value.details.actual_source_digests) == {
        "SKILL_BUNDLE_SOURCE_SET"
    }


def test_shared_runtime_change_invalidates_and_reconcile_restores_bundle(
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
    old_bundle = load_mcp_bundle(tmp_path)
    (tmp_path / source_paths["SHARED_MCP_SOURCE_SET"][0]).write_text(
        "shared-runtime-changed", encoding="utf-8"
    )

    with pytest.raises(ToolingError) as error:
        reconciler.check(tmp_path)

    assert error.value.code is ResultCode.MCP_SOURCE_BUNDLE_DRIFT
    assert isinstance(error.value.details, mcp_tooling.McpSourceDriftDetails)
    assert error.value.details.changed_source_sets == ("SHARED_MCP_SOURCE_SET",)
    assert error.value.details.affected_servers == (
        "azurpilot-dev",
        "azurpilot-game",
    )
    assert error.value.details.expected_source_digests != {}
    assert error.value.details.actual_source_digests != {}

    reconciler.reconcile(tmp_path)
    new_bundle = reconciler.check(tmp_path).bundle
    assert new_bundle.source_digests["SHARED_MCP_SOURCE_SET"] != old_bundle.source_digests[
        "SHARED_MCP_SOURCE_SET"
    ]
