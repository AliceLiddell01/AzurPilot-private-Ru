"""Production-контракты и сервисы статистики в PostgreSQL."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from threading import Lock
from typing import Protocol, TypeVar
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from module.application.errors import StorageConfigurationError
from module.application.storage_models import (
    CommissionIncome,
    CommissionItem,
    InstanceIdentity,
    MonthlyAggregate,
    MonthlyMetric,
    OpsiItemEvent,
    ResourceSnapshot,
)
from module.application.storage_ports import StorageUnitOfWork

_IDENTITY_NAMESPACE = UUID("bc6db2da-cb91-4d6e-bc33-bb598d715c13")
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class ApSnapshot:
    observed_at: datetime | None
    ap: int
    ap_total: int | None
    asset: Decimal | None
    yellow_coin: int | None
    distance: int | None
    source: str


@dataclass(frozen=True, slots=True)
class ApPurchase:
    observed_at: datetime | None
    amount: int
    base_amount: int
    purchase_count: int
    source: str


@dataclass(frozen=True, slots=True)
class CurrencySnapshot:
    observed_at: datetime | None
    currency_code: str
    amount: int
    source: str


@dataclass(frozen=True, slots=True)
class MeowTimingSample:
    observed_at: datetime | None
    sample_kind: str
    duration_seconds: Decimal
    hazard_level: int | None


@dataclass(frozen=True, slots=True)
class MeowHazardSummary:
    hazard_level: int
    battle_count: int
    effective_rounds: Decimal
    round_times: tuple[Decimal, ...]
    battle_times: tuple[Decimal, ...]
    siren_research_devices: int


@dataclass(frozen=True, slots=True)
class MonthlyStatistics:
    month: date
    metrics: tuple[MonthlyAggregate, ...]
    ap_snapshots: tuple[ApSnapshot, ...]
    ap_purchases: tuple[ApPurchase, ...]
    currency_snapshots: tuple[CurrencySnapshot, ...]
    meow_timings: tuple[MeowTimingSample, ...]
    meow_hazards: tuple[MeowHazardSummary, ...]
    siren_cl1_devices: int

    def metric(self, metric: MonthlyMetric) -> Decimal:
        for item in self.metrics:
            if item.metric is metric:
                return item.value
        return Decimal(0)


@dataclass(frozen=True, slots=True)
class ApNotification:
    last_ap: int
    notified_at: datetime | None
    version: int


@dataclass(frozen=True, slots=True)
class CommissionEntry:
    observed_at: datetime | None
    commission_count: int
    items: tuple[CommissionItem, ...]


@dataclass(frozen=True, slots=True)
class OpsiItemProjection:
    observed_at: datetime
    imgid: str
    genre: str
    item_code: str
    amount: int
    server: str | None
    zone: str | None
    zone_type: str | None
    zone_id: int | None
    hazard_level: int | None
    tag: str | None
    combat_count: int | None


class RuntimeStatisticsRepository(Protocol):
    def monthly_statistics(
        self,
        instance_id: UUID,
        month: date,
        *,
        start: datetime,
        end: datetime,
    ) -> MonthlyStatistics: ...

    def append_ap_snapshot(
        self, instance_id: UUID, *, idempotency_key: str, snapshot: ApSnapshot
    ) -> bool: ...

    def append_ap_purchase(
        self,
        instance_id: UUID,
        *,
        idempotency_key: str,
        observed_at: datetime,
        amount: int,
        base_amount: int,
        purchase_count: int,
        source: str,
    ) -> bool: ...

    def append_currency_snapshot(
        self,
        instance_id: UUID,
        *,
        idempotency_key: str,
        snapshot: CurrencySnapshot,
    ) -> bool: ...

    def last_currency_amount(self, instance_id: UUID, currency_code: str) -> int | None: ...

    def record_meow_battle(
        self, instance_id: UUID, month: date, hazard_level: int, effective_delta: Decimal
    ) -> None: ...

    def append_meow_timing(
        self,
        instance_id: UUID,
        month: date,
        *,
        idempotency_key: str,
        sample: MeowTimingSample,
    ) -> bool: ...

    def record_siren_research_device(
        self,
        instance_id: UUID,
        month: date,
        *,
        idempotency_key: str,
        observed_at: datetime,
        source: str,
        hazard_level: int | None,
    ) -> bool: ...

    def get_ap_notification(self, instance_id: UUID) -> ApNotification | None: ...

    def set_ap_notification(
        self, instance_id: UUID, *, value: int, notified_at: datetime
    ) -> ApNotification: ...

    def commission_entries(
        self, instance_id: UUID, *, start: datetime, end: datetime, limit: int
    ) -> tuple[CommissionEntry, ...]: ...

    def opsi_items(
        self, instance_id: UUID, *, genre: str, limit: int
    ) -> tuple[OpsiItemProjection, ...]: ...


class RuntimeStorageUnitOfWork(StorageUnitOfWork, Protocol):
    runtime: RuntimeStatisticsRepository


class RuntimeStorageService:
    """Короткие fail-closed транзакции production-статистики."""

    def __init__(
        self,
        uow_factory: Callable[[], RuntimeStorageUnitOfWork],
        *,
        runtime_timezone: ZoneInfo | None = None,
    ):
        self._uow_factory = uow_factory
        self._timezone = runtime_timezone or ZoneInfo("UTC")

    def _month(self, value: datetime | None = None) -> date:
        value = (value or datetime.now(UTC)).astimezone(self._timezone)
        return date(value.year, value.month, 1)

    def current_datetime(self) -> datetime:
        """Вернуть текущее время в явно настроенном runtime-часовом поясе."""

        return datetime.now(self._timezone)

    @staticmethod
    def _observation_instant() -> datetime:
        """Вернуть границу секунды, общую для данных и ключа идемпотентности."""

        return datetime.now(UTC).replace(microsecond=0)

    def _month_range(self, year: int, month: int) -> tuple[datetime, datetime]:
        start = datetime(year, month, 1, tzinfo=self._timezone)
        if month == 12:
            end = datetime(year + 1, 1, 1, tzinfo=self._timezone)
        else:
            end = datetime(year, month + 1, 1, tzinfo=self._timezone)
        return start.astimezone(UTC), end.astimezone(UTC)

    @staticmethod
    def _identity_parts(instance: str) -> tuple[str, UUID]:
        if not isinstance(instance, str) or not instance or len(instance) > 128:
            raise StorageConfigurationError("Имя экземпляра хранилища некорректно.")
        digest = sha256(instance.encode("utf-8")).hexdigest()
        return digest, uuid5(_IDENTITY_NAMESPACE, digest)

    def _run(self, instance: str, operation: Callable[[RuntimeStorageUnitOfWork, UUID], _T]) -> _T:
        digest, identity_id = self._identity_parts(instance)
        with self._uow_factory() as uow:
            identity = uow.instances.resolve(
                alias_kind="legacy_instance", alias_digest=digest
            )
            if identity is None:
                identity = InstanceIdentity(identity_id, instance)
                uow.instances.register(
                    identity,
                    alias_kind="legacy_instance",
                    alias_digest=digest,
                    source_provenance="runtime_exact_profile",
                )
            elif identity.id != identity_id:
                raise StorageConfigurationError(
                    "Идентификатор экземпляра не совпадает с происхождением миграции."
                )
            result = operation(uow, identity.id)
            uow.commit()
            return result

    @staticmethod
    def _key(
        domain: str,
        instance: str,
        observed_at: datetime,
        payload: object,
    ) -> str:
        observation_window = (
            observed_at.astimezone(UTC).replace(microsecond=0).isoformat()
        )
        canonical = json.dumps(
            {
                "domain": domain,
                "instance": instance,
                "observation_window": observation_window,
                "payload": payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return f"runtime-v2:{domain}:{sha256(canonical.encode('utf-8')).hexdigest()}"

    def increment_monthly_counter(
        self, instance: str, metric: MonthlyMetric, delta: Decimal = Decimal(1)
    ) -> MonthlyAggregate:
        return self._run(
            instance,
            lambda uow, identity_id: uow.statistics.increment_monthly_counter(
                identity_id, self._month(), metric, delta
            ),
        )

    def monthly_statistics(
        self, instance: str, year: int, month: int
    ) -> MonthlyStatistics:
        month_value = date(year, month, 1)
        start, end = self._month_range(year, month)
        return self._run(
            instance,
            lambda uow, identity_id: uow.runtime.monthly_statistics(
                identity_id, month_value, start=start, end=end
            ),
        )

    def record_ap_purchase(
        self, instance: str, amount: int, base_amount: int, purchase_count: int, source: str
    ) -> bool:
        observed_at = self._observation_instant()

        def operation(uow: RuntimeStorageUnitOfWork, identity_id: UUID) -> bool:
            inserted = uow.runtime.append_ap_purchase(
                identity_id,
                idempotency_key=self._key(
                    "ap-purchase",
                    instance,
                    observed_at,
                    (amount, base_amount, purchase_count, source),
                ),
                observed_at=observed_at,
                amount=amount,
                base_amount=base_amount,
                purchase_count=purchase_count,
                source=source,
            )
            if inserted:
                uow.statistics.increment_monthly_counter(
                    identity_id,
                    self._month(observed_at),
                    MonthlyMetric.AKASHI_AP,
                    Decimal(amount),
                )
            return inserted

        return self._run(instance, operation)

    def record_ap_snapshot(
        self,
        instance: str,
        ap: int,
        *,
        source: str,
        distance: int | None = None,
        ap_total: int | None = None,
    ) -> bool:
        observed_at = self._observation_instant()

        def operation(uow: RuntimeStorageUnitOfWork, identity_id: UUID) -> bool:
            yellow = uow.runtime.last_currency_amount(identity_id, "yellow_coin") or 0
            asset_base = ap_total if ap_total is not None else ap
            snapshot = ApSnapshot(
                observed_at=observed_at,
                ap=int(ap),
                ap_total=None if ap_total is None else int(ap_total),
                asset=Decimal(asset_base) * Decimal(1700) / Decimal(30) + Decimal(yellow),
                yellow_coin=yellow,
                distance=None if distance is None else int(distance),
                source=source,
            )
            return uow.runtime.append_ap_snapshot(
                identity_id,
                idempotency_key=self._key(
                    "ap-snapshot",
                    instance,
                    observed_at,
                    (ap, ap_total, distance, yellow, source),
                ),
                snapshot=snapshot,
            )

        return self._run(instance, operation)

    def record_currency_snapshot(
        self, instance: str, currency_code: str, amount: int, *, source: str
    ) -> bool:
        observed_at = self._observation_instant()

        def operation(uow: RuntimeStorageUnitOfWork, identity_id: UUID) -> bool:
            previous = uow.runtime.last_currency_amount(identity_id, currency_code)
            if previous == int(amount):
                return False
            return uow.runtime.append_currency_snapshot(
                identity_id,
                idempotency_key=self._key(
                    "currency",
                    instance,
                    observed_at,
                    (currency_code, amount, source),
                ),
                snapshot=CurrencySnapshot(observed_at, currency_code, int(amount), source),
            )

        return self._run(instance, operation)

    def record_coins_snapshot(
        self,
        instance: str,
        yellow_coins: int,
        *,
        purple_coins: int | None,
        source: str,
    ) -> bool:
        observed_at = self._observation_instant()

        def operation(uow: RuntimeStorageUnitOfWork, identity_id: UUID) -> bool:
            values = (("yellow_coin", int(yellow_coins)),)
            if purple_coins is not None:
                values += (("purple_coin", int(purple_coins)),)
            if all(
                uow.runtime.last_currency_amount(identity_id, currency_code) == amount
                for currency_code, amount in values
            ):
                return False
            changed = False
            for currency_code, amount in values:
                changed = uow.runtime.append_currency_snapshot(
                    identity_id,
                    idempotency_key=self._key(
                        "currency",
                        instance,
                        observed_at,
                        (currency_code, amount, source),
                    ),
                    snapshot=CurrencySnapshot(observed_at, currency_code, amount, source),
                ) or changed
            return changed

        return self._run(instance, operation)

    def record_meow_battle(self, instance: str, hazard_level: int) -> None:
        if hazard_level not in {2, 3, 4, 5, 6}:
            raise StorageConfigurationError("Уровень коррозии Meow некорректен.")
        battles_per_round = 2 if hazard_level in {2, 3} else 3
        effective = Decimal(1) / Decimal(battles_per_round)

        def operation(uow: RuntimeStorageUnitOfWork, identity_id: UUID) -> None:
            uow.statistics.increment_monthly_counter(
                identity_id, self._month(), MonthlyMetric.MEOW_BATTLE_RAW_COUNT, Decimal(1)
            )
            uow.statistics.increment_monthly_counter(
                identity_id, self._month(), MonthlyMetric.MEOW_BATTLE_COUNT, effective
            )
            uow.runtime.record_meow_battle(
                identity_id, self._month(), hazard_level, effective
            )

        self._run(instance, operation)

    def record_meow_timing(
        self, instance: str, sample_kind: str, duration_seconds: Decimal, hazard_level: int | None
    ) -> bool:
        observed_at = self._observation_instant()
        sample = MeowTimingSample(observed_at, sample_kind, duration_seconds, hazard_level)
        return self._run(
            instance,
            lambda uow, identity_id: uow.runtime.append_meow_timing(
                identity_id,
                self._month(observed_at),
                idempotency_key=self._key(
                    "meow-timing",
                    instance,
                    observed_at,
                    (sample_kind, duration_seconds, hazard_level),
                ),
                sample=sample,
            ),
        )

    def record_siren_research_device(
        self, instance: str, *, source: str, hazard_level: int | None
    ) -> bool:
        observed_at = self._observation_instant()
        return self._run(
            instance,
            lambda uow, identity_id: uow.runtime.record_siren_research_device(
                identity_id,
                self._month(observed_at),
                idempotency_key=self._key(
                    "siren-device",
                    instance,
                    observed_at,
                    (source, hazard_level),
                ),
                observed_at=observed_at,
                source=source,
                hazard_level=hazard_level,
            ),
        )

    def get_ap_notification(self, instance: str) -> ApNotification | None:
        return self._run(instance, lambda uow, identity_id: uow.runtime.get_ap_notification(identity_id))

    def set_ap_notification(self, instance: str, value: int) -> ApNotification:
        observed_at = self._observation_instant()
        return self._run(
            instance,
            lambda uow, identity_id: uow.runtime.set_ap_notification(
                identity_id, value=int(value), notified_at=observed_at
            ),
        )

    def record_commission_income(
        self, instance: str, items: dict[str, int], commission_count: int = 1
    ) -> bool:
        observed_at = self._observation_instant()

        def operation(uow: RuntimeStorageUnitOfWork, identity_id: UUID) -> bool:
            income = CommissionIncome(
                id=uuid4(),
                instance_id=identity_id,
                idempotency_key=self._key(
                    "commission",
                    instance,
                    observed_at,
                    (commission_count, sorted(items.items())),
                ),
                observed_at=observed_at,
                commission_count=int(commission_count),
                source="runtime",
                items=tuple(
                    CommissionItem(name, int(value))
                    for name, value in sorted(items.items())
                    if int(value) > 0
                ),
            )
            return uow.statistics.record_commission_income(income)

        return self._run(instance, operation)

    def commission_entries(
        self, instance: str, *, start: datetime, end: datetime, limit: int = 5000
    ) -> tuple[CommissionEntry, ...]:
        return self._run(
            instance,
            lambda uow, identity_id: uow.runtime.commission_entries(
                identity_id, start=start, end=end, limit=limit
            ),
        )

    def commission_entries_for_month(
        self, instance: str, year: int, month: int, *, limit: int = 5000
    ) -> tuple[CommissionEntry, ...]:
        """Вернуть комиссионные записи за локальный календарный месяц."""

        start, end = self._month_range(year, month)
        return self.commission_entries(instance, start=start, end=end, limit=limit)

    def to_runtime_timezone(self, value: datetime) -> datetime:
        """Преобразовать aware-время в настроенный runtime-часовой пояс."""

        if value.tzinfo is None:
            raise StorageConfigurationError("Время без часового пояса недопустимо.")
        return value.astimezone(self._timezone)

    def record_resource_snapshot(self, instance: str, resources: dict[str, int | None]) -> bool:
        observed_at = self._observation_instant()
        snapshot = ResourceSnapshot(
            id=uuid4(),
            instance_id=UUID(int=0),
            idempotency_key=self._key(
                "resource",
                instance,
                observed_at,
                resources,
            ),
            observed_at=observed_at,
            source="dashboard",
            **resources,
        )

        def operation(uow: RuntimeStorageUnitOfWork, identity_id: UUID) -> bool:
            return uow.statistics.append_resource_snapshot(
                replace(snapshot, instance_id=identity_id)
            )

        return self._run(instance, operation)

    def resource_timeline(self, instance: str, *, limit: int = 500) -> tuple[ResourceSnapshot, ...]:
        return self._run(
            instance,
            lambda uow, identity_id: uow.statistics.resource_timeline(identity_id, limit=limit),
        )

    def record_opsi_items(self, instance: str, rows: tuple[dict[str, object], ...]) -> int:
        observed_at = self._observation_instant()

        def operation(uow: RuntimeStorageUnitOfWork, identity_id: UUID) -> int:
            inserted = 0
            for index, row in enumerate(rows):
                event = OpsiItemEvent(
                    id=uuid4(),
                    instance_id=identity_id,
                    idempotency_key=self._key(
                        "opsi",
                        instance,
                        observed_at,
                        (index, row),
                    ),
                    observed_at=observed_at,
                    imgid=str(row["imgid"]),
                    genre=str(row["genre"]),
                    item_code=str(row["item"]),
                    amount=int(row["amount"]),
                    server=None if row.get("server") is None else str(row["server"]),
                    zone=None if row.get("zone") is None else str(row["zone"]),
                    zone_type=None if row.get("zone_type") is None else str(row["zone_type"]),
                    zone_id=None if row.get("zone_id") is None else int(row["zone_id"]),
                    hazard_level=None if row.get("hazard_level") is None else int(row["hazard_level"]),
                    tag=None if row.get("tag") is None else str(row["tag"]),
                    combat_count=None if row.get("combat_count") is None else int(row["combat_count"]),
                )
                inserted += int(uow.statistics.append_opsi_item_event(event))
            return inserted

        return self._run(instance, operation)

    def opsi_items(
        self, instance: str, *, genre: str, limit: int = 10000
    ) -> tuple[OpsiItemProjection, ...]:
        return self._run(
            instance,
            lambda uow, identity_id: uow.runtime.opsi_items(
                identity_id, genre=genre, limit=limit
            ),
        )


_provider_lock = Lock()
_provider: Callable[[], RuntimeStorageService] | None = None


def install_runtime_storage_provider(
    provider: Callable[[], RuntimeStorageService],
) -> None:
    global _provider
    with _provider_lock:
        _provider = provider


def clear_runtime_storage_provider() -> None:
    global _provider
    with _provider_lock:
        _provider = None


def get_runtime_storage() -> RuntimeStorageService:
    provider = _provider
    if provider is None:
        raise StorageConfigurationError(
            "Production-хранилище PostgreSQL не настроено."
        )
    return provider()


__all__ = [
    "ApNotification",
    "ApPurchase",
    "ApSnapshot",
    "CommissionEntry",
    "CurrencySnapshot",
    "MeowHazardSummary",
    "MeowTimingSample",
    "MonthlyStatistics",
    "OpsiItemProjection",
    "RuntimeStatisticsRepository",
    "RuntimeStorageService",
    "RuntimeStorageUnitOfWork",
    "clear_runtime_storage_provider",
    "get_runtime_storage",
    "install_runtime_storage_provider",
]
