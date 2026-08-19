import json
from pathlib import Path

from module.event_datamine.artifact import build_artifact, write_artifact
from module.event_datamine.assets import build_asset_catalog
from module.webui.event_assets import PLACEHOLDER_URL, event_asset_url


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "module" / "event_datamine" / "data" / "production" / "en-51101.json"


def test_current_en_currency_uses_supplied_local_asset():
    data = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    currency = data["event_spec"]["currencies"][0]

    resolved = event_asset_url(currency["asset"])

    assert resolved != PLACEHOLDER_URL
    assert resolved == "/static/assets/webui/event_shop/activity_currency-741.png"
    assert (ROOT / "assets" / "webui" / "event_shop" / "activity_currency-741.png").is_file()


def test_asset_catalog_collects_currency_and_milestone_display_assets(tmp_path: Path):
    artifact_root = tmp_path / "data"
    asset_root = tmp_path / "assets"
    display_root = asset_root / "webui" / "event_shop"
    display_root.mkdir(parents=True)
    (display_root / "activity_currency-741.png").write_bytes(b"currency")
    (display_root / "item-54005.png").write_bytes(b"reward")

    artifact = build_artifact(
        {
            "id": "en:test",
            "currencies": [
                {
                    "asset": {
                        "kind": "activity_currency",
                        "game_id": "741",
                        "source_path": "Props/66064",
                    }
                }
            ],
            "shop_items": [],
            "milestones": [
                {
                    "rewards": [
                        {
                            "asset": {
                                "kind": "item",
                                "game_id": "54005",
                                "source_path": "Props/54002",
                            }
                        }
                    ]
                }
            ],
        }
    )
    write_artifact(artifact_root / "event.json", artifact)

    catalog = build_asset_catalog(artifact_root, asset_root=asset_root)

    assert catalog["entries"] == {
        "activity_currency:Props/66064": (
            "/static/assets/webui/event_shop/activity_currency-741.png"
        ),
        "item:Props/54002": "/static/assets/webui/event_shop/item-54005.png",
    }
