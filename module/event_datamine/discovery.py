"""Структурное обнаружение major campaign events без знания их ID или имён."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from module.event_datamine.map_compiler import _values
from module.event_datamine.source import ShareCfgError, ShareCfgLoader


class EventDiscoveryError(ValueError):
    """Fail-closed ошибка structural discovery/current selection."""

    def __init__(
        self, code: str, message: str, *, candidates: Sequence[str] = ()
    ) -> None:
        super().__init__(message)
        self.code = code
        self.candidates = tuple(candidates)


@dataclass(frozen=True)
class EventCandidate:
    id: str
    server: str
    activity_id: int
    mark: int
    name: str
    farm_start: str
    farm_end: str
    shop_end: str
    campaign_activity_ids: tuple[int, ...]
    related_activity_ids: tuple[int, ...]
    map_ids: tuple[int, ...]
    supported: bool = True


def _date_part(value: Any) -> str:
    date = value.get(0, {}) if isinstance(value, Mapping) else {}
    clock = value.get(1, {}) if isinstance(value, Mapping) else {}
    try:
        return datetime(
            int(date.get(0)),
            int(date.get(1)),
            int(date.get(2)),
            int(clock.get(0, 0)),
            int(clock.get(1, 0)),
            int(clock.get(2, 0)),
        ).isoformat(sep=" ")
    except (AttributeError, TypeError, ValueError):
        return ""


def activity_times(row: Mapping[str, Any]) -> tuple[str, str]:
    value = row.get("time")
    parts = _values(value)
    return (
        _date_part(parts[1]) if len(parts) > 1 else "",
        _date_part(parts[2]) if len(parts) > 2 else "",
    )


def _linked_name(
    activity_id: int,
    memories: Mapping[int, Any],
    medals: Mapping[int, Any],
) -> str:
    for row in memories.values():
        if (
            isinstance(row, Mapping)
            and int(row.get("link_event", 0) or 0) == activity_id
        ):
            title = str(row.get("title") or "").strip()
            if title:
                return title
    for row in medals.values():
        if not isinstance(row, Mapping):
            continue
        for link in _values(row.get("activity_link")):
            values = _values(link)
            if len(values) > 1 and int(values[1] or 0) == activity_id:
                title = str(row.get("group_name") or "").strip()
                if title:
                    return title
    return ""


def discover_major_events(source: ShareCfgLoader) -> tuple[EventCandidate, ...]:
    """Find coherent map-backed activity groups using only ShareCfg relations.

    Activity type 12 is the established ShareCfg relation that owns campaign map
    IDs.  A group becomes a candidate only when those IDs resolve in
    ``chapter_template`` and one campaign activity is structurally identifiable
    as the named root.  No name, ID range, newest-ID or internal view-class
    heuristic participates in selection.
    """

    activities = source.load_table("activity_template")
    chapters = source.load_table("chapter_template")
    try:
        memories = source.load_table("memory_group")
    except ShareCfgError:
        memories = {}
    try:
        medals = source.load_table("activity_medal_group")
    except ShareCfgError:
        medals = {}

    groups: dict[int, list[tuple[int, Mapping[str, Any]]]] = defaultdict(list)
    for activity_id, row in activities.items():
        if not isinstance(row, Mapping):
            continue
        mark = int(row.get("mark", 0) or 0)
        if mark:
            groups[mark].append((int(activity_id), row))

    candidates: list[EventCandidate] = []
    for mark, related in groups.items():
        campaign_rows: list[tuple[int, Mapping[str, Any], tuple[int, ...]]] = []
        for activity_id, row in related:
            if int(row.get("type", 0) or 0) != 12:
                continue
            map_ids = tuple(
                int(value)
                for value in _values(row.get("config_data"))
                if isinstance(value, int) and int(value) in chapters
            )
            if map_ids:
                campaign_rows.append((activity_id, row, map_ids))
        if not campaign_rows:
            continue

        named_roots = [
            (activity_id, row, map_ids, _linked_name(activity_id, memories, medals))
            for activity_id, row, map_ids in campaign_rows
            if _linked_name(activity_id, memories, medals)
        ]
        supported = len(named_roots) == 1 or (
            not named_roots and len(campaign_rows) == 1
        )
        if len(named_roots) == 1:
            root_id, root, _, name = named_roots[0]
        elif len(campaign_rows) == 1:
            root_id, root, _ = campaign_rows[0]
            name = f"Activity {root_id}"
        else:
            root_id, root, _ = campaign_rows[0]
            name = f"Activity group mark={mark}"

        campaign_times = [activity_times(item[1]) for item in campaign_rows]
        starts = [item[0] for item in campaign_times if item[0] and item[1]]
        ends = [item[1] for item in campaign_times if item[0] and item[1]]
        if not starts or not ends:
            continue
        farm_start, farm_end = activity_times(root)
        if not farm_start or not farm_end:
            supported = False
        if not supported:
            farm_start, farm_end = min(starts), max(ends)
        shop_ends = [
            activity_times(row)[1]
            for _, row in related
            if int(row.get("type", 0) or 0) == 14
            and _values(row.get("config_data"))
            and activity_times(row)[1]
        ]
        shop_end = max(shop_ends, default=farm_end)
        all_maps = tuple(
            dict.fromkeys(
                map_id for _, _, map_ids in campaign_rows for map_id in map_ids
            )
        )
        candidates.append(
            EventCandidate(
                id=f"{source.snapshot.server.lower()}:{root_id}",
                server=source.snapshot.server,
                activity_id=root_id,
                mark=mark,
                name=name,
                farm_start=farm_start,
                farm_end=farm_end,
                shop_end=shop_end,
                campaign_activity_ids=tuple(
                    activity_id for activity_id, _, _ in campaign_rows
                ),
                related_activity_ids=tuple(sorted(item[0] for item in related)),
                map_ids=all_maps,
                supported=supported,
            )
        )
    return tuple(sorted(candidates, key=lambda item: (item.farm_start, item.id)))


def lifecycle(candidate: EventCandidate, now: datetime) -> str:
    start = datetime.fromisoformat(candidate.farm_start)
    farm_end = datetime.fromisoformat(candidate.farm_end)
    shop_end = datetime.fromisoformat(candidate.shop_end)
    current = now.replace(tzinfo=None) if now.tzinfo is not None else now
    if current < start:
        return "upcoming"
    if current <= farm_end:
        return "active"
    if current <= shop_end:
        return "redemption"
    return "expired"


def resolve_current_candidate(
    candidates: Sequence[EventCandidate], *, server: str, now: datetime
) -> EventCandidate | None:
    relevant = [item for item in candidates if item.server == server.upper()]
    for phase in ("active", "redemption"):
        matches = [item for item in relevant if lifecycle(item, now) == phase]
        if len(matches) == 1:
            if not matches[0].supported:
                raise EventDiscoveryError(
                    "ambiguous_campaign_root",
                    f"Current activity group mark={matches[0].mark} не содержит однозначный campaign root",
                    candidates=[
                        f"{matches[0].server.lower()}:{activity_id}"
                        for activity_id in matches[0].campaign_activity_ids
                    ],
                )
            return matches[0]
        if len(matches) > 1:
            raise EventDiscoveryError(
                "ambiguous_active_event",
                f"Для {server.upper()} обнаружено несколько событий lifecycle={phase}",
                candidates=[item.id for item in matches],
            )
    return None
