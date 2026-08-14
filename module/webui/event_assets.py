"""Local-only resolver keyed by generated canonical AssetReference paths."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from module.event_datamine.artifact import BUILTIN_ARTIFACT_ROOT
from module.event_datamine.assets import asset_key, validate_asset_catalog

ASSET_ROOT = Path(__file__).resolve().parents[2] / "assets"
ASSET_CATALOG_PATH = BUILTIN_ARTIFACT_ROOT / "assets.json"
PLACEHOLDER_URL = "/static/assets/gui/icon/event-placeholder.svg"
_SAFE_DISPLAY_KIND = re.compile(r"[A-Za-z0-9_-]+")
_DISPLAY_EXTENSIONS = (".png", ".svg", ".webp")


@lru_cache(maxsize=4)
def _catalog(path: str, modified_ns: int, size: int) -> dict[str, str]:
    del modified_ns, size
    data = validate_asset_catalog(json.loads(Path(path).read_text(encoding="utf-8")))
    return {str(key): str(value) for key, value in data["entries"].items()}


def event_asset_url(
    asset: Mapping[str, Any] | None,
    *,
    catalog_path: Path | str = ASSET_CATALOG_PATH,
    asset_root: Path | str = ASSET_ROOT,
) -> str:
    if not isinstance(asset, Mapping):
        return PLACEHOLDER_URL
    key = asset_key(asset)
    if not key:
        return PLACEHOLDER_URL
    try:
        resolved_catalog = Path(catalog_path).resolve()
        stat = resolved_catalog.stat()
        url = _catalog(
            str(resolved_catalog), stat.st_mtime_ns, stat.st_size
        ).get(key, "")
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return PLACEHOLDER_URL
    prefix = "/static/assets/"
    if not url.startswith(prefix):
        return PLACEHOLDER_URL
    relative = Path(url.removeprefix(prefix))
    if relative.is_absolute() or ".." in relative.parts:
        return PLACEHOLDER_URL
    root = Path(asset_root).resolve()
    candidate = (root / relative).resolve()
    if root not in candidate.parents or not candidate.is_file():
        return PLACEHOLDER_URL
    return url


def event_asset_resolved(asset: Mapping[str, Any] | None, **kwargs: Any) -> bool:
    return event_asset_url(asset, **kwargs) != PLACEHOLDER_URL


def _event_shop_display_url(
    asset: Mapping[str, Any], *, asset_root: Path | str
) -> str:
    kind = str(asset.get("kind") or "").strip().lower()
    game_id = str(asset.get("game_id") or "").strip()
    if (
        not kind
        or not _SAFE_DISPLAY_KIND.fullmatch(kind)
        or not game_id.isdecimal()
        or int(game_id) <= 0
    ):
        return ""

    root = Path(asset_root).resolve()
    display_root = (root / "webui" / "event_shop").resolve()
    for extension in _DISPLAY_EXTENSIONS:
        filename = f"{kind}-{int(game_id)}{extension}"
        candidate = (display_root / filename).resolve()
        if display_root not in candidate.parents or not candidate.is_file():
            continue
        return f"/static/assets/webui/event_shop/{filename}"
    return ""


def event_shop_asset_url(
    item_or_asset: Mapping[str, Any] | None,
    *,
    catalog_path: Path | str = ASSET_CATALOG_PATH,
    asset_root: Path | str = ASSET_ROOT,
) -> str:
    if not isinstance(item_or_asset, Mapping):
        return PLACEHOLDER_URL
    nested = item_or_asset.get("asset")
    asset = nested if isinstance(nested, Mapping) else item_or_asset
    display_url = _event_shop_display_url(asset, asset_root=asset_root)
    if display_url:
        return display_url
    return event_asset_url(
        asset,
        catalog_path=catalog_path,
        asset_root=asset_root,
    )


def event_reward_asset_url(
    asset: Mapping[str, Any] | None, **kwargs: Any
) -> str:
    return event_asset_url(asset, **kwargs)
