import json
from pathlib import Path, PurePosixPath

from dev_tools.event_datamine_build import build_current_event
from module.event_datamine.artifact import BUILTIN_ARTIFACT_ROOT
from module.event_datamine.runtime_policy import (
    RUNTIME_POLICY_SCHEMA_VERSION,
    load_generated_runtime_policy,
    runtime_map_policies,
)
from module.map_detection import utils_assets
from module.template import assets as template_assets
from tests.event_fixture_helpers import (
    CURRENT_FIXTURE_ROOT,
    ROOT,
    artifact_active_time,
    current_fixture_identity,
    current_generated_package_parts,
    production_artifact,
    production_artifact_path,
)

FIXTURE = CURRENT_FIXTURE_ROOT


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


def _build(tmp_path: Path, **kwargs):
    _, server, repository, revision, _ = current_fixture_identity()
    return build_current_event(
        source_root=FIXTURE,
        server=server,
        campaign_selector=_current_selector(),
        repository=repository,
        revision=revision,
        output_root=tmp_path / "data",
        asset_root=ROOT / "assets",
        now=artifact_active_time(),
        maps_output=tmp_path / "maps",
        verify_git=False,
        **kwargs,
    )


def _built_artifact(tmp_path: Path) -> dict:
    artifact_path = production_artifact_path()
    relative = artifact_path.relative_to(artifact_path.parents[1])
    path = tmp_path / "data" / relative
    return json.loads(path.read_text(encoding="utf-8"))


def _generated_package_from_metadata(artifact: dict) -> tuple[str, ...]:
    parents = {
        PurePosixPath(item["module"]).parent.parts
        for item in artifact["metadata"]["generated_maps"]
        if item.get("module")
    }
    assert len(parents) == 1
    return next(iter(parents))


def test_runtime_policy_is_typed_and_matches_artifact_runtime_inventory():
    artifact = production_artifact()
    package_parts = current_generated_package_parts()
    policy = load_generated_runtime_policy(package_parts)

    assert policy is not None
    assert policy["runtime_policy_schema_version"] == RUNTIME_POLICY_SCHEMA_VERSION
    assert policy["event_id"] == artifact["event_spec"]["id"]
    assert "/" in policy["map_evidence"]["repository"]
    assert len(policy["map_evidence"]["revision"]) == 40

    maps = runtime_map_policies(policy)
    assert maps
    records = {
        item["map_id"]: item
        for item in artifact["metadata"]["generated_maps"]
        if item.get("runtime_status") == "verified"
    }
    assert set(records) == set(maps)
    for map_id, runtime in maps.items():
        record = records[map_id]
        assert record["chapter_name"] == runtime.chapter_name
        assert PurePosixPath(record["module"]).parent.parts == package_parts
        assert runtime.boss_clear is not None
        assert runtime.camera_calibration is not None
        assert runtime.detector_calibration is not None
        assert runtime.battle_plan is not None


def test_current_builder_applies_runtime_policy_without_leaking_source_icons(tmp_path: Path):
    result = _build(tmp_path)
    artifact = _built_artifact(tmp_path)
    package_parts = _generated_package_from_metadata(artifact)
    policy = load_generated_runtime_policy(package_parts)
    assert policy is not None
    runtime_maps = runtime_map_policies(policy)

    records = {
        item["map_id"]: item
        for item in artifact["metadata"]["generated_maps"]
        if item.get("runtime_status") == "verified"
    }
    assert result["runtime_map_count"] == len(records) == len(runtime_maps)

    source_maps = {item["id"]: item for item in artifact["event_spec"]["maps"]}
    for map_id, record in records.items():
        runtime = runtime_maps[map_id]
        module_path = tmp_path / "maps" / record["module"]
        assert module_path.is_file()
        content = module_path.read_text(encoding="utf-8")
        assert "class Config" in content
        assert "class Campaign" in content

        siren = runtime.siren_recognition
        if siren is not None:
            for template in siren.templates:
                assert template in content
            source_icons = source_maps[map_id].get("siren_source_icons", [])
            for icon in source_icons:
                if icon not in siren.templates:
                    assert str(icon) not in content


def test_maps_without_runtime_policy_fail_closed(tmp_path: Path):
    empty_policy_root = tmp_path / "empty-policy"
    empty_policy_root.mkdir()
    result = _build(tmp_path, runtime_policy_root=empty_policy_root)
    artifact = _built_artifact(tmp_path)
    records = artifact["metadata"]["generated_maps"]
    source_maps = {item["id"]: item for item in artifact["event_spec"]["maps"]}

    assert result["runtime_map_count"] == 0
    for record in records:
        map_id = record["map_id"]
        assert record["source_status"] == "verified"
        assert record["runtime_status"] == "unsupported"
        assert record["module"] == ""
        raw = source_maps[map_id]
        has_siren = any(
            row.get("siren", 0)
            for key in ("spawn_data", "spawn_data_loop")
            for row in (raw.get(key) or [])
        )
        expected_reason = "siren_recognition_missing" if has_siren else "boss_clear_missing"
        assert record["runtime_reason"] == expected_reason

    generated_root = tmp_path / "maps"
    generated_python = [
        path
        for path in generated_root.rglob("*.py")
        if path.name != "__init__.py"
    ]
    assert generated_python == []


def test_artifact_marks_source_maps_without_runtime_evidence_unsupported():
    artifact = production_artifact()
    policy = load_generated_runtime_policy(current_generated_package_parts())
    assert policy is not None
    runtime_ids = set(runtime_map_policies(policy))
    records = {
        item["map_id"]: item for item in artifact["metadata"]["generated_maps"]
    }

    unsupported = [
        record
        for map_id, record in records.items()
        if record["source_status"] == "verified" and map_id not in runtime_ids
    ]
    assert unsupported
    for record in unsupported:
        assert record["runtime_status"] == "unsupported"
        assert record["module"] == ""
        assert record["runtime_reason"]


def test_event_siren_templates_use_canonical_generated_asset_registry():
    policy = load_generated_runtime_policy(current_generated_package_parts())
    assert policy is not None
    templates = {
        name
        for runtime in runtime_map_policies(policy).values()
        if runtime.siren_recognition is not None
        for name in runtime.siren_recognition.templates
    }
    assert templates

    for name in templates:
        attribute = f"TEMPLATE_SIREN_{name}"
        assert not hasattr(utils_assets, attribute)
        template = getattr(template_assets, attribute)
        path = Path(template.file)
        if not path.is_absolute():
            path = ROOT / path
        assert path.is_file()
        assert path.suffix.lower() in {".gif", ".png"}
