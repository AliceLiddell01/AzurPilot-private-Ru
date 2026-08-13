"""Типизированная, детерминированно сериализуемая модель Event Datamine."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Severity = Literal["error", "warning", "info"]
SourceStatus = Literal["verified", "partial", "unsupported"]
PtSourceKind = Literal[
    "daily",
    "weekly",
    "one_time",
    "first_clear",
    "daily_first_clear",
    "repeatable_map_clear",
    "challenge",
    "unknown",
]


@dataclass(frozen=True)
class ValidationFinding:
    code: str
    severity: Severity
    message: str
    path: str = ""


@dataclass(frozen=True)
class Provenance:
    provider: str
    repository: str
    revision: str
    server: str
    activity_id: int
    schema_version: int = 1


@dataclass(frozen=True)
class AssetReference:
    kind: str
    game_id: str
    source_path: str = ""
    resolved: bool = False


@dataclass(frozen=True)
class RewardSpec:
    reward_type: int
    reward_id: int
    amount: int
    name: str
    rarity: int | None = None
    asset: AssetReference | None = None


@dataclass(frozen=True)
class CurrencySpec:
    id: int
    name: str
    asset: AssetReference
    runtime_token: str = ""


@dataclass(frozen=True)
class ShopItemSpec:
    row_id: int
    item_type: int
    item_id: int
    amount: int
    price: int
    currency_id: int
    stock: int
    name: str
    category: str
    rarity: int | None
    event_shop_filter: str
    asset: AssetReference
    limit_args: Any = ""


@dataclass(frozen=True)
class MilestoneSpec:
    threshold: int
    rewards: tuple[RewardSpec, ...]


@dataclass(frozen=True)
class PtSourceSpec:
    id: str
    kind: str
    name: str
    points: int | None
    recurring: bool
    source_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class PortalSpec:
    source: str
    target: str


@dataclass(frozen=True)
class MapSpec:
    id: int
    chapter_name: str
    name: str
    shape: str
    map_data: tuple[tuple[str, ...], ...]
    map_data_loop: tuple[tuple[str, ...], ...] | None
    spawn_data: tuple[dict[str, int], ...]
    spawn_data_loop: tuple[dict[str, int], ...] | None
    camera_data: tuple[str, ...]
    camera_spawn_points: tuple[str, ...]
    boss_refresh: int
    siren_templates: tuple[str, ...]
    movable_enemy_turns: tuple[int, ...]
    land_based: tuple[tuple[str, str], ...]
    portals: tuple[PortalSpec, ...]
    star_requirements: tuple[int, int, int]
    has_story: bool
    has_fleet_step: bool
    has_ambush: bool
    has_mystery: bool
    unknown_grid_types: tuple[int, ...] = ()
    unknown_effects: tuple[str, ...] = ()
    compatibility_patch_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class EventSpec:
    id: str
    name: str
    server: str
    farm_start: str
    farm_end: str
    shop_end: str
    source_status: SourceStatus
    provenance: Provenance
    related_activity_ids: tuple[int, ...]
    currencies: tuple[CurrencySpec, ...]
    maps: tuple[MapSpec, ...]
    shop_items: tuple[ShopItemSpec, ...]
    milestones: tuple[MilestoneSpec, ...]
    pt_sources: tuple[PtSourceSpec, ...]
    findings: tuple[ValidationFinding, ...] = field(default_factory=tuple)

    @property
    def eligible(self) -> bool:
        return self.source_status != "unsupported" and not any(
            item.severity == "error" for item in self.findings
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["eligible"] = self.eligible
        return data
