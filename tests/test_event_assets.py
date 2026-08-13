from pathlib import Path

from module.webui.event_assets import (
    PLACEHOLDER_URL,
    event_reward_asset_url,
    event_shop_asset_url,
)


def test_asset_resolver_uses_local_exact_token_and_safe_fallback(tmp_path: Path):
    folder = tmp_path / "shop" / "event"
    folder.mkdir(parents=True)
    (folder / "Chip.png").write_bytes(b"png")

    assert (
        event_shop_asset_url("Chip", asset_root=tmp_path)
        == "/static/assets/shop/event/Chip.png"
    )
    assert event_shop_asset_url("Missing", asset_root=tmp_path) == PLACEHOLDER_URL
    assert event_shop_asset_url("../secret", asset_root=tmp_path) == PLACEHOLDER_URL
    assert event_reward_asset_url(1, 1).endswith("icon_5.png")
    assert event_reward_asset_url(99, 99) == PLACEHOLDER_URL
