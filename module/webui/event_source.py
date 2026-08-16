"""Проекция immutable EventSpec + отдельно хранимой пользовательской политики."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
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
from module.event_datamine.artifact import BUILTIN_ARTIFACT_ROOT, load_builtin_artifact
from module.event_datamine.discovery import EventDiscoveryError
from module.event_datamine.registry import EventArtifactRegistry
from module.logger import logger
from module.webui.event_observation import (
    load_event_observation,
    observation_is_fresh,
)
from module.webui.event_plan import EVENT_PLAN_ROOT, empty_event_plan, load_event_plan
from module.webui.state_lock import state_write_lock

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
        "source_event_id": "",
        "explicit_empty": False,
        "shop_selections": {},
        "legacy_unverified": None,
        "legacy_debug_evidence": None,
    }


def normalize_event_user_state(raw: Any) -> dict[str, Any]:
    result = empty_event_user_state()
    if not isinstance(raw, Mapping):
        return result
    result["source_event_id"] = str(raw.get("source_event_id") or "")
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


def _event_user_state_lock_path(
    instance: str, root: Path | str = EVENT_USER_STATE_ROOT
) -> Path:
    state_path = event_user_state_path(instance, root)
    return state_path.with_suffix(f"{state_path.suffix}.lock")


def event_user_state_write_lock(
    instance: str, root: Path | str = EVENT_USER_STATE_ROOT
):
    """Вернуть общую блокировку полного read-modify-write пользовательского состояния."""

    return state_write_lock(_event_user_state_lock_path(instance, root))


def _save_event_user_state_unlocked(
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


def _load_event_user_state_unlocked(
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
        _save_event_user_state_unlocked(instance, state, root=root)
        return state
    return empty_event_user_state()


def save_event_user_state(
    instance: str, state: Mapping[str, Any], root: Path | str = EVENT_USER_STATE_ROOT
) -> Path:
    """Сохранить пользовательское состояние под общей блокировкой файла."""

    with event_user_state_write_lock(instance, root):
        return _save_event_user_state_unlocked(instance, state, root=root)


def load_event_user_state(
    instance: str,
    *,
    root: Path | str = EVENT_USER_STATE_ROOT,
    legacy_root: Path | str = EVENT_PLAN_ROOT,
) -> dict[str, Any]:
    """Прочитать пользовательское состояние под общей блокировкой файла."""

    with event_user_state_write_lock(instance, root):
        return _load_event_user_state_unlocked(
            instance, root=root, legacy_root=legacy_root
        )


def mutate_event_user_state(
    instance: str,
    mutation: Callable[[Mapping[str, Any]], Mapping[str, Any] | None],
    *,
    root: Path | str = EVENT_USER_STATE_ROOT,
    legacy_root: Path | str = EVENT_PLAN_ROOT,
) -> bool:
    """Атомарно выполнить чтение, изменение и сохранение пользовательского состояния."""

    with event_user_state_write_lock(instance, root):
        current = _load_event_user_state_unlocked(
            instance, root=root, legacy_root=legacy_root
        )
        updated = mutation(current)
        if updated is None:
            return False
        _save_event_user_state_unlocked(instance, updated, root=root)
        return True


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
        "farm_start": str(spec.get("farm_start") or ""),
        "farm_end": str(spec.get("farm_end") or ""),
        "shop_end": str(spec.get("shop_end") or ""),
        "source": {
            "kind": "azurlane_lua",
            "verified": status == "verified",
            "status": status,
            "updated_at": "",
            "revision": str(provenance.get("revision") or ""),
            "repository": str(provenance.get("repository") or ""),
            "provider": str(provenance.get("provider") or ""),
        },
    }
    plan["currencies"] = [
        dict(item) for item in spec.get("currencies", []) if isinstance(item, Mapping)
    ]
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
                    "source_status": str(item.get("source_status") or "verified"),
                    "runtime_eligible": str(item.get("source_status") or "verified")
                    == "verified",
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
                "category": str(item.get("category") or "unknown"),
                "rarity": item.get("rarity"),
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


def load_event_plan_from_artifact(
    instance: str,
    artifact: Mapping[str, Any],
    runtime_observation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    spec = artifact["event_spec"]
    provenance = spec.get("provenance", {})
    revision = str(provenance.get("revision") or "")
    observation = load_event_observation(
        instance,
        str(spec.get("id") or ""),
        str(spec.get("server") or "EN"),
        revision,
    )
    runtime_matches = (
        isinstance(runtime_observation, Mapping)
        and str(runtime_observation.get("event_id") or "") == str(spec.get("id") or "")
        and str(runtime_observation.get("server") or "").upper()
        == str(spec.get("server") or "EN").upper()
        and str(runtime_observation.get("source_revision") or "") == revision
    )
    if isinstance(runtime_observation, Mapping):
        if not runtime_matches:
            observation.setdefault("findings", []).append(
                {
                    "code": "runtime_observation_identity_rejected",
                    "message": "Runtime observation не совпадает с current event identity",
                    "path": "runtime_observation",
                }
            )
        elif _current_pt_evidence_is_newer(runtime_observation, observation):
            current_pt = runtime_observation.get("current_pt")
            current_pt_observed_at = str(
                runtime_observation.get("current_pt_observed_at")
                or runtime_observation.get("observed_at")
                or ""
            )
            current_pt_status = str(
                runtime_observation.get("current_pt_status") or ""
            ).lower()
            if current_pt is None:
                current_pt_status = "unavailable"
            elif current_pt_status not in {"observed", "stale"}:
                current_pt_status = (
                    "observed"
                    if observation_is_fresh({"observed_at": current_pt_observed_at})
                    else "stale"
                )
            observation.update(
                {
                    "current_pt": current_pt,
                    "current_pt_source": str(
                        runtime_observation.get("current_pt_source")
                        or runtime_observation.get("source")
                        or ""
                    ),
                    "current_pt_observed_at": current_pt_observed_at,
                    "current_pt_status": current_pt_status,
                }
            )
        else:
            observation.setdefault("findings", []).append(
                {
                    "code": "runtime_observation_not_newer",
                    "message": "Runtime observation не новее сохранённого PT evidence",
                    "path": "runtime_observation",
                }
            )
    return event_plan_from_source(spec, load_event_user_state(instance), observation)


def _current_pt_evidence_is_newer(
    candidate: Mapping[str, Any], existing: Mapping[str, Any]
) -> bool:
    """Не позволить более старому или равному OCR evidence затереть свежую запись."""

    def timestamp(value: Any) -> float | None:
        try:
            observed = datetime.fromisoformat(str(value or ""))
        except ValueError:
            return None
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        return observed.timestamp()

    candidate_at = timestamp(
        candidate.get("current_pt_observed_at") or candidate.get("observed_at")
    )
    existing_at = timestamp(
        existing.get("current_pt_observed_at") or existing.get("observed_at")
    )
    return candidate_at is not None and (
        existing_at is None or candidate_at > existing_at
    )


def load_builtin_event_plan(
    instance: str,
    name: str,
    runtime_observation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Загрузить явно указанный demo/golden artifact, никогда не production default."""

    return load_event_plan_from_artifact(
        instance, load_builtin_artifact(name), runtime_observation
    )


