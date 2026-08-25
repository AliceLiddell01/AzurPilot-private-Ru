"""PostgreSQL adapters append-only истории Formation Surface Fleet."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import Connection, func, insert, select, update
from sqlalchemy.exc import SQLAlchemyError

from module.application.canonical_payload import payload_digest
from module.application.errors import StorageConflictError, StorageInvalidDataError
from module.application.fleet_state import (
    FleetScanAttempt,
    FleetScanRun,
    FleetScanRunStatus,
    FleetStateObservation,
)
from module.dock_inventory.model import CanonicalShipIdentity, IdentityStatus, ShipForm
from module.formation.model import (
    FleetSelection,
    FormationFleetSide,
    FormationFleetSlotObservation,
    FormationFleetSnapshot,
    validate_surface_fleet_index,
)
from module.persistence.database import translate_database_error
from module.persistence.schema import (
    formation_surface_fleet_scan_request,
    formation_surface_fleet_scan_run,
    formation_surface_fleet_slot,
    formation_surface_fleet_snapshot,
)


def _bounded_optional(value: str | None, *, field: str, maximum: int) -> str | None:
    if value is not None and (not isinstance(value, str) or len(value) > maximum):
        raise StorageInvalidDataError(f"Поле {field} некорректно.")
    return value


def _snapshot_payload(observation: FleetStateObservation) -> dict[str, object]:
    snapshot = observation.snapshot
    return {
        "run_id": observation.run_id,
        "instance_id": observation.instance_id,
        "fleet_index": snapshot.fleet_index,
        "observed_at": observation.observed_at,
        "complete": snapshot.complete,
        "catalog_fingerprint": snapshot.catalog_fingerprint,
        "slots": tuple(
            {
                "side": slot.side,
                "position": slot.position,
                "occupied": slot.occupied,
                "identity_status": slot.identity_status,
                "raw_name_ocr": slot.raw_name_ocr,
                "displayed_name": slot.displayed_name,
                "canonical_identity_key": (
                    slot.canonical_identity.key
                    if slot.canonical_identity is not None
                    else None
                ),
                "canonical_name": slot.canonical_name,
                "ship_form": slot.ship_form,
            }
            for slot in snapshot.slots
        ),
    }


class PostgresFleetStateRepository:
    def __init__(self, connection: Connection):
        self._connection = connection

    def create_run(self, run: FleetScanRun) -> None:
        try:
            self._connection.execute(
                insert(formation_surface_fleet_scan_run).values(
                    id=run.id,
                    instance_id=run.instance_id,
                    source=run.source,
                    started_at=run.started_at,
                    status=FleetScanRunStatus.STARTED.value,
                )
            )
            self._connection.execute(
                insert(formation_surface_fleet_scan_request),
                [
                    {"run_id": run.id, "fleet_index": fleet_index}
                    for fleet_index in run.selection.fleet_indices
                ],
            )
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def append_observation(self, observation: FleetStateObservation) -> bool:
        payload = _snapshot_payload(observation)
        digest = payload_digest(payload)
        for slot in observation.snapshot.slots:
            _bounded_optional(slot.raw_name_ocr, field="raw_name_ocr", maximum=256)
            _bounded_optional(slot.displayed_name, field="displayed_name", maximum=256)
            _bounded_optional(slot.canonical_name, field="canonical_name", maximum=256)
            _bounded_optional(
                slot.canonical_identity.key
                if slot.canonical_identity is not None
                else None,
                field="canonical_identity_key",
                maximum=128,
            )
        try:
            existing = self._connection.execute(
                select(
                    formation_surface_fleet_snapshot.c.id,
                    formation_surface_fleet_snapshot.c.payload_digest,
                ).where(
                    formation_surface_fleet_snapshot.c.idempotency_key
                    == observation.idempotency_key
                )
            ).one_or_none()
            if existing is not None:
                if existing.payload_digest == digest:
                    return False
                raise StorageConflictError(
                    "Fleet snapshot idempotency key содержит другой payload."
                )

            requested_instance = self._connection.execute(
                select(formation_surface_fleet_scan_run.c.instance_id)
                .select_from(
                    formation_surface_fleet_scan_run.join(
                        formation_surface_fleet_scan_request,
                        formation_surface_fleet_scan_request.c.run_id
                        == formation_surface_fleet_scan_run.c.id,
                    )
                )
                .where(
                    formation_surface_fleet_scan_run.c.id == observation.run_id,
                    formation_surface_fleet_scan_request.c.fleet_index
                    == observation.fleet_index,
                )
            ).scalar_one_or_none()
            if requested_instance != observation.instance_id:
                raise StorageConflictError(
                    "Fleet observation не соответствует scan run или selection."
                )

            self._connection.execute(
                insert(formation_surface_fleet_snapshot).values(
                    id=observation.id,
                    run_id=observation.run_id,
                    instance_id=observation.instance_id,
                    idempotency_key=observation.idempotency_key,
                    payload_digest=digest,
                    fleet_index=observation.fleet_index,
                    observed_at=observation.observed_at,
                    complete=observation.snapshot.complete,
                    catalog_fingerprint=observation.snapshot.catalog_fingerprint,
                )
            )
            self._connection.execute(
                insert(formation_surface_fleet_slot),
                [
                    {
                        "snapshot_id": observation.id,
                        "side": slot.side.value,
                        "position": slot.position,
                        "occupied": slot.occupied,
                        "identity_status": (
                            slot.identity_status.value
                            if slot.identity_status is not None
                            else None
                        ),
                        "raw_name_ocr": slot.raw_name_ocr,
                        "displayed_name": slot.displayed_name,
                        "canonical_identity_key": (
                            slot.canonical_identity.key
                            if slot.canonical_identity is not None
                            else None
                        ),
                        "canonical_name": slot.canonical_name,
                        "ship_form": (
                            slot.ship_form.value
                            if slot.ship_form is not None
                            else None
                        ),
                    }
                    for slot in observation.snapshot.slots
                ],
            )
            return True
        except StorageConflictError:
            raise
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def finish_run(
        self,
        run_id: UUID,
        *,
        status: FleetScanRunStatus,
        finished_at: datetime,
        error_code: str | None,
    ) -> None:
        if status is FleetScanRunStatus.STARTED:
            raise StorageInvalidDataError("Завершённый scan run не может быть started.")
        _bounded_optional(error_code, field="error_code", maximum=64)
        if not isinstance(finished_at, datetime) or finished_at.tzinfo is None:
            raise StorageInvalidDataError("finished_at должен содержать timezone.")
        if status is FleetScanRunStatus.SUCCEEDED and error_code is not None:
            raise StorageInvalidDataError("Успешный scan run не содержит error_code.")
        if status in {FleetScanRunStatus.PARTIAL, FleetScanRunStatus.FAILED} and (
            not error_code
        ):
            raise StorageInvalidDataError("Неуспешный scan run требует error_code.")
        try:
            result = self._connection.execute(
                update(formation_surface_fleet_scan_run)
                .where(
                    formation_surface_fleet_scan_run.c.id == run_id,
                    formation_surface_fleet_scan_run.c.status
                    == FleetScanRunStatus.STARTED.value,
                )
                .values(
                    status=status.value,
                    finished_at=finished_at,
                    error_code=error_code,
                )
            )
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None
        if result.rowcount != 1:
            raise StorageConflictError("Fleet scan run transition отклонён.")

    def latest(
        self,
        instance_id: UUID,
        selection: FleetSelection,
    ) -> tuple[FleetStateObservation, ...]:
        ranked = (
            select(
                formation_surface_fleet_snapshot.c.id,
                func.row_number()
                .over(
                    partition_by=formation_surface_fleet_snapshot.c.fleet_index,
                    order_by=(
                        formation_surface_fleet_snapshot.c.observed_at.desc(),
                        formation_surface_fleet_snapshot.c.id.desc(),
                    ),
                )
                .label("rank"),
            )
            .where(
                formation_surface_fleet_snapshot.c.instance_id == instance_id,
                formation_surface_fleet_snapshot.c.fleet_index.in_(
                    selection.fleet_indices
                ),
            )
            .subquery()
        )
        statement = (
            select(formation_surface_fleet_snapshot)
            .join(ranked, ranked.c.id == formation_surface_fleet_snapshot.c.id)
            .where(ranked.c.rank == 1)
            .order_by(formation_surface_fleet_snapshot.c.fleet_index)
        )
        return self._read(statement)

    def history(
        self,
        instance_id: UUID,
        fleet_index: int,
        *,
        limit: int,
    ) -> tuple[FleetStateObservation, ...]:
        fleet_index = validate_surface_fleet_index(fleet_index)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise StorageInvalidDataError("Fleet history limit некорректен.")
        statement = (
            select(formation_surface_fleet_snapshot)
            .where(
                formation_surface_fleet_snapshot.c.instance_id == instance_id,
                formation_surface_fleet_snapshot.c.fleet_index == fleet_index,
            )
            .order_by(
                formation_surface_fleet_snapshot.c.observed_at.desc(),
                formation_surface_fleet_snapshot.c.id.desc(),
            )
            .limit(limit)
        )
        return self._read(statement)

    def complete_in_window(
        self,
        instance_id: UUID,
        selection: FleetSelection,
        *,
        start: datetime,
        end: datetime,
    ) -> tuple[int, ...]:
        if not isinstance(start, datetime) or start.tzinfo is None:
            raise StorageInvalidDataError("start должен содержать timezone.")
        if not isinstance(end, datetime) or end.tzinfo is None or end <= start:
            raise StorageInvalidDataError("end должен быть позже start и содержать timezone.")
        statement = (
            select(formation_surface_fleet_snapshot.c.fleet_index)
            .where(
                formation_surface_fleet_snapshot.c.instance_id == instance_id,
                formation_surface_fleet_snapshot.c.fleet_index.in_(
                    selection.fleet_indices
                ),
                formation_surface_fleet_snapshot.c.complete.is_(True),
                formation_surface_fleet_snapshot.c.observed_at >= start,
                formation_surface_fleet_snapshot.c.observed_at < end,
            )
            .distinct()
            .order_by(formation_surface_fleet_snapshot.c.fleet_index)
        )
        try:
            return tuple(self._connection.execute(statement).scalars().all())
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def latest_attempts(
        self,
        instance_id: UUID,
        selection: FleetSelection,
        *,
        source: str,
    ) -> tuple[FleetScanAttempt, ...]:
        if not isinstance(source, str) or not source.strip() or len(source) > 64:
            raise StorageInvalidDataError("source некорректен.")
        ranked = (
            select(
                formation_surface_fleet_scan_request.c.fleet_index,
                formation_surface_fleet_scan_run.c.id.label("run_id"),
                formation_surface_fleet_scan_run.c.source,
                formation_surface_fleet_scan_run.c.started_at,
                formation_surface_fleet_scan_run.c.status,
                formation_surface_fleet_scan_run.c.error_code,
                func.row_number()
                .over(
                    partition_by=formation_surface_fleet_scan_request.c.fleet_index,
                    order_by=(
                        formation_surface_fleet_scan_run.c.started_at.desc(),
                        formation_surface_fleet_scan_run.c.id.desc(),
                    ),
                )
                .label("rank"),
            )
            .select_from(
                formation_surface_fleet_scan_run.join(
                    formation_surface_fleet_scan_request,
                    formation_surface_fleet_scan_request.c.run_id
                    == formation_surface_fleet_scan_run.c.id,
                )
            )
            .where(
                formation_surface_fleet_scan_run.c.instance_id == instance_id,
                formation_surface_fleet_scan_run.c.source == source,
                formation_surface_fleet_scan_request.c.fleet_index.in_(
                    selection.fleet_indices
                ),
            )
            .subquery()
        )
        statement = (
            select(
                ranked.c.run_id,
                ranked.c.fleet_index,
                ranked.c.source,
                ranked.c.started_at,
                ranked.c.status,
                ranked.c.error_code,
            )
            .where(ranked.c.rank == 1)
            .order_by(ranked.c.fleet_index)
        )
        try:
            rows = tuple(self._connection.execute(statement).mappings().all())
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None
        try:
            return tuple(
                FleetScanAttempt(
                    run_id=row["run_id"],
                    fleet_index=row["fleet_index"],
                    source=row["source"],
                    started_at=row["started_at"],
                    status=FleetScanRunStatus(row["status"]),
                    error_code=row["error_code"],
                )
                for row in rows
            )
        except (TypeError, ValueError):
            raise StorageInvalidDataError(
                "PostgreSQL содержит некорректную Fleet scan attempt."
            ) from None

    def _read(self, statement) -> tuple[FleetStateObservation, ...]:
        try:
            snapshot_rows = tuple(
                self._connection.execute(statement).mappings().all()
            )
            if not snapshot_rows:
                return ()
            snapshot_ids = tuple(row["id"] for row in snapshot_rows)
            slot_rows = tuple(
                self._connection.execute(
                    select(formation_surface_fleet_slot)
                    .where(
                        formation_surface_fleet_slot.c.snapshot_id.in_(snapshot_ids)
                    )
                    .order_by(
                        formation_surface_fleet_slot.c.snapshot_id,
                        formation_surface_fleet_slot.c.side,
                        formation_surface_fleet_slot.c.position,
                    )
                )
                .mappings()
                .all()
            )
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None
        by_snapshot: dict[UUID, list[Mapping[str, object]]] = {
            snapshot_id: [] for snapshot_id in snapshot_ids
        }
        for slot in slot_rows:
            by_snapshot[slot["snapshot_id"]].append(slot)
        try:
            return tuple(
                self._hydrate(row, by_snapshot[row["id"]]) for row in snapshot_rows
            )
        except StorageInvalidDataError:
            raise
        except (KeyError, TypeError, ValueError):
            raise StorageInvalidDataError(
                "PostgreSQL содержит некорректный Fleet snapshot."
            ) from None

    @staticmethod
    def _hydrate(
        row: Mapping[str, object],
        slot_rows: Sequence[Mapping[str, object]],
    ) -> FleetStateObservation:
        if len(slot_rows) != 6:
            raise StorageInvalidDataError(
                "Fleet snapshot должен содержать ровно шесть slot rows."
            )
        slots = []
        for slot in sorted(
            slot_rows,
            key=lambda item: (
                0 if item["side"] == FormationFleetSide.MAIN.value else 1,
                item["position"],
            ),
        ):
            status_value = slot["identity_status"]
            status = IdentityStatus(status_value) if status_value is not None else None
            ship_form_value = slot["ship_form"]
            try:
                ship_form = ShipForm(ship_form_value) if ship_form_value is not None else None
            except (TypeError, ValueError):
                raise StorageInvalidDataError(
                    "PostgreSQL содержит некорректную форму Fleet slot."
                ) from None
            if status is IdentityStatus.MATCHED and ship_form is None:
                raise StorageInvalidDataError(
                    "MATCHED Fleet slot в PostgreSQL не содержит форму корабля."
                )
            if status is not IdentityStatus.MATCHED and ship_form is not None:
                raise StorageInvalidDataError(
                    "Только MATCHED Fleet slot в PostgreSQL может содержать форму корабля."
                )
            canonical_key = slot["canonical_identity_key"]
            slots.append(
                FormationFleetSlotObservation(
                    side=FormationFleetSide(slot["side"]),
                    position=slot["position"],
                    occupied=slot["occupied"],
                    identity_status=status,
                    raw_name_ocr=slot["raw_name_ocr"],
                    displayed_name=slot["displayed_name"],
                    canonical_identity=(
                        CanonicalShipIdentity(canonical_key)
                        if canonical_key is not None
                        else None
                    ),
                    canonical_name=slot["canonical_name"],
                    ship_form=ship_form,
                )
            )
        snapshot = FormationFleetSnapshot(
            fleet_index=row["fleet_index"],
            slots=tuple(slots),
            catalog_fingerprint=row["catalog_fingerprint"],
        )
        if snapshot.complete != row["complete"]:
            raise StorageInvalidDataError(
                "Fleet snapshot complete не соответствует slot rows."
            )
        return FleetStateObservation(
            id=row["id"],
            run_id=row["run_id"],
            instance_id=row["instance_id"],
            idempotency_key=row["idempotency_key"],
            observed_at=row["observed_at"],
            snapshot=snapshot,
        )
