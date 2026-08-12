from module.webui.event_manifest import event_plan_from_manifest, merge_event_manifest
from module.webui.event_plan import empty_event_plan


def _manifest():
    return {
        "event": {
            "name": "Miracle by Midnight",
            "server": "EN",
            "farm_end": "2026-07-08 23:59:59",
            "shop_end": "2026-07-15 23:59:59",
        },
        "stages": [{"name": "HT3", "points": 180}],
        "daily": [{"name": "Daily", "points": 300}],
        "extra": [{"name": "SP", "points": 800}],
        "shop_items": [
            {"name": "Cube", "price": 100, "stock": 5, "filter": "Cube"}
        ],
    }


def test_manifest_adapter_is_provider_neutral_and_tracks_provenance():
    plan = event_plan_from_manifest(
        _manifest(),
        source_kind="azurlane_lua",
        verified=True,
        revision="abc123",
        updated_at="2026-07-01 12:00:00",
    )

    assert plan["event"]["name"] == "Miracle by Midnight"
    assert plan["event"]["source"] == {
        "kind": "azurlane_lua",
        "verified": True,
        "updated_at": "2026-07-01 12:00:00",
        "revision": "abc123",
    }
    assert plan["shop_items"][0]["selected"] == 5


def test_manifest_refresh_preserves_local_progress_and_compatible_shop_choice():
    old = empty_event_plan()
    old["progress"]["current_pt"] = 12345
    old["shop_items"] = [
        {"name": "Cube", "price": 100, "stock": 5, "selected": 2, "filter": "Cube"}
    ]

    merged = merge_event_manifest(
        old,
        _manifest(),
        source_kind="azurlane_lua",
        verified=True,
        revision="next",
        updated_at="2026-07-02 12:00:00",
    )

    assert merged["progress"]["current_pt"] == 12345
    assert merged["shop_items"][0]["selected"] == 2
    assert merged["event"]["source"]["revision"] == "next"
