"""Generated canonical-source-path to local-static-asset catalog."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from deploy.atomic import file_remove, file_write, replace_tmp, to_tmp_file
from module.event_datamine.artifact import (
    BUILTIN_ARTIFACT_ROOT,
    canonical_json,
    load_artifact,
)

EVENT_ASSET_CATALOG_SCHEMA_VERSION = 1
EVENT_ASSET_CATALOG_NAME = "assets.json"
_SAFE_SOURCE_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*")
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_-]+")


def asset_key(asset: Mapping[str, Any]) -> str:
    kind = str(asset.get("kind") or "").strip()
    source_path = str(asset.get("source_path") or "").strip()
    if not kind or not source_path or not _SAFE_SOURCE_PATH.fullmatch(source_path):
        return ""
    return f"{kind}:{source_path}"


def asset_catalog_digest(data: Mapping[str, Any]) -> str:
    clean = dict(data)
    clean.pop("digest", None)
    return sha256(canonical_json(clean).encode("utf-8")).hexdigest()


def build_asset_catalog(
    artifact_root: Path | str = BUILTIN_ARTIFACT_ROOT,
    *,
    asset_root: Path | str,
) -> dict[str, Any]:
    """Build mappings from canonical AssetReference paths, never game IDs.

    Existing EventShop templates are admitted only at developer/build time and
    only when the compiled row already declares a safe legacy runtime token.
    Runtime consumes the generated canonical-path mapping and never treats the
    token as an asset identity.
    """

    data_root = Path(artifact_root).resolve()
    local_root = Path(asset_root).resolve()
    mappings: dict[str, str] = {}
    for path in sorted(data_root.rglob("*.json")):
        if path.name in {EVENT_ASSET_CATALOG_NAME, "index.json"}:
            continue
        try:
            artifact = load_artifact(path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"Некорректный Event artifact {path}") from exc
        spec = artifact["event_spec"]
        for item in spec.get("shop_items", []):
            if not isinstance(item, Mapping):
                continue
            asset = item.get("asset")
            if not isinstance(asset, Mapping):
                continue
            key = asset_key(asset)
            token = str(item.get("event_shop_filter") or "")
            if not key or not _SAFE_TOKEN.fullmatch(token):
                continue
            candidate = (local_root / "shop" / "event" / f"{token}.png").resolve()
            expected = (local_root / "shop" / "event").resolve()
            if expected not in candidate.parents or not candidate.is_file():
                continue
            url = f"/static/assets/shop/event/{token}.png"
            previous = mappings.setdefault(key, url)
            if previous != url:
                raise ValueError(f"Конфликт local asset для canonical key {key}")
    result = {
        "asset_catalog_schema_version": EVENT_ASSET_CATALOG_SCHEMA_VERSION,
        "entries": dict(sorted(mappings.items())),
    }
    result["digest"] = asset_catalog_digest(result)
    return result


def validate_asset_catalog(data: Any) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise ValueError("Event asset catalog должен быть JSON object")
    result = dict(data)
    if (
        int(result.get("asset_catalog_schema_version", 0) or 0)
        != EVENT_ASSET_CATALOG_SCHEMA_VERSION
    ):
        raise ValueError("Неподдерживаемая версия Event asset catalog")
    if str(result.get("digest") or "") != asset_catalog_digest(result):
        raise ValueError("Digest Event asset catalog не совпадает")
    entries = result.get("entries")
    if not isinstance(entries, Mapping):
        raise ValueError("Event asset catalog не содержит entries")
    for key, url in entries.items():
        if not str(key) or not str(url).startswith("/static/assets/"):
            raise ValueError("Event asset catalog содержит небезопасную запись")
        if ":" not in str(key) or ".." in str(key).split(":", 1)[1].split("/"):
            raise ValueError("Event asset catalog содержит небезопасный source path")
    return result


def write_asset_catalog(
    artifact_root: Path | str = BUILTIN_ARTIFACT_ROOT,
    *,
    asset_root: Path | str,
) -> Path:
    root = Path(artifact_root)
    target = root / EVENT_ASSET_CATALOG_NAME
    data = build_asset_catalog(root, asset_root=asset_root)
    temp = to_tmp_file(str(target))
    try:
        file_write(
            temp,
            json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        replace_tmp(temp, str(target))
    except BaseException:
        try:
            file_remove(temp)
        except OSError:
            pass
        raise
    return target
