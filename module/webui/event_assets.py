"""Local-only asset resolver for Event UI."""

from __future__ import annotations

import re
from pathlib import Path

ASSET_ROOT = Path(__file__).resolve().parents[2] / "assets"
PLACEHOLDER_URL = "/static/assets/gui/icon/event-placeholder.svg"
REWARD_ICON_BY_GAME_ID = {
    (1, 1): "/static/assets/gui/icon/icon_5.png",
    (1, 2): "/static/assets/gui/icon/icon_4.png",
    (2, 15008): "/static/assets/gui/icon/icon_3.png",
}


def event_shop_asset_url(filter_token: str, *, asset_root: Path = ASSET_ROOT) -> str:
    token = str(filter_token or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        return PLACEHOLDER_URL
    candidate = (asset_root / "shop" / "event" / f"{token}.png").resolve()
    expected_root = (asset_root / "shop" / "event").resolve()
    if expected_root not in candidate.parents or not candidate.is_file():
        return PLACEHOLDER_URL
    return f"/static/assets/shop/event/{token}.png"


def event_reward_asset_url(reward_type: int, reward_id: int) -> str:
    return REWARD_ICON_BY_GAME_ID.get(
        (int(reward_type), int(reward_id)), PLACEHOLDER_URL
    )
