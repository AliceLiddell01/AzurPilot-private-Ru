import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from dev_tools.event_datamine_build import build_current_event
from module.event_datamine.artifact import build_artifact, load_artifact
from module.event_datamine.compiler import EventCompiler
from module.event_datamine.discovery import (
    discover_major_events,
    resolve_current_candidate,
)
from module.event_datamine.map_compiler import _values
from module.event_datamine.source import ShareCfgLoader, SourceSnapshot
from module.webui.event_source import load_current_event_plan

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "event_datamine" / "current_en"
REVISION = "a6b6f81f8fdd1220ef0ad1015a362a7361eb3d91"


def _loader():
    return ShareCfgLoader(
        SourceSnapshot(
            FIXTURE,
            "EN",
            "AzurLaneTools/AzurLaneLuaScripts",
            REVISION,
        )
    )


def _current(loader):
    candidate = resolve_current_candidate(
        discover_major_events(loader),
        server="EN",
        now=datetime(2026, 8, 13, 20),
    )
    assert candidate is not None
    return candidate


def test_current_fixture_preserves_source_identity_and_hashes():
    manifest = json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "derived_sharecfg_subset"
    assert manifest["source"]["revision"] == REVISION
    for relative, expected in manifest["sha256"].items():
        assert hashlib.sha256((FIXTURE / relative).read_bytes()).hexdigest() == expected


def test_current_fixture_compiles_complete_relations_with_independent_oracles():
    loader = _loader()
    candidate = _current(loader)
    assert candidate is not None
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
        for value in _values(row.get("config_data"))
    }
    source_currencies = {
        int(row["resource_type"])
        for row in loader.load_table("activity_shop_template").values()
    } | {int(milestone["pt"])}

    assert len(spec.shop_items) == len(_values(shop_activity["config_data"]))
    assert len(spec.milestones) == len(_values(milestone["target"]))
    assert {item.id for item in spec.maps} == source_maps
    assert {item.id for item in spec.currencies} == source_currencies
    assert spec.provenance.revision == REVISION
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
    compiled = EventCompiler(_loader()).compile(_current(_loader()).activity_id).to_dict()
    artifact = load_artifact(
        ROOT / "module" / "event_datamine" / "data" / "production" / "en-51101.json"
    )
    assert artifact["role"] == "production"
    assert artifact["event_spec"] == build_artifact(compiled)["event_spec"]

    plan = load_current_event_plan(
        "test",
        server="EN",
        now=datetime(2026, 8, 13, 20),
        registry_root=ROOT / "module" / "event_datamine" / "data",
    )
    assert plan["event"]["id"] == compiled["id"]
    assert plan["event"]["name"] != "A Rose on the High Tower"
    assert plan["event"]["source"]["revision"] != REVISION
    assert plan["event"]["source"]["verified"] is True
    assert plan["source_status"] == "verified"


def test_production_python_contains_no_event_specific_datamine_hardcode():
    forbidden = (
        "51101",
        "51104",
        "2050001",
        "5941",
        "1920004",
        "Depths of the Astrarium",
        "StarsCity",
        "event_20260813_cn",
        "event_20250520_cn",
    )
    paths = [
        *sorted((ROOT / "module").rglob("*.py")),
        *sorted((ROOT / "dev_tools").glob("event_datamine*.py")),
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert not any(token in text for token in forbidden), path


def test_current_builder_is_id_free_and_byte_deterministic(tmp_path: Path):
    outputs = []
    for name in ("first", "second"):
        root = tmp_path / name
        build_current_event(
            source_root=FIXTURE,
            server="EN",
            repository="AzurLaneTools/AzurLaneLuaScripts",
            revision=REVISION,
            output_root=root / "data",
            asset_root=ROOT / "assets",
            now=datetime(2026, 8, 13, 20),
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
    assert len([path for path in outputs[0] if path.endswith(".py")]) == 15


def test_current_builder_preflights_artifact_before_writing_maps(tmp_path: Path):
    output_root = tmp_path / "data"
    artifact = output_root / "production" / "en-51101.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("reserved", encoding="utf-8")
    maps_output = tmp_path / "maps"

    with pytest.raises(FileExistsError):
        build_current_event(
            source_root=FIXTURE,
            server="EN",
            repository="AzurLaneTools/AzurLaneLuaScripts",
            revision=REVISION,
            output_root=output_root,
            asset_root=ROOT / "assets",
            now=datetime(2026, 8, 13, 20),
            maps_output=maps_output,
            verify_git=False,
        )

    assert not maps_output.exists()
