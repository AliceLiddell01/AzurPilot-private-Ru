import json
from pathlib import Path

from module.event_datamine.assets import (
    asset_catalog_digest,
    asset_key,
    build_asset_catalog,
)
from module.webui.event_assets import (
    ASSET_CATALOG_PATH,
    ASSET_ROOT,
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


def test_resource_asset_identity_falls_back_to_stable_resource_id():
    assert asset_key({"kind": "resource", "source_path": "", "game_id": "1"}) == "resource-id:1"
    assert asset_key({"kind": "resource", "source_path": "", "game_id": 2}) == "resource-id:2"
    assert asset_key({"kind": "resource", "source_path": "Props/1", "game_id": 1}) == "resource:Props/1"
    assert asset_key({"kind": "resource", "source_path": "", "game_id": 0}) == ""
    assert asset_key({"kind": "item", "source_path": "", "game_id": 15008}) == ""


def test_asset_catalog_prefers_webui_display_asset_and_keeps_scanner_fallback(tmp_path: Path):
    scanner = tmp_path / "shop" / "event"
    display = tmp_path / "webui" / "event_shop"
    scanner.mkdir(parents=True)
    display.mkdir(parents=True)
    (scanner / "Chip.png").write_bytes(b"scanner")
    (display / "Chip.png").write_bytes(b"display")

    catalog = build_asset_catalog(asset_root=tmp_path)
    assert catalog["entries"]["item:Props/15008"] == "/static/assets/webui/event_shop/Chip.png"

    (display / "Chip.png").unlink()
    catalog = build_asset_catalog(asset_root=tmp_path)
    assert catalog["entries"]["item:Props/15008"] == "/static/assets/shop/event/Chip.png"


def test_asset_catalog_reuses_single_display_asset_for_shared_source_identity(tmp_path: Path):
    scanner = tmp_path / "shop" / "event"
    display = tmp_path / "webui" / "event_shop"
    scanner.mkdir(parents=True)
    display.mkdir(parents=True)
    (scanner / "BoxT4.png").write_bytes(b"scanner")
    (display / "item-30014.png").write_bytes(b"display")

    catalog = build_asset_catalog(asset_root=tmp_path)

    assert catalog["entries"]["item:Props/30004"] == (
        "/static/assets/webui/event_shop/item-30014.png"
    )


def test_asset_catalog_uses_scanner_fallback_when_shared_source_has_multiple_displays(tmp_path: Path):
    scanner = tmp_path / "shop" / "event"
    display = tmp_path / "webui" / "event_shop"
    scanner.mkdir(parents=True)
    display.mkdir(parents=True)
    (scanner / "BoxT4.png").write_bytes(b"scanner")
    (display / "item-30014.png").write_bytes(b"eagle")
    (display / "item-30024.png").write_bytes(b"royal")

    catalog = build_asset_catalog(asset_root=tmp_path)

    assert catalog["entries"]["item:Props/30004"] == (
        "/static/assets/shop/event/BoxT4.png"
    )


def test_committed_asset_catalog_matches_repository_assets():
    committed = json.loads(ASSET_CATALOG_PATH.read_text(encoding="utf-8"))

    assert committed == build_asset_catalog(asset_root=ASSET_ROOT)
