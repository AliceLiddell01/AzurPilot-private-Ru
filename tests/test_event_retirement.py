import json
from datetime import datetime
from pathlib import Path

import pytest

from module.event_datamine.artifact import build_artifact, write_artifact
from module.event_datamine.assets import write_asset_catalog
from module.event_datamine.registry import EventArtifactRegistry, write_registry
from module.event_datamine.retirement import (
    EventOverlayRetirementError,
    retire_event_overlay,
)
from module.event_datamine.supplemental import event_supplemental_slug


def _artifact(
    event_id: str,
    package: str,
    *,
    farm_start: str,
    farm_end: str,
    shop_end: str,
):
    return build_artifact(
        {
            "id": event_id,
            "server": "EN",
            "farm_start": farm_start,
            "farm_end": farm_end,
            "shop_end": shop_end,
            "source_status": "verified",
            "provenance": {"revision": "a" * 40},
            "currencies": [
                {
                    "asset": {
                        "kind": "resource",
                        "source_path": "sharecfg/resource/1",
                        "game_id": 1,
                    }
                }
            ],
        },
        metadata={
            "generated_package": package,
            "generated_maps": [
                {
                    "module": f"{package}/a1.py",
                }
            ],
        },
    )


def _write_package(root: Path, package: str, event_id: str) -> Path:
    directory = root / package
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text("", encoding="utf-8")
    (directory / "a1.py").write_text("VALUE = 1\n", encoding="utf-8")
    (directory / "runtime.json").write_text(
        json.dumps({"event_id": event_id}),
        encoding="utf-8",
    )
    return directory


def _write_overlay_side_data(
    event_id: str,
    *,
    supplemental_root: Path,
    compatibility_root: Path,
) -> tuple[Path, Path]:
    supplemental = supplemental_root / event_supplemental_slug(event_id)
    supplemental.mkdir(parents=True)
    (supplemental / "manifest.json").write_text(
        json.dumps({"event_id": event_id}),
        encoding="utf-8",
    )
    (supplemental / "part.json").write_text("[]", encoding="utf-8")

    compatibility = compatibility_root / f"{event_supplemental_slug(event_id)}.json"
    compatibility.parent.mkdir(parents=True, exist_ok=True)
    compatibility.write_text(
        json.dumps({"event_id": event_id}),
        encoding="utf-8",
    )
    return supplemental, compatibility


def test_retirement_removes_only_expired_overlay_and_preserves_shared_assets(
    tmp_path: Path,
):
    artifact_root = tmp_path / "data"
    asset_root = tmp_path / "assets"
    generated_root = tmp_path / "generated"
    supplemental_root = tmp_path / "supplemental"
    compatibility_root = tmp_path / "compatibility"

    shared_asset = asset_root / "webui" / "event_shop" / "resource-1.png"
    shared_asset.parent.mkdir(parents=True)
    shared_asset.write_bytes(b"shared-asset")

    event_a = "en:101"
    event_b = "en:202"
    artifact_a = artifact_root / "production" / "a.json"
    artifact_b = artifact_root / "production" / "b.json"
    artifact_a.parent.mkdir(parents=True)

    write_artifact(
        artifact_a,
        _artifact(
            event_a,
            "en_a",
            farm_start="2026-07-01",
            farm_end="2026-07-20",
            shop_end="2026-07-27",
        ),
    )
    write_artifact(
        artifact_b,
        _artifact(
            event_b,
            "en_b",
            farm_start="2026-08-01",
            farm_end="2026-08-20",
            shop_end="2026-08-27",
        ),
    )
    write_registry(
        artifact_root,
        campaign_selector={
            "server": "EN",
            "selector": "event_a",
            "event_id": event_a,
        },
    )
    write_registry(
        artifact_root,
        campaign_selector={
            "server": "EN",
            "selector": "event_b",
            "event_id": event_b,
        },
    )
    write_asset_catalog(artifact_root, asset_root=asset_root)

    package_a = _write_package(generated_root, "en_a", event_a)
    package_b = _write_package(generated_root, "en_b", event_b)
    supplemental_a, compatibility_a = _write_overlay_side_data(
        event_a,
        supplemental_root=supplemental_root,
        compatibility_root=compatibility_root,
    )
    supplemental_b, compatibility_b = _write_overlay_side_data(
        event_b,
        supplemental_root=supplemental_root,
        compatibility_root=compatibility_root,
    )
    unrelated = artifact_root / "keep.txt"
    unrelated.write_text("keep\n", encoding="utf-8")

    result = retire_event_overlay(
        event_a,
        now=datetime(2026, 8, 10),
        artifact_root=artifact_root,
        asset_root=asset_root,
        generated_root=generated_root,
        supplemental_root=supplemental_root,
        compatibility_root=compatibility_root,
    )

    assert result["event_id"] == event_a
    assert result["lifecycle"] == "expired"
    assert result["generated_package"] == "en_a"
    assert result["static_assets_removed"] is False

    assert not artifact_a.exists()
    assert artifact_b.is_file()
    assert not package_a.exists()
    assert package_b.is_dir()
    assert not supplemental_a.exists()
    assert supplemental_b.is_dir()
    assert not compatibility_a.exists()
    assert compatibility_b.is_file()
    assert unrelated.read_text(encoding="utf-8") == "keep\n"

    registry = EventArtifactRegistry(artifact_root)
    assert registry.resolve_campaign_selector("EN", "event_a") is None
    resolved_b = registry.resolve_campaign_selector("EN", "event_b")
    assert resolved_b is not None
    assert resolved_b["event_spec"]["id"] == event_b

    assert shared_asset.read_bytes() == b"shared-asset"
    asset_catalog = json.loads(
        (artifact_root / "assets.json").read_text(encoding="utf-8")
    )
    assert asset_catalog["entries"]["resource:sharecfg/resource/1"] == (
        "/static/assets/webui/event_shop/resource-1.png"
    )


def test_retirement_rejects_non_expired_overlay_without_mutation(tmp_path: Path):
    artifact_root = tmp_path / "data"
    asset_root = tmp_path / "assets"
    generated_root = tmp_path / "generated"
    supplemental_root = tmp_path / "supplemental"
    compatibility_root = tmp_path / "compatibility"

    event_id = "en:303"
    artifact_path = artifact_root / "production" / "active.json"
    artifact_path.parent.mkdir(parents=True)
    write_artifact(
        artifact_path,
        _artifact(
            event_id,
            "en_active",
            farm_start="2026-08-01",
            farm_end="2026-08-20",
            shop_end="2026-08-27",
        ),
    )
    write_registry(
        artifact_root,
        campaign_selector={
            "server": "EN",
            "selector": "event_active",
            "event_id": event_id,
        },
    )
    package = _write_package(generated_root, "en_active", event_id)
    supplemental, compatibility = _write_overlay_side_data(
        event_id,
        supplemental_root=supplemental_root,
        compatibility_root=compatibility_root,
    )
    before_artifact = artifact_path.read_bytes()
    before_index = (artifact_root / "index.json").read_bytes()

    with pytest.raises(
        EventOverlayRetirementError,
        match="нельзя удалить в фазе 'active'",
    ):
        retire_event_overlay(
            event_id,
            now=datetime(2026, 8, 10),
            artifact_root=artifact_root,
            asset_root=asset_root,
            generated_root=generated_root,
            supplemental_root=supplemental_root,
            compatibility_root=compatibility_root,
        )

    assert artifact_path.read_bytes() == before_artifact
    assert (artifact_root / "index.json").read_bytes() == before_index
    assert package.is_dir()
    assert supplemental.is_dir()
    assert compatibility.is_file()
