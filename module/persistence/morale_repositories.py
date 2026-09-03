"""PostgreSQL-адаптер добавления наблюдений Morale по отдельным кораблям."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from uuid import UUID

from sqlalchemy import Connection, and_, func, insert, or_, select
from sqlalchemy.exc import SQLAlchemyError

from module.application.canonical_payload import payload_digest
from module.application.errors import StorageConflictError, StorageInvalidDataError
from module.application.morale import (
    MoraleKnowledge,
    MoraleLocation,
    MoraleObservation,
    MoraleRecoveryProfile,
)
from module.dock_inventory.model import CanonicalShipIdentity, ShipForm
from module.formation.model import FleetSelection, FormationFleetSide
from module.persistence.database import translate_database_error
from module.persistence.schema import (
    formation_surface_fleet_morale_observation,
    formation_surface_fleet_slot,
    formation_surface_fleet_snapshot,
)


def _payload(observation: MoraleObservation) -> dict[str, object]:
    return {
        "formation_snapshot_id": observation.formation_snapshot_id,
        "instance_id": observation.instance_id,
        "fleet_index": observation.fleet_index,
        "side": observation.side,
        "position": observation.position,
        "canonical_identity_key": observation.canonical_identity.key,
        "ship_form": observation.ship_form,
        "baseline": observation.baseline,
        "observed_at": observation.observed_at,
        "recovery_per_hour": observation.recovery.recovery_per_hour,
        "recovery_ceiling": observation.recovery.recovery_ceiling,
        "source": observation.source,
        "recovery_source": observation.recovery.source,
        "knowledge": observation.knowledge,
        "location": observation.location,
        "dorm_scan_id": observation.dorm_scan_id,
    }


def _storage_idempotency_for(instance_id: UUID, key: str) -> str:
    """Получить ключ хранения для ключа вызова без раскрытия исходного ключа в БД."""

    return payload_digest(
        {
            "instance_id": instance_id,
            "idempotency_key": key,
        }
    )


def _storage_idempotency_key(observation: MoraleObservation) -> str:
    """Изолировать idempotency вызова внутри app instance без изменения поля БД."""

    return _storage_idempotency_for(
        observation.instance_id,
        observation.idempotency_key,
    )


class PostgresMoraleRepository:
    def __init__(self, connection: Connection):
        self._connection = connection

    def append(self, observation: MoraleObservation) -> MoraleObservation:
        if not isinstance(observation, MoraleObservation):
            raise StorageInvalidDataError("Morale observation имеет неверный тип.")
        digest = payload_digest(_payload(observation))
        storage_idempotency_key = _storage_idempotency_key(observation)
        table = formation_surface_fleet_morale_observation
        try:
            existing = (
                self._connection.execute(
                    select(table).where(
                        table.c.idempotency_key == storage_idempotency_key
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if existing["payload_digest"] == digest:
                    try:
                        hydrated = self._hydrate(existing)
                    except (KeyError, TypeError, ValueError):
                        raise StorageInvalidDataError(
                            "PostgreSQL содержит некорректное Morale observation."
                        ) from None
                    return replace(
                        hydrated,
                        idempotency_key=observation.idempotency_key,
                    )
                raise StorageConflictError(
                    "Morale idempotency key содержит другой payload."
                )
            self._connection.execute(
                insert(table).values(
                    id=observation.id,
                    formation_snapshot_id=observation.formation_snapshot_id,
                    instance_id=observation.instance_id,
                    idempotency_key=storage_idempotency_key,
                    payload_digest=digest,
                    fleet_index=observation.fleet_index,
                    side=observation.side.value,
                    position=observation.position,
                    canonical_identity_key=observation.canonical_identity.key,
                    ship_form=observation.ship_form.value,
                    baseline=observation.baseline,
                    observed_at=observation.observed_at,
                    recovery_per_hour=observation.recovery.recovery_per_hour,
                    recovery_ceiling=observation.recovery.recovery_ceiling,
                    source=observation.source,
                    recovery_source=observation.recovery.source,
                    knowledge=observation.knowledge.value,
                    location=observation.location.value,
                    dorm_scan_id=observation.dorm_scan_id,
                )
            )
            return observation
        except StorageConflictError:
            raise
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def latest(
        self,
        instance_id: UUID,
        selection: FleetSelection,
    ) -> tuple[MoraleObservation, ...]:
        if not isinstance(instance_id, UUID) or not isinstance(
            selection, FleetSelection
        ):
            raise StorageInvalidDataError("Morale latest request некорректен.")
        table = formation_surface_fleet_morale_observation
        anchor_snapshot = formation_surface_fleet_snapshot.alias(
            "morale_anchor_snapshot"
        )
        later_snapshot = formation_surface_fleet_snapshot.alias("morale_later_snapshot")
        later_slot = formation_surface_fleet_slot.alias("morale_later_slot")
        continuity_break = (
            select(later_snapshot.c.id)
            .select_from(
                later_snapshot.join(
                    later_slot,
                    later_slot.c.snapshot_id == later_snapshot.c.id,
                )
            )
            .where(
                later_snapshot.c.instance_id == table.c.instance_id,
                later_snapshot.c.fleet_index == table.c.fleet_index,
                or_(
                    later_snapshot.c.observed_at > anchor_snapshot.c.observed_at,
                    and_(
                        later_snapshot.c.observed_at == anchor_snapshot.c.observed_at,
                        later_snapshot.c.id > anchor_snapshot.c.id,
                    ),
                ),
                later_slot.c.side == table.c.side,
                later_slot.c.position == table.c.position,
                or_(
                    later_slot.c.occupied.is_(False),
                    later_slot.c.identity_status.is_distinct_from("matched"),
                    later_slot.c.canonical_identity_key.is_distinct_from(
                        table.c.canonical_identity_key
                    ),
                    later_slot.c.ship_form.is_distinct_from(table.c.ship_form),
                ),
            )
            .exists()
        )
        ranked = (
            select(
                table.c.id,
                func.row_number()
                .over(
                    partition_by=(table.c.fleet_index, table.c.side, table.c.position),
                    order_by=(table.c.observed_at.desc(), table.c.id.desc()),
                )
                .label("rank"),
            )
            .select_from(
                table.join(
                    anchor_snapshot,
                    anchor_snapshot.c.id == table.c.formation_snapshot_id,
                )
            )
            .where(
                table.c.instance_id == instance_id,
                table.c.fleet_index.in_(selection.fleet_indices),
                ~continuity_break,
            )
            .subquery()
        )
        statement = (
            select(table)
            .join(ranked, ranked.c.id == table.c.id)
            .where(ranked.c.rank == 1)
            .order_by(table.c.fleet_index, table.c.side, table.c.position)
        )
        try:
            rows = tuple(self._connection.execute(statement).mappings().all())
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None
        try:
            return tuple(self._hydrate(row) for row in rows)
        except (KeyError, TypeError, ValueError):
            raise StorageInvalidDataError(
                "PostgreSQL содержит некорректное Morale observation."
            ) from None

    def contains_idempotency(
        self,
        instance_id: UUID,
        keys: tuple[str, ...],
    ) -> frozenset[str]:
        """Проверить ключи отдельных слотов одним set-based запросом."""

        if not isinstance(instance_id, UUID) or not isinstance(keys, tuple):
            raise StorageInvalidDataError("Morale idempotency request некорректен.")
        if not keys:
            return frozenset()
        if any(
            not isinstance(key, str) or not key.strip() or len(key) > 128
            for key in keys
        ):
            raise StorageInvalidDataError(
                "Morale idempotency request содержит некорректный key."
            )
        storage_keys = {
            _storage_idempotency_for(instance_id, key): key for key in keys
        }
        table = formation_surface_fleet_morale_observation
        try:
            rows = self._connection.execute(
                select(table.c.idempotency_key).where(
                    table.c.idempotency_key.in_(tuple(storage_keys))
                )
            ).scalars()
            return frozenset(storage_keys[value] for value in rows)
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    @staticmethod
    def _hydrate(row: Mapping[str, object]) -> MoraleObservation:
        return MoraleObservation(
            id=row["id"],
            formation_snapshot_id=row["formation_snapshot_id"],
            instance_id=row["instance_id"],
            fleet_index=row["fleet_index"],
            side=FormationFleetSide(row["side"]),
            position=row["position"],
            canonical_identity=CanonicalShipIdentity(row["canonical_identity_key"]),
            ship_form=ShipForm(row["ship_form"]),
            baseline=row["baseline"],
            observed_at=row["observed_at"],
            recovery=MoraleRecoveryProfile(
                recovery_per_hour=row["recovery_per_hour"],
                recovery_ceiling=row["recovery_ceiling"],
                source=row["recovery_source"],
            ),
            source=row["source"],
            idempotency_key=row["idempotency_key"],
            knowledge=MoraleKnowledge(row["knowledge"]),
            location=MoraleLocation(row["location"]),
            dorm_scan_id=row["dorm_scan_id"],
        )


__all__ = ["PostgresMoraleRepository"]
