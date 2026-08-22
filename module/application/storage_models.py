"""Неизменяемые transport-neutral модели PostgreSQL foundation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID


class StorageHealthState(StrEnum):
    READY = "ready"
    UNAVAILABLE = "unavailable"
    AUTHENTICATION_FAILED = "authentication_failed"
    INCOMPATIBLE_SCHEMA = "incompatible_schema"


class ImportBatchStatus(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    CONFLICT = "conflict"


class MonthlyMetric(StrEnum):
    BATTLE_COUNT = "battle_count"
    AKASHI_ENCOUNTERS = "akashi_encounters"
    MEOW_BATTLE_RAW_COUNT = "meow_battle_raw_count"
    MEOW_BATTLE_COUNT = "meow_battle_count"


@dataclass(frozen=True, slots=True)
class StorageHealth:
    state: StorageHealthState
    schema_head: str | None = None


@dataclass(frozen=True, slots=True)
class InstanceIdentity:
    id: UUID
    name: str


@dataclass(frozen=True, slots=True)
class MonthlyAggregate:
    instance_id: UUID
    month: date
    metric: MonthlyMetric
    value: Decimal
    version: int


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    id: UUID
    instance_id: UUID
    idempotency_key: str
    observed_at: datetime | None
    source: str
    oil: int | None = None
    coin: int | None = None
    gem: int | None = None
    pt: int | None = None
    cube: int | None = None
    core: int | None = None
    medal: int | None = None
    merit: int | None = None
    guild_coin: int | None = None
    action_point: int | None = None
    yellow_coin: int | None = None
    purple_coin: int | None = None
    legacy_timestamp_text: str | None = None
    legacy_timezone: str | None = None


@dataclass(frozen=True, slots=True)
class OpsiItemEvent:
    id: UUID
    instance_id: UUID
    idempotency_key: str
    observed_at: datetime
    imgid: str
    genre: str
    item_code: str
    amount: int
    server: str | None = None
    zone: str | None = None
    zone_type: str | None = None
    zone_id: int | None = None
    hazard_level: int | None = None
    tag: str | None = None
    combat_count: int | None = None


@dataclass(frozen=True, slots=True)
class CommissionItem:
    item_code: str
    amount: int


@dataclass(frozen=True, slots=True)
class CommissionIncome:
    id: UUID
    instance_id: UUID
    idempotency_key: str
    observed_at: datetime | None
    commission_count: int
    source: str
    items: tuple[CommissionItem, ...]
    legacy_timestamp_text: str | None = None
    legacy_timezone: str | None = None


@dataclass(frozen=True, slots=True)
class ImportBatch:
    id: UUID
    idempotency_key: str
    source_kind: str
    source_digest: str
    status: ImportBatchStatus
    started_at: datetime
    finished_at: datetime | None = None
    record_count: int = 0
    imported_count: int = 0
    conflict_count: int = 0
    quarantine_count: int = 0
