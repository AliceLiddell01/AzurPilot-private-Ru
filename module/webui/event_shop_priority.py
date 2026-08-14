"""Persistent purchase priorities for the EventShop runtime and WebUI."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from deploy.atomic import (
    atomic_read_text,
    atomic_replace,
    file_remove,
    file_write,
    replace_tmp,
    to_tmp_file,
)
from module.event_datamine.registry import EventArtifactRegistry
from module.logger import logger
from module.webui.event_shop_observation import reconcile_event_shop

EVENT_SHOP_PRIORITY_SCHEMA_VERSION = 1
EVENT_SHOP_PRIORITY_ROOT = Path("./config/state/event_shop_priority")


class PriorityRuntimeItems(list):
    """Runtime subset that preserves the complete scan for observation persistence."""

    def __init__(self, values: Iterable[Any], *, observation_items: Sequence[Any]):
        super().__init__(values)
        self.observation_items = list(observation_items)


def _safe_instance_key(instance: str) -> str:
    raw = str(instance or "alas")
    slug = re.sub(r"[^0-9A-Za-z_.-]+", "_", raw).strip("._-") or "alas"
    digest = sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{slug[:48]}-{digest}"


def event_shop_priority_path(
    instance: str, root: Path | str = EVENT_SHOP_PRIORITY_ROOT
) -> Path:
    return Path(root) / f"{_safe_instance_key(instance)}.json"


def empty_event_shop_priority(event_id: str = "") -> dict[str, Any]:
    return {
        "schema_version": EVENT_SHOP_PRIORITY_SCHEMA_VERSION,
        "event_id": str(event_id or ""),
        "priorities": {},
        "purchased": [],
        "remaining": {},
        "blocked": {},
    }


def normalize_event_shop_priority(
    raw: Any, *, event_id: str = ""
) -> dict[str, Any]:
    result = empty_event_shop_priority(event_id)
    if not isinstance(raw, Mapping):
        return result
    saved_event_id = str(raw.get("event_id") or "")
    if event_id and saved_event_id and saved_event_id != event_id:
        return result
    result["event_id"] = str(event_id or saved_event_id)
    priorities = raw.get("priorities")
    if isinstance(priorities, Mapping):
        for row_id, value in priorities.items():
            if isinstance(value, (Mapping, list, tuple, bool)):
                continue
            try:
                priority = int(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if priority >= 0:
                result["priorities"][str(row_id)] = priority
    purchased = raw.get("purchased")
    if isinstance(purchased, Sequence) and not isinstance(purchased, (str, bytes)):
        result["purchased"] = sorted(
            {str(row_id) for row_id in purchased if str(row_id).strip()}
        )
    remaining = raw.get("remaining")
    if isinstance(remaining, Mapping):
        for row_id, value in remaining.items():
            try:
                count = int(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if count >= 0:
                result["remaining"][str(row_id)] = count
    blocked = raw.get("blocked")
    if isinstance(blocked, Mapping):
        result["blocked"] = {
            str(row_id): str(message)
            for row_id, message in blocked.items()
            if str(row_id).strip() and str(message).strip()
        }
    return result


def save_event_shop_priority(
    instance: str,
    state: Mapping[str, Any],
    *,
    root: Path | str = EVENT_SHOP_PRIORITY_ROOT,
) -> Path:
    normalized = normalize_event_shop_priority(
        state, event_id=str(state.get("event_id") or "")
    )
    path = event_shop_priority_path(instance, root)
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


def load_event_shop_priority(
    instance: str,
    event_id: str,
    *,
    root: Path | str = EVENT_SHOP_PRIORITY_ROOT,
) -> dict[str, Any]:
    path = event_shop_priority_path(instance, root)
    content = atomic_read_text(str(path))
    if content:
        try:
            state = normalize_event_shop_priority(json.loads(content), event_id=event_id)
        except (TypeError, ValueError) as exc:
            backup = path.with_name(f"{path.name}.corrupt-{uuid4().hex[:12]}")
            try:
                atomic_replace(str(path), str(backup))
            except OSError as backup_exc:
                logger.warning(
                    f"[Магазин события — приоритеты] Не удалось сохранить повреждённый файл {path}: {backup_exc}"
                )
            else:
                logger.warning(
                    f"[Магазин события — приоритеты] Повреждённый файл {path} сохранён как {backup}: {exc}"
                )
            return empty_event_shop_priority(event_id)
        if state["event_id"] != str(event_id or ""):
            state = empty_event_shop_priority(event_id)
        return state
    return empty_event_shop_priority(event_id)


def set_event_shop_priority(
    instance: str,
    event_id: str,
    row_id: str | int,
    priority: int | None,
    *,
    root: Path | str = EVENT_SHOP_PRIORITY_ROOT,
) -> dict[str, Any]:
    state = load_event_shop_priority(instance, event_id, root=root)
    key = str(row_id)
    if priority is None:
        state["priorities"].pop(key, None)
    else:
        value = int(priority)
        if value < 0:
            raise ValueError("Приоритет покупки не может быть отрицательным")
        state["priorities"][key] = value
        state["purchased"] = [item for item in state["purchased"] if item != key]
    state["blocked"].pop(key, None)
    save_event_shop_priority(instance, state, root=root)
    return state


def _server_from_config(config: Any) -> str:
    server = str(getattr(config, "SERVER", "EN") or "EN").upper()
    return server if server else "EN"


def _current_spec(config: Any) -> Mapping[str, Any] | None:
    artifact = EventArtifactRegistry().resolve_current(
        _server_from_config(config), datetime.now()
    )
    if not isinstance(artifact, Mapping):
        return None
    spec = artifact.get("event_spec")
    return spec if isinstance(spec, Mapping) else None


def _catalog_rows(spec: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        item
        for item in spec.get("shop_items", [])
        if isinstance(item, Mapping) and item.get("row_id") is not None
    ]


def _runtime_filter(item: Any) -> str:
    return "".join(
        str(getattr(item, field, "") or "") for field in ("group", "sub_genre", "tier")
    )


def prepare_event_shop_runtime_items(
    config: Any,
    runtime_items: Sequence[Any],
    *,
    root: Path | str = EVENT_SHOP_PRIORITY_ROOT,
) -> PriorityRuntimeItems:
    """Return only safely matched priority rows while preserving the full scan."""
    full_scan = list(runtime_items)
    spec = _current_spec(config)
    if spec is None:
        logger.warning(
            "[Магазин события — приоритеты] EventSpec недоступен; покупка заблокирована"
        )
        return PriorityRuntimeItems([], observation_items=full_scan)

    event_id = str(spec.get("id") or "")
    state = load_event_shop_priority(config.config_name, event_id, root=root)
    rows, _ = reconcile_event_shop(spec, full_scan)
    runtime_by_row: dict[str, Any] = {}
    ambiguous_filters = set()
    for runtime_row in rows:
        status = str(runtime_row.get("status") or "")
        token = str(runtime_row.get("filter") or "").lower()
        if status == "matched" and runtime_row.get("row_id") is not None:
            index = int(runtime_row["runtime_index"])
            runtime_by_row[str(runtime_row["row_id"])] = full_scan[index]
        elif status == "ambiguous" and token:
            ambiguous_filters.add(token)

    changed = False
    purchased = set(state["purchased"])
    for row_id, runtime in runtime_by_row.items():
        count = max(int(getattr(runtime, "count", 0) or 0), 0)
        if state["remaining"].get(row_id) != count:
            state["remaining"][row_id] = count
            changed = True
    for row_id in list(purchased):
        runtime = runtime_by_row.get(row_id)
        if runtime is not None and int(getattr(runtime, "count", 0) or 0) > 0:
            purchased.remove(row_id)
            changed = True
    if changed:
        state["purchased"] = sorted(purchased)

    catalog = _catalog_rows(spec)
    catalog_by_id = {str(item.get("row_id")): item for item in catalog}
    candidates: list[tuple[int, int, str, Mapping[str, Any], Any]] = []
    blocked: dict[str, str] = {}
    catalog_order = {str(item.get("row_id")): index for index, item in enumerate(catalog)}
    for row_id, priority in state["priorities"].items():
        if row_id in purchased:
            continue
        source = catalog_by_id.get(row_id)
        if source is None:
            continue
        runtime = runtime_by_row.get(row_id)
        source_filter = str(source.get("event_shop_filter") or "")
        if runtime is None:
            if source_filter.lower() in ambiguous_filters:
                blocked[row_id] = "Не удаётся безопасно отличить от похожего товара"
            else:
                blocked[row_id] = "Товар сейчас не найден в магазине"
            continue
        if str(getattr(runtime, "cost", "") or "").lower() == "urpt":
            blocked[row_id] = "Покупка за UR-очки пока недоступна в режиме приоритетов"
            continue
        candidates.append(
            (
                int(priority),
                catalog_order.get(row_id, 10**9),
                row_id,
                source,
                runtime,
            )
        )

    candidates.sort(key=lambda item: (item[0], item[1]))
    token_counts = Counter(
        str(source.get("event_shop_filter") or "").lower()
        for _, _, _, source, _ in candidates
    )
    safe_candidates = []
    for candidate in candidates:
        _, _, row_id, source, _ = candidate
        token = str(source.get("event_shop_filter") or "")
        if not token:
            blocked[row_id] = "Для этого товара пока нет безопасного правила покупки"
            continue
        if token_counts[token.lower()] > 1:
            blocked[row_id] = "Одинаковые товары нельзя безопасно развести по разным приоритетам"
            continue
        safe_candidates.append(candidate)

    state["blocked"] = blocked
    save_event_shop_priority(config.config_name, state, root=root)

    filter_tokens = [
        str(source.get("event_shop_filter") or "")
        for _, _, _, source, _ in safe_candidates
    ]
    config.override(
        EventShop_PriorityMode=True,
        EventShop_PresetFilter="custom",
        EventShop_CustomFilter=" > ".join(filter_tokens),
        EventShop_BuyURShip=0,
        EventShop_UnlockSSRShip=False,
    )
    selected = [runtime for _, _, _, _, runtime in safe_candidates]
    return PriorityRuntimeItems(selected, observation_items=full_scan)


def confirm_event_shop_purchase(
    config: Any,
    runtime_item: Any,
    *,
    full_purchase: bool,
    remaining_after: int | None = None,
    root: Path | str = EVENT_SHOP_PRIORITY_ROOT,
) -> None:
    """Persist the confirmed remaining count and clear priority after a full purchase."""
    spec = _current_spec(config)
    if spec is None:
        return
    rows, _ = reconcile_event_shop(spec, [runtime_item])
    if len(rows) != 1:
        return
    row = rows[0]
    if row.get("status") != "matched" or row.get("row_id") is None:
        return
    event_id = str(spec.get("id") or "")
    state = load_event_shop_priority(config.config_name, event_id, root=root)
    row_id = str(row["row_id"])
    state["blocked"].pop(row_id, None)
    if remaining_after is not None:
        state["remaining"][row_id] = max(int(remaining_after), 0)
    if full_purchase:
        state["priorities"].pop(row_id, None)
        state["remaining"][row_id] = 0
        state["purchased"] = sorted(set(state["purchased"]) | {row_id})
        logger.info(
            f"[Магазин события — приоритеты] Покупка строки {row_id} подтверждена; приоритет сброшен"
        )
    else:
        state["purchased"] = [item for item in state["purchased"] if item != row_id]
    save_event_shop_priority(config.config_name, state, root=root)
