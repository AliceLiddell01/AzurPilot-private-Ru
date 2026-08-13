"""Persistent provider-neutral planning model for Event WebUI.

The model is intentionally independent from campaign runtime and from any concrete
external data source. A plan may be entered manually today and later populated by
an AzurLaneLuaScripts provider without changing the three Event pages again.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time
from hashlib import sha256
from math import ceil
from pathlib import Path
from typing import Any, Dict, List, Mapping
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


EVENT_PLAN_SCHEMA_VERSION = 3
EVENT_PLAN_ROOT = Path("./config/state/event_plans")


def empty_event_plan(server: str = "EN") -> Dict[str, Any]:
    return {
        "schema_version": EVENT_PLAN_SCHEMA_VERSION,
        "event": {
            "id": "",
            "name": "",
            "server": str(server or "EN").upper(),
            "farm_end": "",
            "shop_end": "",
            "source": {
                "kind": "manual",
                "verified": False,
                "updated_at": "",
                "revision": "",
            },
        },
        "progress": {"current_pt": 0, "pt_mode": "auto"},
        "stages": [],
        "daily": [],
        "extra": [],
        "shop_items": [],
    }


def _safe_instance_key(instance: str) -> str:
    raw = str(instance or "alas")
    slug = re.sub(r"[^0-9A-Za-z_.-]+", "_", raw).strip("._-") or "alas"
    digest = sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{slug[:48]}-{digest}"


def event_plan_path(instance: str, root: Path | str = EVENT_PLAN_ROOT) -> Path:
    return Path(root) / f"{_safe_instance_key(instance)}.json"


def _non_negative_int(value: Any, default: int = 0) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(result, 0)


def _normalize_points(items: Any) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    if not isinstance(items, list):
        return result
    for item in items:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        points = _non_negative_int(item.get("points"))
        if name and points > 0:
            result.append({"name": name, "points": points})
    return result


def _normalize_recurring_points(items: Any) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    if not isinstance(items, list):
        return result
    for item in items:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        points = _non_negative_int(item.get("points"))
        if not name or points <= 0:
            continue
        completed_date = str(item.get("completed_date") or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", completed_date):
            completed_date = ""
        result.append(
            {
                "name": name,
                "points": points,
                "skip": bool(item.get("skip", False)),
                "completed_date": completed_date,
            }
        )
    return result


def _normalize_shop(items: Any) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    if not isinstance(items, list):
        return result
    for item in items:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        price = _non_negative_int(item.get("price"))
        stock = _non_negative_int(item.get("stock", item.get("quantity", 0)))
        selected = _non_negative_int(item.get("selected", stock))
        selected = min(selected, stock)
        filter_token = str(item.get("filter") or "").strip()
        item_id = str(item.get("id") or "").strip()
        if not name or price <= 0 or stock <= 0:
            continue
        result.append(
            {
                "id": item_id,
                "name": name,
                "price": price,
                "stock": stock,
                "selected": selected,
                "filter": filter_token,
            }
        )
    return result


def normalize_event_plan(raw: Any) -> Dict[str, Any]:
    plan = empty_event_plan()
    if not isinstance(raw, Mapping):
        return plan

    event = raw.get("event")
    if isinstance(event, Mapping):
        source = event.get("source")
        source_data = plan["event"]["source"]
        if isinstance(source, Mapping):
            source_data.update(
                {
                    "kind": str(source.get("kind") or "manual"),
                    "verified": bool(source.get("verified", False)),
                    "updated_at": str(source.get("updated_at") or ""),
                    "revision": str(source.get("revision") or ""),
                }
            )
        plan["event"].update(
            {
                "id": str(event.get("id") or "").strip(),
                "name": str(event.get("name") or "").strip(),
                "server": str(event.get("server") or "EN").upper(),
                "farm_end": str(event.get("farm_end") or "").strip(),
                "shop_end": str(event.get("shop_end") or "").strip(),
                "source": source_data,
            }
        )

    progress = raw.get("progress")
    if isinstance(progress, Mapping):
        plan["progress"]["current_pt"] = _non_negative_int(progress.get("current_pt"))
        mode = str(progress.get("pt_mode") or "auto").lower()
        plan["progress"]["pt_mode"] = mode if mode in {"auto", "manual"} else "auto"

    plan["stages"] = _normalize_points(raw.get("stages"))
    plan["daily"] = _normalize_recurring_points(raw.get("daily"))
    plan["extra"] = _normalize_recurring_points(raw.get("extra"))
    plan["shop_items"] = _normalize_shop(raw.get("shop_items"))
    return plan


def _preserve_corrupt_event_plan(path: Path) -> Path | None:
    """Move an unreadable JSON plan aside before allowing a fresh plan to replace it."""
    backup = path.with_name(f"{path.name}.corrupt-{uuid4().hex[:12]}")
    try:
        atomic_replace(str(path), str(backup))
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning(
            f"[WebUI — План ивента] Не удалось сохранить повреждённый файл {path}: {exc}"
        )
        return None
    return backup


def load_event_plan(instance: str, root: Path | str = EVENT_PLAN_ROOT) -> Dict[str, Any]:
    path = event_plan_path(instance, root)
    try:
        content = atomic_read_text(str(path))
    except OSError as exc:
        logger.warning(f"[WebUI — План ивента] Ошибка чтения плана {path}: {exc}")
        raise

    if not content:
        if not path.exists():
            return empty_event_plan()
        exc = ValueError("файл плана пуст")
    else:
        try:
            return normalize_event_plan(json.loads(content))
        except (ValueError, TypeError) as parse_exc:
            exc = parse_exc

    backup = _preserve_corrupt_event_plan(path)
    if backup is not None:
        logger.warning(
            f"[WebUI — План ивента] Повреждённый план {path} сохранён как {backup}: {exc}"
        )
    else:
        logger.warning(f"[WebUI — План ивента] Не удалось прочитать план {path}: {exc}")
    return empty_event_plan()


def save_event_plan(
    instance: str,
    plan: Mapping[str, Any],
    root: Path | str = EVENT_PLAN_ROOT,
) -> Path:
    normalized = normalize_event_plan(plan)
    normalized["schema_version"] = EVENT_PLAN_SCHEMA_VERSION
    path = event_plan_path(instance, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"
    temp = to_tmp_file(str(path))
    try:
        file_write(temp, content)
        replace_tmp(temp, str(path))
    except BaseException:
        try:
            file_remove(temp)
        except OSError as cleanup_exc:
            logger.warning(
                f"[WebUI — План ивента] Не удалось удалить временный файл {temp}: {cleanup_exc}"
            )
        raise
    return path


def import_legacy_event_calculator(data: Mapping[str, Any], server: str = "EN") -> Dict[str, Any]:
    """Normalize an already-cached legacy BWiki payload without performing network I/O."""
    plan = empty_event_plan(server)
    event = plan["event"]
    event["name"] = str(data.get("event_name") or "").strip()
    event["farm_end"] = str(data.get("end_date") or "").strip()
    event["source"] = {
        "kind": "legacy_bwiki",
        "verified": False,
        "updated_at": str(
            data.get("updated_at")
            or datetime.now().replace(microsecond=0).isoformat(sep=" ")
        ),
        "revision": "",
    }
    plan["stages"] = _normalize_points(data.get("stages"))
    plan["daily"] = _normalize_recurring_points(data.get("daily"))
    plan["extra"] = _normalize_recurring_points(data.get("extra"))
    plan["shop_items"] = _normalize_shop(data.get("shop_items"))
    return plan


def shop_plan_total(plan: Mapping[str, Any]) -> int:
    return sum(
        item["price"] * item["selected"]
        for item in _normalize_shop(plan.get("shop_items"))
    )


def selected_shop_filter_tokens(plan: Mapping[str, Any]) -> List[str]:
    """Build ordered EventShop DSL tokens, including safe per-token amount limits."""
    items = _normalize_shop(plan.get("shop_items"))
    by_token: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        token = item["filter"]
        if token:
            by_token.setdefault(token, []).append(item)

    tokens: List[str] = []
    seen = set()
    for item in items:
        if item["selected"] <= 0:
            continue
        token = item["filter"]
        if not token or token in seen:
            continue
        seen.add(token)
        bucket = by_token.get(token, [])
        if len(bucket) == 1 and item["selected"] < item["stock"]:
            tokens.append(f"{token}:{item['selected']}")
        else:
            tokens.append(token)
    return tokens


def selected_shop_items_missing_filter(plan: Mapping[str, Any]) -> List[str]:
    return [
        item["name"]
        for item in _normalize_shop(plan.get("shop_items"))
        if item["selected"] > 0 and not item["filter"]
    ]


def selected_shop_items_partial(plan: Mapping[str, Any]) -> List[str]:
    return [
        item["name"]
        for item in _normalize_shop(plan.get("shop_items"))
        if 0 < item["selected"] < item["stock"]
    ]


def selected_shop_filter_conflicts(plan: Mapping[str, Any]) -> Dict[str, List[str]]:
    """Return filter tokens whose per-item selection cannot be represented safely by EventShop DSL."""
    by_token: Dict[str, List[Dict[str, Any]]] = {}
    for item in _normalize_shop(plan.get("shop_items")):
        token = item["filter"]
        if token:
            by_token.setdefault(token, []).append(item)

    conflicts: Dict[str, List[str]] = {}
    for token, items in by_token.items():
        selected = [item for item in items if item["selected"] > 0]
        unselected = [item for item in items if item["selected"] == 0]
        shared_partial = len(items) > 1 and any(
            0 < item["selected"] < item["stock"] for item in items
        )
        if (selected and unselected) or shared_partial:
            conflicts[token] = [item["name"] for item in items]
    return conflicts


def estimate_stage_runs(plan: Mapping[str, Any], remaining_pt: int) -> List[Dict[str, Any]]:
    remaining = max(_non_negative_int(remaining_pt), 0)
    result = []
    for stage in _normalize_points(plan.get("stages")):
        points = stage["points"]
        result.append(
            {
                "name": stage["name"],
                "points": points,
                "runs": 0 if remaining == 0 else ceil(remaining / points),
            }
        )
    return result


def _parse_event_end(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo is not None else value
    if isinstance(value, date):
        return datetime.combine(value, time.max.replace(microsecond=0))

    text_value = str(value or "").strip().replace("T", " ")
    if not text_value:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text_value):
            return datetime.combine(date.fromisoformat(text_value), time.max.replace(microsecond=0))
        parsed = datetime.fromisoformat(text_value)
        return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed
    except ValueError:
        return None


def _current_datetime(value: date | datetime | None) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo is not None else value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    return datetime.now().replace(microsecond=0)


def remaining_event_days(
    plan: Mapping[str, Any],
    today: date | datetime | None = None,
) -> int:
    """Return inclusive farming days still available, respecting an end time on the current day."""
    event = plan.get("event")
    farm_end = event.get("farm_end") if isinstance(event, Mapping) else ""
    end_at = _parse_event_end(farm_end)
    if end_at is None:
        return 0
    current = _current_datetime(today)
    if current > end_at:
        return 0
    return (end_at.date() - current.date()).days + 1


def projected_recurring_pt(
    plan: Mapping[str, Any],
    days_remaining: int | None = None,
    today: date | datetime | None = None,
) -> int:
    """Estimate PT still available from recurring daily/extra sources."""
    current = _current_datetime(today)
    current_day = current.date()
    days = (
        remaining_event_days(plan, current)
        if days_remaining is None
        else max(int(days_remaining), 0)
    )
    if days <= 0:
        return 0

    total = 0
    today_text = current_day.isoformat()
    recurring_items = _normalize_recurring_points(plan.get("daily")) + _normalize_recurring_points(
        plan.get("extra")
    )
    for item in recurring_items:
        if item["skip"]:
            continue
        contribution = item["points"] * days
        if item["completed_date"] == today_text:
            contribution = max(contribution - item["points"], 0)
        total += contribution
    return total


def event_farm_summary(
    plan: Mapping[str, Any],
    target_pt: int,
    current_pt: int | None = None,
    today: date | datetime | None = None,
) -> Dict[str, int]:
    """Calculate the user-facing Event farming forecast."""
    normalized = normalize_event_plan(plan)
    current = (
        normalized["progress"]["current_pt"]
        if current_pt is None
        else _non_negative_int(current_pt)
    )
    target = _non_negative_int(target_pt)
    remaining_before_recurring = max(target - current, 0)
    days = remaining_event_days(normalized, today=today)
    recurring = projected_recurring_pt(normalized, days_remaining=days, today=today)
    farm_required = max(remaining_before_recurring - recurring, 0)
    return {
        "target_pt": target,
        "current_pt": current,
        "remaining_days": days,
        "recurring_pt": recurring,
        "remaining_before_recurring": remaining_before_recurring,
        "farm_required_pt": farm_required,
    }