def _unavailable_current_plan(
    server: str, *, code: str, message: str, candidates: tuple[str, ...] = ()
) -> dict[str, Any]:
    plan = empty_event_plan(server)
    plan["event"]["source"] = {
        "kind": "azurlane_lua",
        "verified": False,
        "status": "unsupported",
        "updated_at": "",
        "revision": "",
        "repository": "AzurLaneTools/AzurLaneLuaScripts",
        "provider": "AzurLaneLuaScripts",
    }
    plan["source_status"] = "unsupported"
    plan["source_findings"] = [
        {
            "code": code,
            "severity": "error",
            "message": message,
            "path": "registry.current",
            "candidates": list(candidates),
        }
    ]
    return plan


def resolve_current_event_artifact(
    *,
    server: str = "EN",
    now: datetime | None = None,
    registry_root: Path | str = BUILTIN_ARTIFACT_ROOT,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Разрешить current artifact один раз либо вернуть typed unavailable plan."""

    current_time = now or datetime.now()
    try:
        artifact = EventArtifactRegistry(registry_root).resolve_current(
            server, current_time
        )
    except EventDiscoveryError as exc:
        return None, _unavailable_current_plan(
            server,
            code=exc.code,
            message=str(exc),
            candidates=exc.candidates,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return None, _unavailable_current_plan(
            server, code="event_registry_invalid", message=str(exc)
        )
    if artifact is None:
        return None, _unavailable_current_plan(
            server,
            code="current_event_unavailable",
            message="Для текущего server-local lifecycle нет production Event artifact",
        )
    return artifact, None


def load_current_event_plan(
    instance: str,
    runtime_observation: Mapping[str, Any] | None = None,
    *,
    server: str = "EN",
    now: datetime | None = None,
    registry_root: Path | str = BUILTIN_ARTIFACT_ROOT,
) -> dict[str, Any]:
    artifact, unavailable = resolve_current_event_artifact(
        server=server, now=now, registry_root=registry_root
    )
    if artifact is None:
        assert unavailable is not None
        return unavailable
    return load_event_plan_from_artifact(instance, artifact, runtime_observation)


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
