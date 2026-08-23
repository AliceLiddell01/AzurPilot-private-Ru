"""PostgreSQL-репозитории production runtime."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import Connection, Table, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import NoResultFound, SQLAlchemyError

from module.application.errors import StorageConflictError, StorageInvalidDataError
from module.application.runtime_storage import (
    ApNotification,
    ApPurchase,
    ApSnapshot,
    CommissionEntry,
    CurrencySnapshot,
    MeowHazardSummary,
    MeowTimingSample,
    MonthlyStatistics,
    OpsiItemProjection,
)
from module.application.storage_models import (
    CommissionItem,
    MonthlyAggregate,
    MonthlyMetric,
)
from module.persistence.database import translate_database_error
from module.persistence.schema import (
    ap_notification_state,
    cl1_ap_purchase_event,
    cl1_ap_snapshot,
    cl1_currency_snapshot,
    commission_income_event,
    commission_income_item,
    meow_hazard_aggregate,
    meow_timing_sample,
    monthly_aggregate,
    opsi_item_event,
    siren_research_device_event,
    siren_research_device_stat,
)

_LOGGER = logging.getLogger(__name__)


def _bounded(value: str, *, label: str, maximum: int) -> str:
    if not value or len(value) > maximum:
        raise StorageInvalidDataError(f"Поле {label} некорректно.")
    return value


def _normalized(value: object) -> object:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value.normalize())
    if isinstance(value, dict):
        return {key: _normalized(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_normalized(item) for item in value]
    return value


def _digest(value: object) -> str:
    encoded = json.dumps(
        _normalized(value), sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class PostgresRuntimeStatisticsRepository:
    def __init__(self, connection: Connection):
        self._connection = connection

    def monthly_statistics(
        self,
        instance_id: UUID,
        month: date,
        *,
        start: datetime,
        end: datetime,
    ) -> MonthlyStatistics:
        if month.day != 1:
            raise StorageInvalidDataError("Месяц должен указывать на первый день месяца.")
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise StorageInvalidDataError("Границы календарного месяца некорректны.")
        try:
            metric_rows = self._connection.execute(
                select(
                    monthly_aggregate.c.instance_id,
                    monthly_aggregate.c.month,
                    monthly_aggregate.c.metric,
                    monthly_aggregate.c.value,
                    monthly_aggregate.c.version,
                )
                .where(
                    monthly_aggregate.c.instance_id == instance_id,
                    monthly_aggregate.c.month == month,
                )
                .order_by(monthly_aggregate.c.metric)
            ).all()
            ap_rows = self._connection.execute(
                select(
                    cl1_ap_snapshot.c.observed_at,
                    cl1_ap_snapshot.c.ap,
                    cl1_ap_snapshot.c.ap_total,
                    cl1_ap_snapshot.c.asset,
                    cl1_ap_snapshot.c.yellow_coin,
                    cl1_ap_snapshot.c.distance,
                    cl1_ap_snapshot.c.source,
                )
                .where(
                    cl1_ap_snapshot.c.instance_id == instance_id,
                    cl1_ap_snapshot.c.observed_at >= start,
                    cl1_ap_snapshot.c.observed_at < end,
                )
                .order_by(cl1_ap_snapshot.c.observed_at, cl1_ap_snapshot.c.id)
                .limit(1000)
            ).all()
            purchase_rows = self._connection.execute(
                select(
                    cl1_ap_purchase_event.c.observed_at,
                    cl1_ap_purchase_event.c.amount,
                    cl1_ap_purchase_event.c.base_amount,
                    cl1_ap_purchase_event.c.purchase_count,
                    cl1_ap_purchase_event.c.source,
                )
                .where(
                    cl1_ap_purchase_event.c.instance_id == instance_id,
                    cl1_ap_purchase_event.c.observed_at >= start,
                    cl1_ap_purchase_event.c.observed_at < end,
                )
                .order_by(
                    cl1_ap_purchase_event.c.observed_at,
                    cl1_ap_purchase_event.c.id,
                )
                .limit(1000)
            ).all()
            currency_rows = self._connection.execute(
                select(
                    cl1_currency_snapshot.c.observed_at,
                    cl1_currency_snapshot.c.currency_code,
                    cl1_currency_snapshot.c.amount,
                    cl1_currency_snapshot.c.source,
                )
                .where(
                    cl1_currency_snapshot.c.instance_id == instance_id,
                    cl1_currency_snapshot.c.observed_at >= start,
                    cl1_currency_snapshot.c.observed_at < end,
                )
                .order_by(cl1_currency_snapshot.c.observed_at, cl1_currency_snapshot.c.id)
                .limit(2000)
            ).all()
            timing_rows = self._connection.execute(
                select(
                    meow_timing_sample.c.observed_at,
                    meow_timing_sample.c.sample_kind,
                    meow_timing_sample.c.duration_seconds,
                    meow_timing_sample.c.hazard_level,
                )
                .where(
                    meow_timing_sample.c.instance_id == instance_id,
                    meow_timing_sample.c.month == month,
                )
                .order_by(meow_timing_sample.c.observed_at, meow_timing_sample.c.id)
                .limit(1000)
            ).all()
            for row_count, limit, dataset in (
                (len(ap_rows), 1000, "снимки очков действия"),
                (len(purchase_rows), 1000, "покупки очков действия"),
                (len(currency_rows), 2000, "снимки валют"),
                (len(timing_rows), 1000, "замеры длительности Meow"),
            ):
                if row_count == limit:
                    _LOGGER.warning(
                        f"[PostgreSQL] Набор «{dataset}» достиг лимита {limit}; "
                        "результат статистики может быть усечён"
                    )
            hazard_rows = self._connection.execute(
                select(
                    meow_hazard_aggregate.c.hazard_level,
                    meow_hazard_aggregate.c.raw_battle_count,
                    meow_hazard_aggregate.c.effective_rounds,
                )
                .where(
                    meow_hazard_aggregate.c.instance_id == instance_id,
                    meow_hazard_aggregate.c.month == month,
                )
                .order_by(meow_hazard_aggregate.c.hazard_level)
            ).all()
            siren_rows = self._connection.execute(
                select(
                    siren_research_device_stat.c.source,
                    siren_research_device_stat.c.hazard_level,
                    siren_research_device_stat.c.device_count,
                ).where(
                    siren_research_device_stat.c.instance_id == instance_id,
                    siren_research_device_stat.c.month == month,
                )
            ).all()
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

        round_by_hazard: dict[int, list[Decimal]] = {}
        battle_by_hazard: dict[int, list[Decimal]] = {}
        for row in timing_rows:
            if row.hazard_level is None:
                continue
            target = round_by_hazard if row.sample_kind == "round" else battle_by_hazard
            target.setdefault(row.hazard_level, []).append(row.duration_seconds)
        siren_meow = {
            row.hazard_level: int(row.device_count)
            for row in siren_rows
            if row.source == "meow"
        }
        hazard_levels = sorted(
            {row.hazard_level for row in hazard_rows}
            | {level for level in siren_meow if level is not None}
        )
        hazard_by_level = {row.hazard_level: row for row in hazard_rows}
        hazards = tuple(
            MeowHazardSummary(
                hazard_level=level,
                battle_count=int(hazard_by_level[level].raw_battle_count)
                if level in hazard_by_level
                else 0,
                effective_rounds=hazard_by_level[level].effective_rounds
                if level in hazard_by_level
                else Decimal(0),
                round_times=tuple(round_by_hazard.get(level, ())),
                battle_times=tuple(battle_by_hazard.get(level, ())),
                siren_research_devices=siren_meow.get(level, 0),
            )
            for level in hazard_levels
        )
        return MonthlyStatistics(
            month=month,
            metrics=tuple(
                MonthlyAggregate(
                    row.instance_id,
                    row.month,
                    MonthlyMetric(row.metric),
                    row.value,
                    row.version,
                )
                for row in metric_rows
            ),
            ap_snapshots=tuple(ApSnapshot(*row) for row in ap_rows),
            ap_purchases=tuple(ApPurchase(*row) for row in purchase_rows),
            currency_snapshots=tuple(CurrencySnapshot(*row) for row in currency_rows),
            meow_timings=tuple(MeowTimingSample(*row) for row in timing_rows),
            meow_hazards=hazards,
            siren_cl1_devices=sum(
                int(row.device_count) for row in siren_rows if row.source == "cl1"
            ),
        )

    def append_ap_snapshot(
        self, instance_id: UUID, *, idempotency_key: str, snapshot: ApSnapshot
    ) -> bool:
        values = {
            "id": uuid4(),
            "instance_id": instance_id,
            "idempotency_key": _bounded(idempotency_key, label="idempotency_key", maximum=128),
            **asdict(snapshot),
        }
        return self._append(cl1_ap_snapshot, values)

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
    ) -> bool:
        if min(amount, base_amount, purchase_count) < 0:
            raise StorageInvalidDataError("Поля покупки AP не могут быть отрицательными.")
        values = {
            "id": uuid4(),
            "instance_id": instance_id,
            "idempotency_key": _bounded(idempotency_key, label="idempotency_key", maximum=128),
            "observed_at": observed_at,
            "legacy_timestamp_text": None,
            "legacy_timezone": None,
            "amount": amount,
            "base_amount": base_amount,
            "purchase_count": purchase_count,
            "source": _bounded(source, label="source", maximum=64),
        }
        return self._append(cl1_ap_purchase_event, values)

    def append_currency_snapshot(
        self,
        instance_id: UUID,
        *,
        idempotency_key: str,
        snapshot: CurrencySnapshot,
    ) -> bool:
        if snapshot.amount < 0:
            raise StorageInvalidDataError("Сумма валюты не может быть отрицательной.")
        _bounded(snapshot.currency_code, label="currency_code", maximum=32)
        values = {
            "id": uuid4(),
            "instance_id": instance_id,
            "idempotency_key": _bounded(idempotency_key, label="idempotency_key", maximum=128),
            **asdict(snapshot),
            "legacy_timestamp_text": None,
            "legacy_timezone": None,
        }
        return self._append(cl1_currency_snapshot, values)

    def last_currency_amount(self, instance_id: UUID, currency_code: str) -> int | None:
        _bounded(currency_code, label="currency_code", maximum=32)
        try:
            value = self._connection.execute(
                select(cl1_currency_snapshot.c.amount)
                .where(
                    cl1_currency_snapshot.c.instance_id == instance_id,
                    cl1_currency_snapshot.c.currency_code == currency_code,
                )
                .order_by(
                    cl1_currency_snapshot.c.observed_at.desc().nulls_last(),
                    cl1_currency_snapshot.c.id.desc(),
                )
                .limit(1)
            ).scalar_one_or_none()
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None
        return None if value is None else int(value)

    def record_meow_battle(
        self, instance_id: UUID, month: date, hazard_level: int, effective_delta: Decimal
    ) -> None:
        if hazard_level not in {2, 3, 4, 5, 6} or effective_delta <= 0:
            raise StorageInvalidDataError("Команда записи боя Meow некорректна.")
        statement = insert(meow_hazard_aggregate).values(
            instance_id=instance_id,
            month=month,
            hazard_level=hazard_level,
            raw_battle_count=1,
            effective_rounds=effective_delta,
            source="runtime",
        )
        statement = statement.on_conflict_do_update(
            index_elements=["instance_id", "month", "hazard_level"],
            set_={
                "raw_battle_count": meow_hazard_aggregate.c.raw_battle_count + 1,
                "effective_rounds": meow_hazard_aggregate.c.effective_rounds
                + effective_delta,
                "source": "runtime",
            },
        )
        try:
            self._connection.execute(statement)
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def append_meow_timing(
        self,
        instance_id: UUID,
        month: date,
        *,
        idempotency_key: str,
        sample: MeowTimingSample,
    ) -> bool:
        if sample.sample_kind not in {"battle", "round"} or sample.duration_seconds < 0:
            raise StorageInvalidDataError("Замер времени Meow некорректен.")
        if sample.hazard_level is not None and sample.hazard_level not in {2, 3, 4, 5, 6}:
            raise StorageInvalidDataError("Уровень коррозии замера Meow некорректен.")
        values = {
            "id": uuid4(),
            "instance_id": instance_id,
            "idempotency_key": _bounded(idempotency_key, label="idempotency_key", maximum=128),
            "month": month,
            **asdict(sample),
            "source": "runtime",
            "legacy_timestamp_text": None,
            "legacy_timezone": None,
        }
        return self._append(meow_timing_sample, values)

    def record_siren_research_device(
        self,
        instance_id: UUID,
        month: date,
        *,
        idempotency_key: str,
        observed_at: datetime,
        source: str,
        hazard_level: int | None,
    ) -> bool:
        if source == "cl1":
            if hazard_level is not None:
                raise StorageInvalidDataError("Событие Сирен CL1 не содержит уровень коррозии.")
            stat_hazard = 0
        elif source == "meow" and hazard_level in {2, 3, 4, 5, 6}:
            stat_hazard = int(hazard_level)
        else:
            raise StorageInvalidDataError("Источник или уровень коррозии события Сирен некорректны.")
        values = {
            "id": uuid4(),
            "instance_id": instance_id,
            "idempotency_key": _bounded(idempotency_key, label="idempotency_key", maximum=128),
            "observed_at": observed_at,
            "legacy_timestamp_text": None,
            "legacy_timezone": None,
            "source": source,
            "hazard_level": hazard_level,
        }
        inserted = self._append(siren_research_device_event, values)
        if not inserted:
            return False
        statement = insert(siren_research_device_stat).values(
            instance_id=instance_id,
            month=month,
            source=source,
            hazard_level=stat_hazard,
            device_count=1,
        )
        statement = statement.on_conflict_do_update(
            index_elements=["instance_id", "month", "source", "hazard_level"],
            set_={"device_count": siren_research_device_stat.c.device_count + 1},
        )
        try:
            self._connection.execute(statement)
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None
        return True

    def get_ap_notification(self, instance_id: UUID) -> ApNotification | None:
        try:
            row = self._connection.execute(
                select(
                    ap_notification_state.c.last_ap,
                    ap_notification_state.c.notified_at,
                    ap_notification_state.c.version,
                ).where(ap_notification_state.c.instance_id == instance_id)
            ).one_or_none()
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None
        return None if row is None else ApNotification(*row)

    def set_ap_notification(
        self, instance_id: UUID, *, value: int, notified_at: datetime
    ) -> ApNotification:
        if value < 0:
            raise StorageInvalidDataError("Значение уведомления об очках действия некорректно.")
        statement = insert(ap_notification_state).values(
            instance_id=instance_id,
            last_ap=value,
            notified_at=notified_at,
            legacy_timestamp_text=None,
            legacy_timezone=None,
            version=1,
        )
        statement = statement.on_conflict_do_update(
            index_elements=["instance_id"],
            set_={
                "last_ap": value,
                "notified_at": notified_at,
                "legacy_timestamp_text": None,
                "legacy_timezone": None,
                "version": ap_notification_state.c.version + 1,
            },
        ).returning(
            ap_notification_state.c.last_ap,
            ap_notification_state.c.notified_at,
            ap_notification_state.c.version,
        )
        try:
            row = self._connection.execute(statement).one()
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None
        return ApNotification(*row)

    def commission_entries(
        self, instance_id: UUID, *, start: datetime, end: datetime, limit: int
    ) -> tuple[CommissionEntry, ...]:
        if limit < 1 or limit > 5000 or start >= end:
            raise StorageInvalidDataError("Границы запроса комиссий некорректны.")
        try:
            event_ids = (
                select(commission_income_event.c.id)
                .where(
                    commission_income_event.c.instance_id == instance_id,
                    commission_income_event.c.observed_at >= start,
                    commission_income_event.c.observed_at < end,
                )
                .order_by(
                    commission_income_event.c.observed_at.desc(),
                    commission_income_event.c.id.desc(),
                )
                .limit(limit)
                .subquery()
            )
            rows = self._connection.execute(
                select(
                    commission_income_event.c.id,
                    commission_income_event.c.observed_at,
                    commission_income_event.c.commission_count,
                    commission_income_item.c.item_code,
                    commission_income_item.c.amount,
                )
                .select_from(
                    commission_income_event.outerjoin(
                        commission_income_item,
                        commission_income_event.c.id == commission_income_item.c.event_id,
                    )
                )
                .where(
                    commission_income_event.c.id.in_(select(event_ids.c.id)),
                )
                .order_by(
                    commission_income_event.c.observed_at,
                    commission_income_event.c.id,
                    commission_income_item.c.item_code,
                )
            ).all()
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None
        grouped: dict[UUID, tuple[datetime | None, int, list[CommissionItem]]] = {}
        for row in rows:
            if row.id not in grouped:
                grouped[row.id] = (row.observed_at, row.commission_count, [])
            if row.item_code is not None and row.amount is not None:
                grouped[row.id][2].append(
                    CommissionItem(row.item_code, int(row.amount))
                )
        entries = tuple(
            CommissionEntry(observed_at, count, tuple(items))
            for observed_at, count, items in grouped.values()
        )
        return entries

    def opsi_items(
        self, instance_id: UUID, *, genre: str, limit: int
    ) -> tuple[OpsiItemProjection, ...]:
        _bounded(genre, label="genre", maximum=64)
        if limit < 1 or limit > 10000:
            raise StorageInvalidDataError("Предел запроса Operation Siren некорректен.")
        try:
            rows = self._connection.execute(
                select(
                    opsi_item_event.c.observed_at,
                    opsi_item_event.c.imgid,
                    opsi_item_event.c.genre,
                    opsi_item_event.c.item_code,
                    opsi_item_event.c.amount,
                    opsi_item_event.c.server,
                    opsi_item_event.c.zone,
                    opsi_item_event.c.zone_type,
                    opsi_item_event.c.zone_id,
                    opsi_item_event.c.hazard_level,
                    opsi_item_event.c.tag,
                    opsi_item_event.c.combat_count,
                )
                .where(
                    opsi_item_event.c.instance_id == instance_id,
                    opsi_item_event.c.genre == genre,
                )
                .order_by(opsi_item_event.c.observed_at, opsi_item_event.c.id)
                .limit(limit)
            ).all()
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None
        return tuple(OpsiItemProjection(*row) for row in rows)

    def _append(self, table: Table, values: dict[str, object]) -> bool:
        payload = {key: value for key, value in values.items() if key != "id"}
        digest = _digest(payload)
        values["payload_digest"] = digest
        key = str(values["idempotency_key"])
        try:
            inserted = self._connection.execute(
                insert(table)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
                .returning(table.c.id)
            ).scalar_one_or_none()
            if inserted is not None:
                return True
            existing = self._connection.execute(
                select(table.c.payload_digest).where(table.c.idempotency_key == key)
            ).scalar_one()
        except NoResultFound:
            raise StorageConflictError(
                "Idempotency key обрабатывается конкурирующей транзакцией."
            ) from None
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None
        if existing == digest:
            return False
        raise StorageConflictError("Idempotency key уже связан с другими данными.")


__all__ = ["PostgresRuntimeStatisticsRepository"]
