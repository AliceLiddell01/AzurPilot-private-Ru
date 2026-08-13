import json
from pathlib import Path

from module.event_datamine.assets import asset_catalog_digest
from module.webui.event_assets import (
    PLACEHOLDER_URL,
    event_asset_resolved,
    event_reward_asset_url,
    event_shop_asset_url,
)


def test_asset_resolver_uses_local_exact_token_and_safe_fallback(tmp_path: Path):
    folder = tmp_path / "shop" / "event"
    folder.mkdir(parents=True)
    (folder / "Chip.png").write_bytes(b"png")
    catalog = {
        "asset_catalog_schema_version": 1,
        "entries": {"item:Props/15008": "/static/assets/shop/event/Chip.png"},
    }
    catalog["digest"] = asset_catalog_digest(catalog)
    catalog_path = tmp_path / "assets.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    asset = {"kind": "item", "source_path": "Props/15008", "game_id": "15008"}

    assert (
        event_shop_asset_url(
            asset, catalog_path=catalog_path, asset_root=tmp_path
        )
        == "/static/assets/shop/event/Chip.png"
    )
    assert (
        event_shop_asset_url(
            {"kind": "item", "source_path": "missing"},
            catalog_path=catalog_path,
            asset_root=tmp_path,
        )
        == PLACEHOLDER_URL
    )
    assert (
        event_shop_asset_url(
            {"kind": "item", "source_path": "../secret"},
            catalog_path=catalog_path,
            asset_root=tmp_path,
        )
        == PLACEHOLDER_URL
    )
    assert (
        event_reward_asset_url(
            asset, catalog_path=catalog_path, asset_root=tmp_path
        )
        == "/static/assets/shop/event/Chip.png"
    )
    assert event_asset_resolved(
        asset, catalog_path=catalog_path, asset_root=tmp_path
    )


def test_asset_catalog_cache_invalidates_when_file_changes(tmp_path: Path):
    folder = tmp_path / "shop" / "event"
    folder.mkdir(parents=True)
    (folder / "Chip.png").write_bytes(b"png")
    (folder / "Array.png").write_bytes(b"png2")
    catalog_path = tmp_path / "assets.json"

    def write(entries):
        catalog = {"asset_catalog_schema_version": 1, "entries": entries}
        catalog["digest"] = asset_catalog_digest(catalog)
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

    asset = {"kind": "item", "source_path": "Props/15008"}
    write({"item:Props/15008": "/static/assets/shop/event/Chip.png"})
    assert event_shop_asset_url(
        asset, catalog_path=catalog_path, asset_root=tmp_path
    ).endswith("/Chip.png")

    write({"item:Props/15008": "/static/assets/shop/event/Array.png"})
    assert event_shop_asset_url(
        asset, catalog_path=catalog_path, asset_root=tmp_path
    ).endswith("/Array.png")
