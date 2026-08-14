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
from module.webui.event_source import load_event_user_state, save_event_user_state

EVENT_SHOP_PRIORITY_SCHEMA_VERSION = 4
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
        "completed": [],
        "remaining": {},
        "target_baselines": {},
        "blocked": {},
        "pending": {},
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
    for field in ("purchased", "completed"):
        values = raw.get(field)
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
            result[field] = sorted(
                {str(row_id) for row_id in values if str(row_id).strip()}
            )
    for field in ("remaining", "target_baselines"):
        values = raw.get(field)
        if not isinstance(values, Mapping):
            continue
        for row_id, value in values.items():
            try:
                count = int(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if count >= 0:
                result[field][str(row_id)] = count
    blocked = raw.get("blocked")
    if isinstance(blocked, Mapping):
        result["blocked"] = {
            str(row_id): str(message)
            for row_id, message in blocked.items()
            if str(row_id).strip() and str(message).strip()
        }
    pending = raw.get("pending")
    if isinstance(pending, Mapping):
        row_id = str(pending.get("row_id") or "").strip()
        try:
            before_remaining = int(pending.get("before_remaining"))
            expected_remaining = int(pending.get("expected_remaining"))
        except (TypeError, ValueError, OverflowError):
            row_id = ""
        if (
            row_id
            and before_remaining >= 0
            and expected_remaining >= 0
            and expected_remaining <= before_remaining
        ):
            result["pending"] = {
                "row_id": row_id,
                "before_remaining": before_remaining,
                "expected_remaining": expected_remaining,
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
        state["target_baselines"].pop(key, None)
    else:
        value = int(priority)
        if value < 0:
            raise ValueError("Приоритет покупки не может быть отрицательным")
        state["priorities"][key] = value
        state["purchased"] = [item for item in state["purchased"] if item != key]
        state["completed"] = [item for item in state["completed"] if item != key]
        if key not in state["target_baselines"] and key in state["remaining"]:
            state["target_baselines"][key] = max(
                int(state["remaining"][key]),
                0,
            )
    if str(state.get("pending", {}).get("row_id") or "") != key:
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


def _selected_targets(config: Any, event_id: str) -> dict[str, int]:
    """Read the user's quantity goals for this exact event."""
    state = load_event_user_state(config.config_name)
    saved_event_id = str(state.get("source_event_id") or "")
    if saved_event_id and saved_event_id != str(event_id or ""):
        return {}
    selections = state.get("shop_selections")
    if not isinstance(selections, Mapping):
        return {}
    result: dict[str, int] = {}
    for row_id, value in selections.items():
        try:
            selected = max(int(value or 0), 0)
        except (TypeError, ValueError, OverflowError):
            continue
        result[str(row_id)] = selected
    return result


def _clear_selected_target(
    config: Any,
    event_id: str,
    row_id: str,
    expected_selected: int,
) -> bool:
    """Clear exactly the goal that was verified, never a newer user edit."""
    state = load_event_user_state(config.config_name)
    saved_event_id = str(state.get("source_event_id") or "")
    if saved_event_id and saved_event_id != str(event_id or ""):
        return False
    selections = state.get("shop_selections")
    if not isinstance(selections, Mapping):
        return False
    key = str(row_id)
    try:
        current = max(int(selections.get(key, 0) or 0), 0)
    except (TypeError, ValueError, OverflowError):
        return False
    if current != max(int(expected_selected), 0):
        return False
    updated = dict(state)
    updated_selections = dict(selections)
    updated_selections[key] = 0
    updated["shop_selections"] = updated_selections
    if not str(updated.get("source_event_id") or ""):
        updated["source_event_id"] = str(event_id or "")
    save_event_user_state(config.config_name, updated)
    return True


def _target_remaining(
    source: Mapping[str, Any],
    runtime: Any,
    selected: int,
    baseline_remaining: int | None = None,
) -> int:
    """Return how many units remain in the current user goal."""
    stock = max(int(source.get("stock", 0) or 0), 0)
    current = max(int(getattr(runtime, "count", 0) or 0), 0)
    current = min(current, stock)
    if baseline_remaining is None:
        baseline = current
    else:
        baseline = min(max(int(baseline_remaining), 0), stock)
        baseline = max(baseline, current)
    target = min(max(int(selected), 0), baseline)
    bought_for_goal = max(baseline - current, 0)
    return max(target - bought_for_goal, 0)


def _runtime_filter(item: Any) -> str:
    return "".join(
        str(getattr(item, field, "") or "")
        for field in ("group", "sub_genre", "tier")
    )


def _verify_pending_purchase(
    state: dict[str, Any],
    *,
    config: Any,
    selected_targets: Mapping[str, int],
    runtime_by_row: Mapping[str, Any],
    ambiguous_filters: set[str],
    catalog_by_id: Mapping[str, Mapping[str, Any]],
) -> tuple[bool, str]:
    """Verify the previous click only from the next complete scanner snapshot."""
    pending = state.get("pending")
    if not isinstance(pending, Mapping) or not pending:
        return True, ""

    row_id = str(pending.get("row_id") or "")
    before = int(pending.get("before_remaining", 0) or 0)
    expected = int(pending.get("expected_remaining", 0) or 0)
    source = catalog_by_id.get(row_id)
    token = str(source.get("event_shop_filter") or "").lower() if source else ""
    runtime = runtime_by_row.get(row_id)

    if expected == 0 and runtime is None and token not in ambiguous_filters:
        after = 0
    elif runtime is None:
        return (
            False,
            "После покупки товар не удалось однозначно проверить повторным сканированием",
        )
    else:
        after = max(int(getattr(runtime, "count", 0) or 0), 0)

    if after != expected:
        if after == before:
            return False, "После покупки повторный скан не подтвердил изменение остатка"
        return (
            False,
            f"После покупки ожидался остаток {expected}, повторный скан показал {after}",
        )

    state["remaining"][row_id] = after
    state["blocked"].pop(row_id, None)
    stock = max(int(source.get("stock", 0) or 0), 0) if source else before
    selected = min(max(int(selected_targets.get(row_id, 0) or 0), 0), stock)
    baseline = min(
        max(int(state["target_baselines"].get(row_id, before) or 0), 0),
        stock,
    )
    baseline = max(baseline, after)
    goal_size = min(selected, baseline)
    bought_for_goal = max(baseline - min(after, baseline), 0)
    target_done = selected > 0 and bought_for_goal >= goal_size

    if after == 0:
        if selected > 0:
            _clear_selected_target(config, state["event_id"], row_id, selected)
        state["target_baselines"].pop(row_id, None)
        state["priorities"].pop(row_id, None)
        state["purchased"] = sorted(set(state["purchased"]) | {row_id})
        state["completed"] = [item for item in state["completed"] if item != row_id]
        logger.info(
            f"[Магазин события — приоритеты] Повторный скан подтвердил полный выкуп строки {row_id}; цель и приоритет завершены"
        )
    elif target_done:
        target_cleared = _clear_selected_target(
            config,
            state["event_id"],
            row_id,
            selected,
        )
        state["purchased"] = [item for item in state["purchased"] if item != row_id]
        if target_cleared:
            state["target_baselines"].pop(row_id, None)
            state["priorities"].pop(row_id, None)
            state["completed"] = sorted(set(state["completed"]) | {row_id})
            logger.info(
                f"[Магазин события — приоритеты] Повторный скан подтвердил выполнение цели строки {row_id}; цель и приоритет сброшены, остаток {after}"
            )
        else:
            state["completed"] = [item for item in state["completed"] if item != row_id]
            logger.info(
                f"[Магазин события — приоритеты] Остаток {after} подтверждён, но цель строки {row_id} была изменена параллельно; новая цель сохранена"
            )
    else:
        state["purchased"] = [item for item in state["purchased"] if item != row_id]
        state["completed"] = [item for item in state["completed"] if item != row_id]
        if selected <= 0:
            state["target_baselines"].pop(row_id, None)
            state["priorities"].pop(row_id, None)
            logger.info(
                f"[Магазин события — приоритеты] Повторный скан подтвердил остаток {after}; активная цель отсутствует, приоритет сброшен"
            )
        else:
            logger.info(
                f"[Магазин события — приоритеты] Повторный скан подтвердил остаток {after} для строки {row_id}"
            )
    state["pending"] = {}
    return True, ""


def prepare_event_shop_runtime_items(
    config: Any,
    runtime_items: Sequence[Any],
    *,
    root: Path | str = EVENT_SHOP_PRIORITY_ROOT,
) -> PriorityRuntimeItems:
    """Build one safe quantity-capped purchase step from a complete scan."""
    full_scan = list(runtime_items)
    spec = _current_spec(config)
    if spec is None:
        logger.warning(
            "[Магазин события — приоритеты] EventSpec недоступен; покупка заблокирована"
        )
        return PriorityRuntimeItems([], observation_items=full_scan)

    event_id = str(spec.get("id") or "")
    state = load_event_shop_priority(config.config_name, event_id, root=root)
    remembered_remaining_rows = set(state["remaining"])
    selected_targets = _selected_targets(config, event_id)
    rows, _ = reconcile_event_shop(spec, full_scan)
    runtime_by_row: dict[str, Any] = {}
    ambiguous_filters: set[str] = set()
    for runtime_row in rows:
        status = str(runtime_row.get("status") or "")
        token = str(runtime_row.get("filter") or "").lower()
        if status == "matched" and runtime_row.get("row_id") is not None:
            index = int(runtime_row["runtime_index"])
            runtime_by_row[str(runtime_row["row_id"])] = full_scan[index]
        elif status == "ambiguous" and token:
            ambiguous_filters.add(token)

    catalog = _catalog_rows(spec)
    catalog_by_id = {str(item.get("row_id")): item for item in catalog}
    pending_ok, pending_problem = _verify_pending_purchase(
        state,
        config=config,
        selected_targets=selected_targets,
        runtime_by_row=runtime_by_row,
        ambiguous_filters=ambiguous_filters,
        catalog_by_id=catalog_by_id,
    )
    if not pending_ok:
        pending_row = str(state.get("pending", {}).get("row_id") or "")
        if pending_row:
            state["blocked"] = {pending_row: pending_problem}
        save_event_shop_priority(config.config_name, state, root=root)
        config.override(
            EventShop_PriorityMode=True,
            EventShop_PresetFilter="custom",
            EventShop_CustomFilter="",
            EventShop_BuyURShip=0,
            EventShop_UnlockSSRShip=False,
        )
        logger.error(
            "[Магазин события — приоритеты] Проверка предыдущей покупки не пройдена; дальнейшие клики заблокированы"
        )
        return PriorityRuntimeItems([], observation_items=full_scan)

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

    candidates: list[
        tuple[int, int, str, Mapping[str, Any], Any, int]
    ] = []
    blocked: dict[str, str] = {}
    catalog_order = {
        str(item.get("row_id")): index for index, item in enumerate(catalog)
    }
    for row_id, priority in state["priorities"].items():
        if row_id in purchased:
            continue
        source = catalog_by_id.get(row_id)
        if source is None:
            continue

        selected = selected_targets.get(row_id, 0)
        if selected <= 0:
            if state["target_baselines"].pop(row_id, None) is not None:
                changed = True
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
            blocked[row_id] = (
                "Покупка за UR-очки пока недоступна в режиме приоритетов"
            )
            continue

        stock = max(int(source.get("stock", 0) or 0), 0)
        current = min(max(int(getattr(runtime, "count", 0) or 0), 0), stock)
        baseline = state["target_baselines"].get(row_id)
        if baseline is None:
            baseline = stock if row_id in remembered_remaining_rows else current
            state["target_baselines"][row_id] = baseline
            changed = True
        elif int(baseline) < current or int(baseline) > stock:
            baseline = current
            state["target_baselines"][row_id] = baseline
            changed = True

        remaining_goal = _target_remaining(
            source,
            runtime,
            selected,
            baseline_remaining=baseline,
        )
        if remaining_goal <= 0:
            continue

        candidates.append(
            (
                int(priority),
                catalog_order.get(row_id, 10**9),
                row_id,
                source,
                runtime,
                remaining_goal,
            )
        )

    candidates.sort(key=lambda item: (item[0], item[1]))
    token_counts = Counter(
        str(source.get("event_shop_filter") or "").lower()
        for _, _, _, source, _, _ in candidates
    )
    safe_candidates = []
    for candidate in candidates:
        _, _, row_id, source, _, _ = candidate
        token = str(source.get("event_shop_filter") or "")
        if not token:
            blocked[row_id] = "Для этого товара пока нет безопасного правила покупки"
            continue
        if token_counts[token.lower()] > 1:
            blocked[row_id] = (
                "Одинаковые товары нельзя безопасно развести по разным приоритетам"
            )
            continue
        safe_candidates.append(candidate)

    state["blocked"] = blocked
    save_event_shop_priority(config.config_name, state, root=root)

    # One scanner snapshot authorizes at most one logical purchase. Quantity
    # comes from the user's target and is capped against purchases made since
    # this target was established.
    next_candidate = safe_candidates[:1]
    filter_tokens = []
    for _, _, _, source, runtime, remaining_goal in next_candidate:
        token = str(source.get("event_shop_filter") or "")
        runtime_remaining = max(int(getattr(runtime, "count", 0) or 0), 0)
        if remaining_goal < runtime_remaining:
            token = f"{token}:{remaining_goal}"
        filter_tokens.append(token)

    config.override(
        EventShop_PriorityMode=True,
        EventShop_PresetFilter="custom",
        EventShop_CustomFilter=" > ".join(filter_tokens),
        EventShop_BuyURShip=0,
        EventShop_UnlockSSRShip=False,
    )
    selected = [runtime for _, _, _, _, runtime, _ in next_candidate]
    return PriorityRuntimeItems(selected, observation_items=full_scan)


def confirm_event_shop_purchase(
    config: Any,
    runtime_item: Any,
    *,
    full_purchase: bool,
    remaining_after: int | None = None,
    root: Path | str = EVENT_SHOP_PRIORITY_ROOT,
) -> None:
    """Record an expected result and force a fresh scan before another click."""
    del full_purchase
    spec = _current_spec(config)
    if spec is None:
        return
    rows, _ = reconcile_event_shop(spec, [runtime_item])
    if len(rows) != 1:
        return
    row = rows[0]
    if row.get("status") != "matched" or row.get("row_id") is None:
        return

    before = max(int(getattr(runtime_item, "count", 0) or 0), 0)
    if remaining_after is None:
        raise ValueError("Для проверки покупки требуется ожидаемый остаток")
    expected = int(remaining_after)
    if expected < 0 or expected > before:
        raise ValueError("Ожидаемый остаток покупки вне допустимого диапазона")

    event_id = str(spec.get("id") or "")
    state = load_event_shop_priority(config.config_name, event_id, root=root)
    row_id = str(row["row_id"])
    state["pending"] = {
        "row_id": row_id,
        "before_remaining": before,
        "expected_remaining": expected,
    }
    state["blocked"].pop(row_id, None)
    save_event_shop_priority(config.config_name, state, root=root)
    logger.info(
        f"[Магазин события — приоритеты] Покупка строки {row_id} ожидает повторного полного сканирования: {before} -> {expected}"
    )

    # Stop this pass immediately. Scheduler will enter EventShop again and
    # scan the whole shop before prepare_event_shop_runtime_items authorizes
    # another purchase.
    config.task_call("EventShop", force_call=True)
    config.task_stop("Повторное сканирование магазина после покупки")


def wake_event_shop_after_currency_increase(
    *,
    instance: str,
    event_id: str,
    previous_value: int | None,
    current_value: int | None,
    source: str,
    root: Path | str = EVENT_SHOP_PRIORITY_ROOT,
) -> bool:
    """Wake EventShop only when a still-unfulfilled quantity target exists."""
    if str(source or "") == "event_shop_ocr":
        return False
    if previous_value is None or current_value is None or current_value <= previous_value:
        return False

    state = load_event_shop_priority(instance, event_id, root=root)
    possible = (
        set(state["priorities"]) - set(state["purchased"]) - set(state["blocked"])
    )
    if not possible:
        return False

    try:
        from module.config.config import AzurLaneConfig

        config = AzurLaneConfig(config_name=instance)
        if not config.is_task_enabled("EventShop"):
            return False

        spec = _current_spec(config)
        if not isinstance(spec, Mapping) or str(spec.get("id") or "") != str(event_id):
            return False
        targets = _selected_targets(config, event_id)
        catalog_by_id = {
            str(item.get("row_id")): item for item in _catalog_rows(spec)
        }

        active = False
        for row_id in possible:
            source_row = catalog_by_id.get(row_id)
            if source_row is None:
                continue
            stock = max(int(source_row.get("stock", 0) or 0), 0)
            observed_remaining = min(
                max(int(state["remaining"].get(row_id, stock) or 0), 0),
                stock,
            )
            saved_baseline = state["target_baselines"].get(row_id)
            if saved_baseline is None:
                baseline = stock
            else:
                baseline = min(max(int(saved_baseline), 0), stock)
                baseline = max(baseline, observed_remaining)
            selected = min(max(int(targets.get(row_id, 0)), 0), baseline)
            bought_for_goal = max(baseline - observed_remaining, 0)
            if selected > bought_for_goal:
                active = True
                break
        if not active:
            return False

        called = bool(config.task_call("EventShop", force_call=False))
    except Exception as exc:
        logger.warning(
            f"[Магазин события — приоритеты] Не удалось разбудить EventShop после роста PT: {exc}"
        )
        return False

    if called:
        logger.info(
            f"[Магазин события — приоритеты] Баланс PT вырос {previous_value} -> {current_value}; EventShop поставлен на ближайший запуск"
        )
    return called
