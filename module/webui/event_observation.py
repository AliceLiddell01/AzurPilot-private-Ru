"""Typed fail-closed runtime evidence for the Event product UI."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, TypedDict
from uuid import uuid4

from deploy.atomic import (
    atomic_read_text,
    atomic_replace,
    file_remove,
    file_write,
    replace_tmp,
    to_tmp_file,
)
from module.logger import logger

EVENT_OBSERVATION_SCHEMA_VERSION = 1
EVENT_OBSERVATION_ROOT = Path("./config/state/event_observation")
EVENT_OBSERVATION_MAX_AGE = timedelta(hours=48)
ObservationSource = Literal[
    "dashboard_ocr", "event_shop_scanner", "mission_scanner", "fixture", "replay"
]


class ObservationFinding(TypedDict):
    code: str
    message: str
    path: str


def _safe_key(value: str, fallback: str) -> str:
    raw = str(value or fallback)
    slug = re.sub(r"[^0-9A-Za-z_.-]+", "_", raw).strip("._-") or fallback
    return f"{slug[:48]}-{sha256(raw.encode('utf-8')).hexdigest()[:10]}"


def event_observation_path(
    instance: str,
    event_id: str,
    server: str,
    root: Path | str = EVENT_OBSERVATION_ROOT,
) -> Path:
    return (
        Path(root)
        / _safe_key(instance, "alas")
        / f"{_safe_key(server, 'EN')}-{_safe_key(event_id, 'event')}.json"
    )


def empty_event_observation(
    event_id: str, server: str, instance: str
) -> dict[str, Any]:
    return {
        "schema_version": EVENT_OBSERVATION_SCHEMA_VERSION,
        "event_id": str(event_id),
        "server": str(server).upper(),
        "instance": str(instance),
        "observed_at": "",
        "source": "",
        "current_pt": None,
        "current_pt_source": "",
        "current_pt_observed_at": "",
        "current_pt_status": "unavailable",
        "shop_source": "",
        "shop_observed_at": "",
        "shop_items": [],
        "maps": [],
        "missions": [],
        "findings": [],
    }


def _optional_non_negative_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        result = int(value)
    except TypeError, ValueError, OverflowError:
        return None
    return result if result >= 0 else None


def _finding(code: str, message: str, path: str = "") -> ObservationFinding:
    return {"code": code, "message": message, "path": path}


def normalize_event_observation(
    raw: Any, *, event_id: str, server: str, instance: str
) -> dict[str, Any]:
    result = empty_event_observation(event_id, server, instance)
    if not isinstance(raw, Mapping):
        return result
    if int(raw.get("schema_version", 0) or 0) != EVENT_OBSERVATION_SCHEMA_VERSION:
        result["findings"].append(
            _finding(
                "observation_schema_unsupported",
                "Версия EventObservation не поддерживается",
                "schema_version",
            )
        )
        return result
    if str(raw.get("event_id") or "") != str(event_id):
        result["findings"].append(
            _finding(
                "cross_event_rejected",
                "Наблюдение относится к другому событию",
                "event_id",
            )
        )
        return result
    if str(raw.get("server") or "").upper() != str(server).upper():
        result["findings"].append(
            _finding(
                "cross_server_rejected",
                "Наблюдение относится к другому серверу",
                "server",
            )
        )
        return result
    if str(raw.get("instance") or "") != str(instance):
        result["findings"].append(
            _finding(
                "cross_profile_rejected",
                "Наблюдение относится к другому профилю",
                "instance",
            )
        )
        return result
    result["observed_at"] = str(raw.get("observed_at") or "")
    result["source"] = str(raw.get("source") or "")
    result["current_pt"] = _optional_non_negative_int(raw.get("current_pt"))
    for field in (
        "current_pt_source",
        "current_pt_observed_at",
        "current_pt_status",
        "shop_source",
        "shop_observed_at",
    ):
        result[field] = str(raw.get(field) or "")
    for field in ("shop_items", "maps", "missions", "findings"):
        value = raw.get(field)
        if isinstance(value, list):
            result[field] = [dict(item) for item in value if isinstance(item, Mapping)]
    return result


def observation_age(
    observation: Mapping[str, Any], now: datetime | None = None
) -> timedelta | None:
    value = str(observation.get("observed_at") or "").replace("Z", "+00:00")
    try:
        observed = datetime.fromisoformat(value)
    except ValueError:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current - observed.replace(tzinfo=None)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=current.tzinfo)
    return current - observed.astimezone(timezone.utc)


def observation_is_fresh(
    observation: Mapping[str, Any],
    *,
    now: datetime | None = None,
    max_age: timedelta = EVENT_OBSERVATION_MAX_AGE,
) -> bool:
    age = observation_age(observation, now)
    return age is not None and timedelta(0) <= age <= max_age


def save_event_observation(
    instance: str,
    observation: Mapping[str, Any],
    *,
    root: Path | str = EVENT_OBSERVATION_ROOT,
    allow_nonproduction: bool = False,
) -> Path:
    event_id = str(observation.get("event_id") or "")
    server = str(observation.get("server") or "").upper()
    normalized = normalize_event_observation(
        observation, event_id=event_id, server=server, instance=instance
    )
    if not event_id or not server:
        raise ValueError("EventObservation требует event_id и server")
    if normalized["source"] in {"fixture", "replay"} and not allow_nonproduction:
        raise ValueError(
            "Fixture/replay evidence запрещено сохранять как production observation"
        )
    path = event_observation_path(instance, event_id, server, root)
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


def load_event_observation(
    instance: str,
    event_id: str,
    server: str,
    *,
    root: Path | str = EVENT_OBSERVATION_ROOT,
    allow_nonproduction: bool = False,
) -> dict[str, Any]:
    path = event_observation_path(instance, event_id, server, root)
    content = atomic_read_text(str(path))
    if not content:
        return empty_event_observation(event_id, server, instance)
    try:
        raw = json.loads(content)
        result = normalize_event_observation(
            raw, event_id=event_id, server=server, instance=instance
        )
    except (TypeError, ValueError) as exc:
        backup = path.with_name(f"{path.name}.corrupt-{uuid4().hex[:12]}")
        try:
            atomic_replace(str(path), str(backup))
        except OSError as backup_exc:
            logger.warning(
                f"[WebUI — наблюдение ивента] Не удалось сохранить повреждённый файл {path}: {backup_exc}"
            )
        else:
            logger.warning(
                f"[WebUI — наблюдение ивента] Повреждённый файл {path} сохранён как {backup}: {exc}"
            )
        return empty_event_observation(event_id, server, instance)
    if result["source"] in {"fixture", "replay"} and not allow_nonproduction:
        clean = empty_event_observation(event_id, server, instance)
        clean["findings"].append(
            _finding(
                "nonproduction_evidence_rejected",
                "Fixture/replay evidence не используется в production UI",
                "source",
            )
        )
        return clean
    return result


def dashboard_pt_observation(
    *,
    instance: str,
    event_id: str,
    server: str,
    value: Any,
    recorded_at: Any,
    now: datetime | None = None,
) -> dict[str, Any]:
    result = empty_event_observation(event_id, server, instance)
    result["source"] = "dashboard_ocr"
    result["observed_at"] = str(recorded_at or "")
    result["current_pt_source"] = "dashboard_ocr"
    result["current_pt_observed_at"] = str(recorded_at or "")
    result["current_pt"] = _optional_non_negative_int(value)
    if result["current_pt"] is None:
        result["findings"].append(
            _finding(
                "current_pt_unavailable",
                "OCR не предоставил валидный баланс PT",
                "current_pt",
            )
        )
    fresh = observation_is_fresh(result, now=now)
    result["current_pt_status"] = (
        "observed"
        if fresh and result["current_pt"] is not None
        else "stale"
        if result["current_pt"] is not None
        else "unavailable"
    )
    if not fresh:
        result["findings"].append(
            _finding(
                "observation_stale",
                "OCR-наблюдение PT отсутствует или устарело",
                "observed_at",
            )
        )
    return result
