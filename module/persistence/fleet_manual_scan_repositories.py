"""PostgreSQL-адаптер устойчивых команд ручного сканирования флотов."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from sqlalchemy import Connection, exists, insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import SQLAlchemyError

from module.application.errors import StorageConflictError, StorageInvalidDataError
from module.application.fleet_manual_scan import (
    FleetManualScanCommand,
    FleetManualScanStatus,
    FleetManualScanSubmission,
)
from module.formation.model import FleetSelection
from module.persistence.database import translate_database_error
from module.persistence.schema import (
    formation_surface_fleet_scan_command,
    formation_surface_fleet_scan_command_fleet,
)

_ACTIVE_STATUSES = (
    FleetManualScanStatus.PENDING.value,
    FleetManualScanStatus.RUNNING.value,
)


class PostgresFleetManualScanCommandRepository:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def create_pending(
        self,
        instance_id: UUID,
        command_id: UUID,
        selection: FleetSelection,
        *,
        created_at: datetime,
    ) -> FleetManualScanSubmission:
        if not isinstance(selection, FleetSelection):
            raise StorageInvalidDataError("Manual Fleet selection некорректен.")
        try:
            statement = (
                postgresql_insert(formation_surface_fleet_scan_command)
                .values(
                    id=command_id,
                    instance_id=instance_id,
                    created_at=created_at,
                    status=FleetManualScanStatus.PENDING.value,
                )
                .on_conflict_do_nothing(
                    index_elements=[formation_surface_fleet_scan_command.c.instance_id],
                    index_where=formation_surface_fleet_scan_command.c.status.in_(
                        _ACTIVE_STATUSES
                    ),
                )
                .returning(formation_surface_fleet_scan_command.c.id)
            )
            inserted_id = self._connection.execute(statement).scalar_one_or_none()
            if inserted_id is not None:
                self._connection.execute(
                    insert(formation_surface_fleet_scan_command_fleet),
                    [
                        {"command_id": command_id, "fleet_index": fleet_index}
                        for fleet_index in selection.fleet_indices
                    ],
                )
                command = FleetManualScanCommand(
                    id=command_id,
                    instance_id=instance_id,
                    selection=selection,
                    created_at=created_at,
                    status=FleetManualScanStatus.PENDING,
                )
                return FleetManualScanSubmission(command=command, created=True)

            row = self._connection.execute(
                select(formation_surface_fleet_scan_command)
                .where(
                    formation_surface_fleet_scan_command.c.instance_id == instance_id,
                    formation_surface_fleet_scan_command.c.status.in_(_ACTIVE_STATUSES),
                )
                .order_by(
                    formation_surface_fleet_scan_command.c.created_at,
                    formation_surface_fleet_scan_command.c.id,
                )
                .with_for_update()
                .limit(1)
            ).mappings().one_or_none()
            if row is None:
                raise StorageConflictError(
                    "Active manual Fleet command не найден после duplicate submit."
                )
            return FleetManualScanSubmission(
                command=self._hydrate(row),
                created=False,
            )
        except StorageConflictError:
            raise
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def latest(self, instance_id: UUID) -> FleetManualScanCommand | None:
        try:
            row = self._connection.execute(
                select(formation_surface_fleet_scan_command)
                .where(formation_surface_fleet_scan_command.c.instance_id == instance_id)
                .order_by(
                    formation_surface_fleet_scan_command.c.created_at.desc(),
                    formation_surface_fleet_scan_command.c.id.desc(),
                )
                .limit(1)
            ).mappings().one_or_none()
            return self._hydrate(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def pending_exists(self, instance_id: UUID) -> bool:
        try:
            return bool(
                self._connection.execute(
                    select(
                        exists().where(
                            formation_surface_fleet_scan_command.c.instance_id
                            == instance_id,
                            formation_surface_fleet_scan_command.c.status
                            == FleetManualScanStatus.PENDING.value,
                        )
                    )
                ).scalar_one()
            )
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def claim_next(
        self,
        instance_id: UUID,
        *,
        started_at: datetime,
    ) -> FleetManualScanCommand | None:
        try:
            command_id = self._connection.execute(
                select(formation_surface_fleet_scan_command.c.id)
                .where(
                    formation_surface_fleet_scan_command.c.instance_id == instance_id,
                    formation_surface_fleet_scan_command.c.status
                    == FleetManualScanStatus.PENDING.value,
                )
                .order_by(
                    formation_surface_fleet_scan_command.c.created_at,
                    formation_surface_fleet_scan_command.c.id,
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            ).scalar_one_or_none()
            if command_id is None:
                return None
            row = self._connection.execute(
                update(formation_surface_fleet_scan_command)
                .where(
                    formation_surface_fleet_scan_command.c.id == command_id,
                    formation_surface_fleet_scan_command.c.instance_id == instance_id,
                    formation_surface_fleet_scan_command.c.status
                    == FleetManualScanStatus.PENDING.value,
                )
                .values(
                    status=FleetManualScanStatus.RUNNING.value,
                    started_at=started_at,
                )
                .returning(formation_surface_fleet_scan_command)
            ).mappings().one_or_none()
            if row is None:
                raise StorageConflictError("Manual Fleet command claim отклонён.")
            return self._hydrate(row)
        except StorageConflictError:
            raise
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def finish(
        self,
        command_id: UUID,
        instance_id: UUID,
        *,
        status: FleetManualScanStatus,
        finished_at: datetime,
        result_run_id: UUID | None,
        error_code: str | None,
    ) -> FleetManualScanCommand:
        if status not in {
            FleetManualScanStatus.SUCCEEDED,
            FleetManualScanStatus.PARTIAL,
            FleetManualScanStatus.FAILED,
        }:
            raise StorageInvalidDataError("Manual Fleet terminal status некорректен.")
        try:
            row = self._connection.execute(
                update(formation_surface_fleet_scan_command)
                .where(
                    formation_surface_fleet_scan_command.c.id == command_id,
                    formation_surface_fleet_scan_command.c.instance_id == instance_id,
                    formation_surface_fleet_scan_command.c.status
                    == FleetManualScanStatus.RUNNING.value,
                )
                .values(
                    status=status.value,
                    finished_at=finished_at,
                    result_run_id=result_run_id,
                    error_code=error_code,
                )
                .returning(formation_surface_fleet_scan_command)
            ).mappings().one_or_none()
            if row is None:
                raise StorageConflictError("Manual Fleet command transition отклонён.")
            return self._hydrate(row)
        except StorageConflictError:
            raise
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def fail_running(
        self,
        instance_id: UUID,
        *,
        finished_at: datetime,
        error_code: str,
    ) -> int:
        try:
            result = self._connection.execute(
                update(formation_surface_fleet_scan_command)
                .where(
                    formation_surface_fleet_scan_command.c.instance_id == instance_id,
                    formation_surface_fleet_scan_command.c.status
                    == FleetManualScanStatus.RUNNING.value,
                )
                .values(
                    status=FleetManualScanStatus.FAILED.value,
                    finished_at=finished_at,
                    result_run_id=None,
                    error_code=error_code,
                )
            )
            return int(result.rowcount or 0)
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def _selection(self, command_id: UUID) -> FleetSelection:
        rows = self._connection.execute(
            select(formation_surface_fleet_scan_command_fleet.c.fleet_index)
            .where(
                formation_surface_fleet_scan_command_fleet.c.command_id == command_id
            )
            .order_by(formation_surface_fleet_scan_command_fleet.c.fleet_index)
        ).scalars().all()
        try:
            return FleetSelection(tuple(rows))
        except (TypeError, ValueError):
            raise StorageInvalidDataError(
                "PostgreSQL содержит некорректную manual Fleet selection."
            ) from None

    def _hydrate(self, row: Mapping[str, object]) -> FleetManualScanCommand:
        try:
            return FleetManualScanCommand(
                id=row["id"],
                instance_id=row["instance_id"],
                selection=self._selection(row["id"]),
                created_at=row["created_at"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                status=FleetManualScanStatus(row["status"]),
                result_run_id=row["result_run_id"],
                error_code=row["error_code"],
            )
        except StorageInvalidDataError:
            raise
        except (KeyError, TypeError, ValueError):
            raise StorageInvalidDataError(
                "PostgreSQL содержит некорректную manual Fleet command."
            ) from None


__all__ = ["PostgresFleetManualScanCommandRepository"]
