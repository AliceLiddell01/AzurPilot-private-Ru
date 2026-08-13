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
from module.webui.event_observation import (
    load_event_observation,
    observation_is_fresh,
)
from module.webui.event_plan import EVENT_PLAN_ROOT, empty_event_plan, load_event_plan

EVENT_USER_STATE_SCHEMA_VERSION = 2
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
        "shop_selections": {},
        "legacy_unverified": None,
        "legacy_debug_evidence": None,
    }


def normalize_event_user_state(raw: Any) -> dict[str, Any]:
    result = empty_event_user_state()
    if not isinstance(raw, Mapping):
        return result
    result["source_event_id"] = str(raw.get("source_event_id") or "en:5941")
    result["explicit_empty"] = bool(raw.get("explicit_empty"))
    selections = raw.get("shop_selections")
    if isinstance(selections, Mapping):
        for item, value in selections.items():
            if isinstance(value, (Mapping, list, tuple)):
                continue
            try:
                result["shop_selections"][str(item)] = max(int(value or 0), 0)
            except TypeError, ValueError, OverflowError:
                continue
    legacy = raw.get("legacy_unverified")
    result["legacy_unverified"] = dict(legacy) if isinstance(legacy, Mapping) else None
    debug = raw.get("legacy_debug_evidence")
    result["legacy_debug_evidence"] = (
        dict(debug) if isinstance(debug, Mapping) else None
    )
    progress = raw.get("progress")
    recurring = raw.get("recurring_status")
    if isinstance(progress, Mapping) or isinstance(recurring, Mapping):
        result["legacy_debug_evidence"] = {
            "manual_progress": dict(progress)
            if isinstance(progress, Mapping)
            else None,
            "manual_recurring_status": dict(recurring)
            if isinstance(recurring, Mapping)
            else None,
            "note": "Stage 3 manual observation retained for diagnostics only",
        }
    return result


