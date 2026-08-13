"""Safe translation from visual EventPlan shop rows to the legacy EventShop DSL.

EventPlan remains provider-neutral and may contain source-provided display data.
Only this bridge knows the runtime selector grammar. It validates every selected
filter token against the real EventShop regex, canonicalizes equivalent spellings,
and emits amount suffixes only when the runtime can represent the visual choice
without ambiguity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Tuple

from module.shop_event.selector import FILTER_REGEX
from module.webui.event_plan import normalize_event_plan


# These categories are removed from the normal filtered item list by EventShop's
# UR-point pre-processing. A visual selector cannot control them reliably without
# changing that runtime contract, so Phase 2 keeps them fail-closed.
_SPECIAL_RUNTIME_TOKENS = frozenset({"shipur", "ptur"})


def _ambiguous_runtime_selector(match: re.Match[str]) -> bool:
    """Reject broad categories that can select several unrelated runtime rows."""
    group, sub_genre, tier = match.groups()
    if group in {"ship", "equip", "pt"}:
        return sub_genre is None
    if group in {"cat", "expbook", "box", "food"}:
        return tier is None
    if group == "augment":
        return sub_genre is None
    if group == "plate":
        return sub_genre is None or tier is None
    if group in {"pr", "dr"}:
        return tier is None
    return False


@dataclass(frozen=True)
class EventShopAutomationPlan:
    filter_text: str
    tokens: Tuple[str, ...]
    invalid_items: Tuple[str, ...]
    conflicts: Mapping[str, Tuple[str, ...]]

    @property
    def safe(self) -> bool:
        return bool(self.tokens) and not self.invalid_items and not self.conflicts


def canonical_event_shop_filter_token(value: Any) -> str | None:
    """Return one canonical EventShop selector token or None for unsafe input.

    The visual plan stores only a selector identity. Quantity is derived from the
    visual ``selected`` field, so embedded ``:N`` limits and ``>`` chains are
    deliberately rejected instead of being passed through to runtime config.
    """
    raw = str(value or "").strip()
    if not raw or ">" in raw or ":" in raw:
        return None
    compact = re.sub(r"\s+", "", raw).lower()
    match = FILTER_REGEX.fullmatch(compact)
    if match is None or _ambiguous_runtime_selector(match):
        return None
    return "".join(part or "" for part in match.groups())


def build_event_shop_automation_plan(plan: Mapping[str, Any]) -> EventShopAutomationPlan:
    """Compile selected visual rows into a fail-closed EventShop custom filter."""
    items = normalize_event_plan(plan)["shop_items"]

    canonical_by_index: Dict[int, str | None] = {}
    invalid_items = []
    by_token: Dict[str, list[dict[str, Any]]] = {}

    for index, item in enumerate(items):
        canonical = canonical_event_shop_filter_token(item.get("filter"))
        canonical_by_index[index] = canonical
        selected = item["selected"] > 0
        if selected and (canonical is None or canonical in _SPECIAL_RUNTIME_TOKENS):
            invalid_items.append(item["name"])
        if canonical is not None and canonical not in _SPECIAL_RUNTIME_TOKENS:
            by_token.setdefault(canonical, []).append(item)

    conflicts: Dict[str, Tuple[str, ...]] = {}
    for token, bucket in by_token.items():
        selected = [item for item in bucket if item["selected"] > 0]
        unselected = [item for item in bucket if item["selected"] == 0]
        shared_partial = len(bucket) > 1 and any(
            0 < item["selected"] < item["stock"] for item in bucket
        )
        if (selected and unselected) or shared_partial:
            conflicts[token] = tuple(item["name"] for item in bucket)

    tokens = []
    seen = set()
    for index, item in enumerate(items):
        if item["selected"] <= 0:
            continue
        canonical = canonical_by_index[index]
        if (
            canonical is None
            or canonical in _SPECIAL_RUNTIME_TOKENS
            or canonical in seen
            or canonical in conflicts
        ):
            continue
        seen.add(canonical)
        bucket = by_token[canonical]
        if len(bucket) == 1 and item["selected"] < item["stock"]:
            tokens.append(f"{canonical}:{item['selected']}")
        else:
            tokens.append(canonical)

    return EventShopAutomationPlan(
        filter_text=" > ".join(tokens),
        tokens=tuple(tokens),
        invalid_items=tuple(invalid_items),
        conflicts=conflicts,
    )
