"""Сгенерированный каталог соответствий canonical source path локальным статическим ассетам."""

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
_SAFE_STATIC_URL = re.compile(r"/static/assets/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*")
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_-]+")
_DISPLAY_EXTENSIONS = (".png", ".svg", ".webp")


def _safe_source_path(source_path: str) -> bool:
    """Проверить canonical source path без родительских сегментов."""

    return bool(
        source_path
        and _SAFE_SOURCE_PATH.fullmatch(source_path)
        and ".." not in source_path.split("/")
    )


def _safe_static_asset_url(url: str) -> bool:
    """Проверить локальный static URL без обхода каталога."""

    return bool(
        url
        and _SAFE_STATIC_URL.fullmatch(url)
        and ".." not in url.split("/")
        and "\\" not in url
    )


def asset_key(asset: Mapping[str, Any]) -> str:
    kind = str(asset.get("kind") or "").strip()
    source_path = str(asset.get("source_path") or "").strip()
    if not kind:
        return ""
    if source_path:
        if not _safe_source_path(source_path):
            return ""
        return f"{kind}:{source_path}"
    if kind == "resource":
        game_id = str(asset.get("game_id") or "").strip()
        if game_id.isdecimal() and int(game_id) > 0:
            return f"resource-id:{int(game_id)}"
    return ""


def asset_catalog_digest(data: Mapping[str, Any]) -> str:
    clean = dict(data)
    clean.pop("digest", None)
    return sha256(canonical_json(clean).encode("utf-8")).hexdigest()


def _display_asset_url(asset: Mapping[str, Any], local_root: Path) -> str:
    kind = str(asset.get("kind") or "").strip().lower()
    game_id = str(asset.get("game_id") or "").strip()
    if (
        not kind
        or not _SAFE_TOKEN.fullmatch(kind)
        or not game_id.isdecimal()
        or int(game_id) <= 0
    ):
        return ""

    display_root = (local_root / "webui" / "event_shop").resolve()
    for extension in _DISPLAY_EXTENSIONS:
        filename = f"{kind}-{int(game_id)}{extension}"
        candidate = (display_root / filename).resolve()
        if display_root in candidate.parents and candidate.is_file():
            return f"/static/assets/webui/event_shop/{filename}"
    return ""


def build_asset_catalog(
    artifact_root: Path | str = BUILTIN_ARTIFACT_ROOT,
    *,
    asset_root: Path | str,
) -> dict[str, Any]:
    """Построить соответствия по canonical-путям AssetReference, а не по game ID.

    Существующие шаблоны EventShop допускаются только на этапе разработки/сборки
    и только если скомпилированная строка уже содержит безопасный legacy runtime-токен.
    Runtime использует сгенерированное соответствие canonical path и никогда не
    считает такой токен идентичностью ассета.
    """

    data_root = Path(artifact_root).resolve()
    local_root = Path(asset_root).resolve()
    display_candidates: dict[str, set[str]] = {}
    fallback_candidates: dict[str, set[str]] = {}
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

            display_url = _display_asset_url(asset, local_root)
            if display_url:
                display_candidates.setdefault(key, set()).add(display_url)

            candidates = (
                (
                    local_root / "webui" / "event_shop" / f"{token}.png",
                    f"/static/assets/webui/event_shop/{token}.png",
                    local_root / "webui" / "event_shop",
                ),
                (
                    local_root / "shop" / "event" / f"{token}.png",
                    f"/static/assets/shop/event/{token}.png",
                    local_root / "shop" / "event",
                ),
            )
            for candidate, url, expected_root in candidates:
                candidate = candidate.resolve()
                expected = expected_root.resolve()
                if expected in candidate.parents and candidate.is_file():
                    fallback_candidates.setdefault(key, set()).add(url)
                    break

    mappings: dict[str, str] = {}
    all_keys = sorted(set(display_candidates) | set(fallback_candidates))
    for key in all_keys:
        displays = display_candidates.get(key, set())
        fallbacks = fallback_candidates.get(key, set())
        if len(fallbacks) > 1:
            raise ValueError(f"Конфликт local asset для canonical key {key}")
        if len(displays) == 1:
            mappings[key] = next(iter(displays))
        elif len(displays) > 1:
            # Несколько display-файлов не дают однозначной canonical identity.
            # Единственный scanner fallback остаётся доказанным общим ассетом;
            # без него ключ намеренно не публикуется.
            if len(fallbacks) == 1:
                mappings[key] = next(iter(fallbacks))
        elif len(fallbacks) == 1:
            mappings[key] = next(iter(fallbacks))

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
        key_text = str(key)
        url_text = str(url)
        if not key_text or not _safe_static_asset_url(url_text):
            raise ValueError("Event asset catalog содержит небезопасную запись")
        if ":" not in key_text or not _safe_source_path(key_text.split(":", 1)[1]):
            raise ValueError("Event asset catalog содержит небезопасный source path")
    return result


def write_asset_catalog(
    artifact_root: Path | str = BUILTIN_ARTIFACT_ROOT,
    *,
    asset_root: Path | str,
) -> Path:
    root = Path(artifact_root)
    target = root / EVENT_ASSET_CATALOG_NAME
    data = validate_asset_catalog(build_asset_catalog(root, asset_root=asset_root))
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
