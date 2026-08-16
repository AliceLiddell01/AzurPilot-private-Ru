from __future__ import annotations

import json
from pathlib import Path

from module.event_datamine.assets import asset_catalog_digest
from module.webui.event_assets import event_asset_url


def _write_catalog(path: Path, entries: dict[str, str]) -> None:
    data = {
        "asset_catalog_schema_version": 1,
        "entries": entries,
    }
    data["digest"] = asset_catalog_digest(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def test_webui_display_identity_distinguishes_shared_scanner_source(tmp_path: Path):
    asset_root = tmp_path / "assets"
    display_root = asset_root / "webui" / "event_shop"
    display_root.mkdir(parents=True)
    (display_root / "item-30014.svg").write_text("<svg/>", encoding="utf-8")
    (display_root / "item-30024.svg").write_text("<svg/>", encoding="utf-8")

    eagle = {
        "kind": "item",
        "game_id": "30014",
        "source_path": "Props/30004",
    }
    royal = {
        "kind": "item",
        "game_id": "30024",
        "source_path": "Props/30004",
    }

    assert event_asset_url(eagle, asset_root=asset_root) == (
        "/static/assets/webui/event_shop/item-30014.svg"
    )
    assert event_asset_url(royal, asset_root=asset_root) == (
        "/static/assets/webui/event_shop/item-30024.svg"
    )


def test_webui_display_identity_keeps_canonical_fallback(tmp_path: Path):
    asset_root = tmp_path / "assets"
    scanner_root = asset_root / "shop" / "event"
    scanner_root.mkdir(parents=True)
    (scanner_root / "BoxT4.png").write_bytes(b"scanner")
    catalog_path = tmp_path / "data" / "assets.json"
    _write_catalog(
        catalog_path,
        {"item:Props/30004": "/static/assets/shop/event/BoxT4.png"},
    )

    asset = {
        "kind": "item",
        "game_id": "30034",
        "source_path": "Props/30004",
    }

    assert event_asset_url(
        asset,
        catalog_path=catalog_path,
        asset_root=asset_root,
    ) == "/static/assets/shop/event/BoxT4.png"