def migrate_stage2_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    state = empty_event_user_state()
    event = plan.get("event", {})
    source = event.get("source", {}) if isinstance(event, Mapping) else {}
    state["explicit_empty"] = (
        isinstance(source, Mapping) and source.get("kind") == "manual_empty"
    )
    progress = plan.get("progress", {})
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
        state["legacy_debug_evidence"] = {
            "manual_current_pt": int(progress.get("current_pt", 0) or 0)
            if isinstance(progress, Mapping)
            else 0,
            "manual_sources": [
                dict(item)
                for kind in ("daily", "extra")
                for item in plan.get(kind, [])
                if isinstance(item, Mapping)
            ],
            "note": "Legacy manual values are debug evidence and never production truth",
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
    spec: Mapping[str, Any],
    state: Mapping[str, Any],
    observation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    saved_source_id = str(state.get("source_event_id") or "")
    state = normalize_event_user_state(state)
    if state["explicit_empty"]:
        plan = empty_event_plan(str(spec.get("server") or "EN"))
        plan["event"]["source"]["kind"] = "manual_empty"
        plan["progress"] = {
            "current_pt": None,
            "source": "",
            "observed_at": "",
            "status": "unavailable",
        }
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
    observation = observation if isinstance(observation, Mapping) else {}
    current_fresh = str(observation.get("current_pt_status") or "") == "observed"
    if not observation.get("current_pt_status"):
        current_fresh = observation_is_fresh(
            {
                "observed_at": observation.get("current_pt_observed_at")
                or observation.get("observed_at")
            }
        )
    shop_fresh = observation_is_fresh(
        {"observed_at": observation.get("shop_observed_at") or ""}
    )
    current_pt = observation.get("current_pt") if current_fresh else None
    plan["progress"] = {
        "current_pt": current_pt
        if isinstance(current_pt, int) and current_pt >= 0
        else None,
        "source": str(
            observation.get("current_pt_source") or observation.get("source") or ""
        ),
        "observed_at": str(
            observation.get("current_pt_observed_at")
            or observation.get("observed_at")
            or ""
        ),
        "status": "observed"
        if current_fresh and isinstance(current_pt, int)
        else "stale"
        if observation.get("current_pt") is not None
        else "unavailable",
    }
    same_source = not saved_source_id or saved_source_id == plan["event"]["id"]
    legacy = state.get("legacy_unverified") if same_source else None
    plan["pt_sources"] = []
    for source in spec.get("pt_sources", []):
        if not isinstance(source, Mapping):
            continue
        plan["pt_sources"].append(
            {
                "id": str(source.get("id") or ""),
                "name": str(source.get("name") or ""),
                "kind": str(source.get("kind") or "unknown"),
                "points": int(source["points"])
                if source.get("points") is not None
                else None,
                "recurring": bool(source.get("recurring")),
                "source_ids": list(source.get("source_ids") or []),
                "observation_status": "unavailable",
            }
        )
    map_observations = {
        str(item.get("map_id") or item.get("id") or ""): item
        for item in observation.get("maps", [])
        if isinstance(item, Mapping)
    }
    for item in spec.get("maps", []):
        if isinstance(item, Mapping):
            identity = str(item.get("id") or "")
            runtime = map_observations.get(identity, {})
            plan["stages"].append(
                {
                    "id": identity,
                    "name": str(item.get("chapter_name") or item.get("id") or ""),
                    "points": runtime.get("points")
                    if observation_is_fresh(observation)
                    else None,
                    "oil": runtime.get("oil")
                    if observation_is_fresh(observation)
                    else None,
                    "coin": runtime.get("coin")
                    if observation_is_fresh(observation)
                    else None,
                    "stars": runtime.get("stars")
                    if observation_is_fresh(observation)
                    else None,
                    "clear_count": runtime.get("clear_count")
                    if observation_is_fresh(observation)
                    else None,
                    "observation_status": "observed"
                    if observation_is_fresh(observation) and runtime
                    else "unavailable",
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
    shop_observed_fresh = shop_fresh
    shop_observations = {
        str(item.get("row_id")): item
        for item in observation.get("shop_items", [])
        if isinstance(item, Mapping) and item.get("row_id") is not None
    }
    for item in spec.get("shop_items", []):
        if not isinstance(item, Mapping):
            continue
        identity = str(item.get("row_id") or "")
        stock = max(int(item.get("stock", 0) or 0), 0)
        runtime = shop_observations.get(identity, {})
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
                "amount": int(item.get("amount", 1) or 1),
                "remaining": runtime.get("remaining") if shop_observed_fresh else None,
                "purchased": runtime.get("purchased") if shop_observed_fresh else None,
                "match_status": str(runtime.get("status") or "unmatched")
                if shop_observed_fresh
                else "unavailable",
            }
        )
    plan["milestones"] = [
        dict(item) for item in spec.get("milestones", []) if isinstance(item, Mapping)
    ]
    plan["observation"] = {
        "status": "fresh"
        if observation_is_fresh(observation)
        else "stale"
        if observation.get("observed_at")
        else "unavailable",
        "observed_at": str(observation.get("observed_at") or ""),
        "findings": list(observation.get("findings", [])),
    }
    plan["source_findings"] = list(spec.get("findings", []))
    plan["source_status"] = status
    return plan


def load_builtin_event_plan(
    instance: str, runtime_observation: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    artifact = load_builtin_artifact()
    spec = artifact["event_spec"]
    observation = load_event_observation(
        instance, str(spec.get("id") or ""), str(spec.get("server") or "EN")
    )
    if isinstance(runtime_observation, Mapping):
        for field in (
            "current_pt",
            "current_pt_source",
            "current_pt_observed_at",
            "current_pt_status",
        ):
            if field in runtime_observation:
                observation[field] = runtime_observation[field]
    return event_plan_from_source(spec, load_event_user_state(instance), observation)


def user_state_from_plan(
    plan: Mapping[str, Any], previous: Mapping[str, Any]
) -> dict[str, Any]:
    state = normalize_event_user_state(previous)
    event = plan.get("event")
    if isinstance(event, Mapping) and event.get("id"):
        state["source_event_id"] = str(event["id"])
    state["shop_selections"] = {
        str(item.get("id")): max(int(item.get("selected", 0) or 0), 0)
        for item in plan.get("shop_items", [])
        if isinstance(item, Mapping) and item.get("id")
    }
    return state
