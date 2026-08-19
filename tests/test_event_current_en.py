import hashlib
import json
from pathlib import Path, PurePosixPath

import pytest

from dev_tools.event_datamine_build import build_current_event
from module.event_datamine.artifact import BUILTIN_ARTIFACT_ROOT, build_artifact, load_artifact
from module.event_datamine.compiler import EventCompiler
from module.event_datamine.discovery import discover_major_events, resolve_current_candidate
from module.event_datamine.map_compiler import sharecfg_values
from module.event_datamine.registry import EventArtifactRegistry
from module.event_datamine.source import ShareCfgLoader, SourceSnapshot
from module.event_datamine.supplemental import resolve_supplemental_event_spec
from module.webui.event_source import load_current_event_plan
from tests.event_fixture_helpers import (
    CURRENT_FIXTURE_ROOT,
    ROOT,
    artifact_active_time,
    current_fixture_identity,
    current_fixture_manifest,
    production_artifact,
    production_artifact_path,
)

FIXTURE = CURRENT_FIXTURE_ROOT


def _loader():
    _, server, repository, revision, _ = current_fixture_identity()
    return ShareCfgLoader(SourceSnapshot(FIXTURE, server, repository, revision))


def _current(loader):
    _, server, _, _, activity_id = current_fixture_identity()
    candidate = resolve_current_candidate(
        discover_major_events(loader),
        server=server,
        now=artifact_active_time(),
    )
    assert candidate is not None
    assert candidate.activity_id == activity_id
    return candidate


def _current_selector() -> str:
    event_id, server, *_ = current_fixture_identity()
    registry = json.loads(
        (BUILTIN_ARTIFACT_ROOT / "index.json").read_text(encoding="utf-8")
    )
    selectors = [
        str(item["selector"])
        for item in registry["campaign_selectors"]
        if item.get("server") == server and item.get("event_id") == event_id
    ]
    assert len(selectors) == 1
    return selectors[0]


def test_current_fixture_preserves_source_identity_and_hashes():
    manifest = current_fixture_manifest()
    _, _, _, revision, _ = current_fixture_identity()
    assert manifest["kind"] == "derived_sharecfg_subset"
    assert manifest["source"]["revision"] == revision
    for relative, expected in manifest["sha256"].items():
        assert hashlib.sha256((FIXTURE / relative).read_bytes()).hexdigest() == expected


def test_current_fixture_compiles_complete_relations_with_independent_oracles():
    loader = _loader()
    candidate = _current(loader)
    spec = EventCompiler(loader).compile(candidate.activity_id)
    activities = loader.load_table("activity_template")
    related = [
        row
        for row in activities.values()
        if int(row.get("mark", 0) or 0) == candidate.mark
    ]
    milestone_activity = next(row for row in related if int(row.get("type", 0)) == 74)
    milestone = loader.load_table("activity_event_pt")[int(milestone_activity["config_id"])]
    shop_activity = activities[int(milestone_activity["config_client"]["shopLinkActID"])]
    source_maps = {
        int(value)
        for row in related
        if int(row.get("type", 0)) == 12
        for value in sharecfg_values(row.get("config_data"))
    }
    shop_row_ids = {int(value) for value in sharecfg_values(shop_activity["config_data"])}
    source_currencies = {
        int(row["resource_type"])
        for row_id, row in loader.load_table("activity_shop_template").items()
        if int(row_id) in shop_row_ids
    } | {int(milestone["pt"])}

    assert len(spec.shop_items) == len(shop_row_ids)
    assert len(spec.milestones) == len(sharecfg_values(milestone["target"]))
    assert {item.id for item in spec.maps} == source_maps
    assert {item.id for item in spec.currencies} == source_currencies
    assert spec.provenance.revision == current_fixture_identity()[3]
    assert not [item for item in spec.findings if item.severity == "error"]
    assert spec.to_dict() == EventCompiler(_loader()).compile(candidate.activity_id).to_dict()
    assert all("siren_source_icons" in item for item in spec.to_dict()["maps"])
    assert all("siren_templates" not in item for item in spec.to_dict()["maps"])


def test_current_compiler_rejects_unlocalized_event_title(monkeypatch):
    loader = _loader()
    candidate = _current(loader)
    compiler = EventCompiler(loader)
    monkeypatch.setattr(compiler, "_linked_name", lambda *_: "未本地化活动")

    spec = compiler.compile(candidate.activity_id)

    assert spec.name == f"Activity {candidate.activity_id}"
    assert any(
        item.code == "source_name_unlocalized" and item.path == "event.name"
        for item in spec.findings
    )


