"""Local-only resolver keyed by generated canonical AssetReference paths."""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from module.event_datamine.artifact import BUILTIN_ARTIFACT_ROOT
from module.event_datamine.assets import asset_key, validate_asset_catalog

ASSET_ROOT = Path(__file__).resolve().parents[2] / "assets"
ASSET_CATALOG_PATH = BUILTIN_ARTIFACT_ROOT / "assets.json"
PLACEHOLDER_URL = "/static/assets/gui/icon/event-placeholder.svg"


@lru_cache(maxsize=4)
def _catalog(path: str) -> dict[str, str]:
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
        url = _catalog(str(Path(catalog_path).resolve())).get(key, "")
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


def event_asset_resolved(asset: Mapping[str, Any] | None) -> bool:
    return event_asset_url(asset) != PLACEHOLDER_URL


def event_shop_asset_url(
    asset: Mapping[str, Any] | None, **kwargs: Any
) -> str:
    return event_asset_url(asset, **kwargs)


def event_reward_asset_url(
    asset: Mapping[str, Any] | None, **kwargs: Any
) -> str:
    return event_asset_url(asset, **kwargs)
