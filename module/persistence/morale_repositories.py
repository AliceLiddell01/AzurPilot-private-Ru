"""PostgreSQL adapter append-only Per-ship Morale observations."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import Connection, func, insert, select
from sqlalchemy.exc import SQLAlchemyError

from module.application.canonical_payload import payload_digest
from module.application.errors import StorageConflictError, StorageInvalidDataError
from module.application.morale import (
    MoraleKnowledge,
    MoraleObservation,
    MoraleRecoveryProfile,
)
from module.dock_inventory.model import CanonicalShipIdentity, ShipForm
from module.formation.model import FleetSelection, FormationFleetSide
from module.persistence.database import translate_database_error
from module.persistence.schema import formation_surface_fleet_morale_observation


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
    }


class PostgresMoraleRepository:
    def __init__(self, connection: Connection):
        self._connection = connection

    def append(self, observation: MoraleObservation) -> MoraleObservation:
        if not isinstance(observation, MoraleObservation):
            raise StorageInvalidDataError("Morale observation имеет неверный тип.")
        digest = payload_digest(_payload(observation))
        table = formation_surface_fleet_morale_observation
        try:
            existing = self._connection.execute(
                select(table).where(
                    table.c.idempotency_key == observation.idempotency_key
                )
            ).mappings().one_or_none()
            if existing is not None:
                if existing["payload_digest"] == digest:
                    return self._hydrate(existing)
                raise StorageConflictError(
                    "Morale idempotency key содержит другой payload."
                )
            self._connection.execute(
                insert(table).values(
                    id=observation.id,
                    formation_snapshot_id=observation.formation_snapshot_id,
                    instance_id=observation.instance_id,
                    idempotency_key=observation.idempotency_key,
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
        if not isinstance(instance_id, UUID) or not isinstance(selection, FleetSelection):
            raise StorageInvalidDataError("Morale latest request некорректен.")
        table = formation_surface_fleet_morale_observation
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
            .where(
                table.c.instance_id == instance_id,
                table.c.fleet_index.in_(selection.fleet_indices),
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
        )


__all__ = ["PostgresMoraleRepository"]