def test_committed_current_artifact_and_production_resolver_use_fixture_result():
    _, server, _, revision, _ = current_fixture_identity()
    compiled = EventCompiler(_loader()).compile(_current(_loader()).activity_id).to_dict()
    artifact = production_artifact()
    assert artifact["role"] == "production"
    assert artifact["event_spec"] == build_artifact(compiled)["event_spec"]

    resolved_spec, resolution = resolve_supplemental_event_spec(artifact)
    plan = load_current_event_plan(
        "test",
        server=server,
        now=artifact_active_time(artifact),
        registry_root=BUILTIN_ARTIFACT_ROOT,
    )
    assert plan["event"]["id"] == compiled["id"]
    assert resolved_spec["provenance"]["base_revision"] == revision
    assert plan["event"]["source"]["revision"] == resolution["composite_revision"]
    assert plan["event"]["source"]["revision"] == resolved_spec["provenance"]["revision"]
    assert plan["event"]["source"]["verified"] is True
    assert plan["source_status"] == "verified"


def _event_specific_backend_tokens() -> set[str]:
    tokens: set[str] = set()
    registry = EventArtifactRegistry()
    for entry in registry.entries:
        spec = entry["artifact"]["event_spec"]
        event_id = str(spec.get("id") or "")
        if ":" in event_id:
            tokens.add(event_id.split(":", 1)[1])
        name = str(spec.get("name") or "").strip()
        if name:
            tokens.add(name)
        for item in spec.get("maps", []):
            map_id = item.get("id")
            if isinstance(map_id, int) and not isinstance(map_id, bool):
                tokens.add(str(map_id))

    compatibility_root = ROOT / "module" / "event_datamine" / "compatibility_data"
    for path in sorted(compatibility_root.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        event_id = str(data.get("event_id") or "")
        if ":" in event_id:
            tokens.add(event_id.split(":", 1)[1])
        for item in data.get("patches", []):
            map_id = item.get("map_id")
            if isinstance(map_id, int) and not isinstance(map_id, bool):
                tokens.add(str(map_id))
            source_path = str(item.get("source_path") or "")
            parts = PurePosixPath(source_path).parts
            if len(parts) >= 2 and parts[0] == "campaign":
                tokens.add(parts[1])

    runtime_root = ROOT / "campaign" / "generated_event"
    for path in sorted(runtime_root.glob("*/runtime.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        event_id = str(data.get("event_id") or "")
        if ":" in event_id:
            tokens.add(event_id.split(":", 1)[1])
        for item in data.get("runtime_maps", []):
            source_path = str(item.get("source_path") or "")
            parts = PurePosixPath(source_path).parts
            if len(parts) >= 2 and parts[0] == "campaign":
                tokens.add(parts[1])

    return {token for token in tokens if token}


def test_production_python_contains_no_event_specific_datamine_hardcode():
    forbidden = _event_specific_backend_tokens()
    assert forbidden
    paths = [
        *sorted((ROOT / "module").rglob("*.py")),
        *sorted((ROOT / "dev_tools").glob("event_datamine*.py")),
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        matches = sorted(token for token in forbidden if token in text)
        assert not matches, f"{path}: event-specific tokens {matches}"


def test_current_builder_is_id_free_and_byte_deterministic(tmp_path: Path):
    _, server, repository, revision, _ = current_fixture_identity()
    selector = _current_selector()
    outputs = []
    for name in ("first", "second"):
        root = tmp_path / name
        build_current_event(
            source_root=FIXTURE,
            server=server,
            campaign_selector=selector,
            repository=repository,
            revision=revision,
            output_root=root / "data",
            asset_root=ROOT / "assets",
            now=artifact_active_time(),
            maps_output=root / "maps",
            verify_git=False,
        )
        outputs.append(
            {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in sorted(root.rglob("*"))
                if path.is_file()
            }
        )
    assert outputs[0] == outputs[1]
    assert any(path.endswith(".py") for path in outputs[0])


def test_current_builder_preflights_artifact_before_writing_maps(tmp_path: Path):
    _, server, repository, revision, _ = current_fixture_identity()
    output_root = tmp_path / "data"
    relative_artifact = production_artifact_path().relative_to(BUILTIN_ARTIFACT_ROOT)
    artifact = output_root / relative_artifact
    artifact.parent.mkdir(parents=True)
    artifact.write_text("reserved", encoding="utf-8")
    maps_output = tmp_path / "maps"

    with pytest.raises(FileExistsError):
        build_current_event(
            source_root=FIXTURE,
            server=server,
            campaign_selector=_current_selector(),
            repository=repository,
            revision=revision,
            output_root=output_root,
            asset_root=ROOT / "assets",
            now=artifact_active_time(),
            maps_output=maps_output,
            verify_git=False,
        )

    assert not maps_output.exists()
