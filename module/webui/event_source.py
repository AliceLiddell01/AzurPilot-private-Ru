"""Проекция immutable EventSpec + отдельно хранимой пользовательской политики."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from deploy.atomic import (
    atomic_read_text,
    atomic_replace,
    file_remove,
    file_write,
    replace_tmp,
    to_tmp_file,
)
from module.event_datamine.artifact import load_builtin_artifact
from module.logger import logger
from module.webui.event_plan import EVENT_PLAN_ROOT, empty_event_plan, load_event_plan

EVENT_USER_STATE_SCHEMA_VERSION = 1
EVENT_USER_STATE_ROOT = Path("./config/state/event_user_state")


def _safe_instance_key(instance: str) -> str:
    raw = str(instance or "alas")
    slug = re.sub(r"[^0-9A-Za-z_.-]+", "_", raw).strip("._-") or "alas"
    digest = sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{slug[:48]}-{digest}"


def event_user_state_path(
    instance: str, root: Path | str = EVENT_USER_STATE_ROOT
) -> Path:
    return Path(root) / f"{_safe_instance_key(instance)}.json"


def empty_event_user_state() -> dict[str, Any]:
    return {
        "schema_version": EVENT_USER_STATE_SCHEMA_VERSION,
        "source_event_id": "en:5941",
        "explicit_empty": False,
        "progress": {"current_pt": 0, "pt_mode": "auto"},
        "shop_selections": {},
        "recurring_status": {},
        "legacy_unverified": None,
    }


def normalize_event_user_state(raw: Any) -> dict[str, Any]:
    result = empty_event_user_state()
    if not isinstance(raw, Mapping):
        return result
    result["source_event_id"] = str(raw.get("source_event_id") or "en:5941")
    result["explicit_empty"] = bool(raw.get("explicit_empty"))
    progress = raw.get("progress")
    if isinstance(progress, Mapping):
        result["progress"]["current_pt"] = max(
            int(progress.get("current_pt", 0) or 0), 0
        )
        result["progress"]["pt_mode"] = "auto"
    selections = raw.get("shop_selections")
    if isinstance(selections, Mapping):
        for item, value in selections.items():
            if isinstance(value, (Mapping, list, tuple)):
                continue
            try:
                result["shop_selections"][str(item)] = max(int(value or 0), 0)
            except TypeError, ValueError, OverflowError:
                continue
    recurring = raw.get("recurring_status")
    if isinstance(recurring, Mapping):
        result["recurring_status"] = {
            str(item): dict(value)
            for item, value in recurring.items()
            if isinstance(value, Mapping)
        }
    legacy = raw.get("legacy_unverified")
    result["legacy_unverified"] = dict(legacy) if isinstance(legacy, Mapping) else None
    return result


def migrate_stage2_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    state = empty_event_user_state()
    event = plan.get("event", {})
    source = event.get("source", {}) if isinstance(event, Mapping) else {}
    state["explicit_empty"] = (
        isinstance(source, Mapping) and source.get("kind") == "manual_empty"
    )
    progress = plan.get("progress", {})
    if isinstance(progress, Mapping):
        state["progress"]["current_pt"] = max(
            int(progress.get("current_pt", 0) or 0), 0
        )
    for item in plan.get("shop_items", []):
        if not isinstance(item, Mapping):
            continue
        identity = str(item.get("id") or "").strip()
        if identity:
            state["shop_selections"][identity] = max(
                int(item.get("selected", 0) or 0), 0
            )
    if isinstance(event, Mapping) and any(
        event.get(key) for key in ("name", "farm_end", "shop_end")
    ):
        state["legacy_unverified"] = {
            "event": dict(event),
            "stages": list(plan.get("stages", [])),
            "daily": list(plan.get("daily", [])),
            "extra": list(plan.get("extra", [])),
            "shop_items_without_stable_id": [
                dict(item)
                for item in plan.get("shop_items", [])
                if isinstance(item, Mapping) and not str(item.get("id") or "").strip()
            ],
            "manual_current_pt": int(progress.get("current_pt", 0) or 0)
            if isinstance(progress, Mapping)
            else 0,
            "note": "Stage 2 facts retained as unverified migration evidence; not an active provider",
        }
    return state


def save_event_user_state(
    instance: str, state: Mapping[str, Any], root: Path | str = EVENT_USER_STATE_ROOT
) -> Path:
    normalized = normalize_event_user_state(state)
    path = event_user_state_path(instance, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = to_tmp_file(str(path))
    try:
        file_write(
            temp,
            json.dumps(normalized, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        replace_tmp(temp, str(path))
    except BaseException:
        try:
            file_remove(temp)
        except OSError:
            pass
        raise
    return path


def load_event_user_state(
    instance: str,
    *,
    root: Path | str = EVENT_USER_STATE_ROOT,
    legacy_root: Path | str = EVENT_PLAN_ROOT,
) -> dict[str, Any]:
    path = event_user_state_path(instance, root)
    content = atomic_read_text(str(path))
    if content:
        try:
            return normalize_event_user_state(json.loads(content))
        except (TypeError, ValueError) as exc:
            backup = path.with_name(f"{path.name}.corrupt-{uuid4().hex[:12]}")
            try:
                atomic_replace(str(path), str(backup))
            except OSError as backup_exc:
                logger.warning(
                    f"[WebUI — политика ивента] Не удалось сохранить повреждённый файл {path}: {backup_exc}"
                )
            else:
                logger.warning(
                    f"[WebUI — политика ивента] Повреждённый файл {path} сохранён как {backup}: {exc}"
                )
    legacy_path = Path(legacy_root) / f"{_safe_instance_key(instance)}.json"
    if legacy_path.exists():
        state = migrate_stage2_plan(load_event_plan(instance, root=legacy_root))
        save_event_user_state(instance, state, root=root)
        return state
    return empty_event_user_state()


def event_plan_from_source(
    spec: Mapping[str, Any], state: Mapping[str, Any]
) -> dict[str, Any]:
    saved_source_id = str(state.get("source_event_id") or "")
    state = normalize_event_user_state(state)
    if state["explicit_empty"]:
        plan = empty_event_plan(str(spec.get("server") or "EN"))
        plan["event"]["source"]["kind"] = "manual_empty"
        return plan
    provenance = spec.get("provenance", {})
    status = str(spec.get("source_status") or "unsupported")
    plan = empty_event_plan(str(spec.get("server") or "EN"))
    plan["event"] = {
        "id": str(spec.get("id") or ""),
        "name": str(spec.get("name") or ""),
        "server": str(spec.get("server") or "EN"),
        "farm_end": str(spec.get("farm_end") or ""),
        "shop_end": str(spec.get("shop_end") or ""),
        "source": {
            "kind": "azurlane_lua",
            "verified": status == "verified",
            "status": status,
            "updated_at": "",
            "revision": str(provenance.get("revision") or ""),
            "repository": str(provenance.get("repository") or ""),
        },
    }
    plan["progress"] = dict(state["progress"])
    same_source = not saved_source_id or saved_source_id == plan["event"]["id"]
    legacy = state.get("legacy_unverified") if same_source else None
    legacy_recurring = []
    if isinstance(legacy, Mapping):
        legacy_recurring = [
            item
            for kind in ("daily", "extra")
            for item in legacy.get(kind, [])
            if isinstance(item, Mapping)
        ]
    for source in spec.get("pt_sources", []):
        if not isinstance(source, Mapping) or source.get("points") is None:
            continue
        row = {
            "id": str(source.get("id") or ""),
            "name": str(source.get("name") or ""),
            "points": max(int(source.get("points", 0) or 0), 0),
            "skip": False,
            "completed_date": "",
        }
        saved = state["recurring_status"].get(row["id"]) if same_source else None
        if not isinstance(saved, Mapping):
            matches = [
                item
                for item in legacy_recurring
                if str(item.get("name") or "") == row["name"]
                and int(item.get("points", 0) or 0) == row["points"]
            ]
            saved = matches[0] if len(matches) == 1 else None
        if isinstance(saved, Mapping):
            row["skip"] = bool(saved.get("skip"))
            row["completed_date"] = str(saved.get("completed_date") or "")
        plan["daily" if source.get("recurring") else "extra"].append(row)
    for item in spec.get("maps", []):
        if isinstance(item, Mapping):
            plan["stages"].append(
                {
                    "id": str(item.get("id") or ""),
                    "name": str(item.get("chapter_name") or item.get("id") or ""),
                    "points": 0,
                }
            )
    selections = dict(state["shop_selections"]) if same_source else {}
    if isinstance(legacy, Mapping):
        source_rows = [
            item for item in spec.get("shop_items", []) if isinstance(item, Mapping)
        ]
        for old in legacy.get("shop_items_without_stable_id", []):
            if not isinstance(old, Mapping):
                continue
            matches = [
                item
                for item in source_rows
                if str(item.get("name") or "") == str(old.get("name") or "")
                and int(item.get("price", 0) or 0) == int(old.get("price", 0) or 0)
                and int(item.get("stock", 0) or 0)
                == int(old.get("stock", old.get("quantity", 0)) or 0)
            ]
            if len(matches) == 1:
                identity = str(matches[0].get("row_id") or "")
                selections.setdefault(
                    identity, max(int(old.get("selected", 0) or 0), 0)
                )
    for item in spec.get("shop_items", []):
        if not isinstance(item, Mapping):
            continue
        identity = str(item.get("row_id") or "")
        stock = max(int(item.get("stock", 0) or 0), 0)
        plan["shop_items"].append(
            {
                "id": identity,
                "name": str(item.get("name") or ""),
                "price": max(int(item.get("price", 0) or 0), 0),
                "stock": stock,
                "selected": min(max(int(selections.get(identity, 0) or 0), 0), stock),
                "filter": str(item.get("event_shop_filter") or ""),
                "currency_id": int(item.get("currency_id", 0) or 0),
                "asset": dict(item.get("asset") or {}),
            }
        )
    plan["source_findings"] = list(spec.get("findings", []))
    plan["source_status"] = status
    return plan


def load_builtin_event_plan(instance: str) -> dict[str, Any]:
    artifact = load_builtin_artifact()
    return event_plan_from_source(
        artifact["event_spec"], load_event_user_state(instance)
    )


def user_state_from_plan(
    plan: Mapping[str, Any], previous: Mapping[str, Any]
) -> dict[str, Any]:
    state = normalize_event_user_state(previous)
    event = plan.get("event")
    if isinstance(event, Mapping) and event.get("id"):
        state["source_event_id"] = str(event["id"])
    progress = plan.get("progress")
    if isinstance(progress, Mapping):
        state["progress"] = {
            "current_pt": max(int(progress.get("current_pt", 0) or 0), 0),
            "pt_mode": "auto",
        }
    state["shop_selections"] = {
        str(item.get("id")): max(int(item.get("selected", 0) or 0), 0)
        for item in plan.get("shop_items", [])
        if isinstance(item, Mapping) and item.get("id")
    }
    state["recurring_status"] = {
        str(item.get("id")): {
            "skip": bool(item.get("skip")),
            "completed_date": str(item.get("completed_date") or ""),
        }
        for kind in ("daily", "extra")
        for item in plan.get(kind, [])
        if isinstance(item, Mapping) and item.get("id")
    }
    return state
