"""PostgreSQL-адаптер неизменяемой истории сканов морали в Dorm."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import Connection, insert, select
from sqlalchemy.exc import SQLAlchemyError

from module.application.canonical_payload import payload_digest
from module.application.errors import StorageConflictError, StorageInvalidDataError
from module.dock_inventory.model import CanonicalShipIdentity, IdentityStatus, ShipForm
from module.dorm.morale_model import (
    DormFloor,
    DormFloorScanAttempt,
    DormFloorScanStatus,
    DormFloorSnapshot,
    DormMoraleObservation,
    DormMoraleScanResult,
)
from module.persistence.database import translate_database_error
from module.persistence.schema import dorm_morale_scan_observation, dorm_morale_scan_run


def _payload(scan: DormMoraleScanResult) -> dict[str, object]:
    return {
        "started_at": scan.started_at,
        "finished_at": scan.finished_at,
        "status": scan.status,
        "source": scan.source,
        "catalog_fingerprint": scan.catalog_fingerprint,
        "attempts": tuple(
            {
                "floor": attempt.floor,
                "status": attempt.status,
                "observed_at": attempt.observed_at,
                "error_code": attempt.error_code,
                "observations": tuple(
                    {
                        "ordinal": item.ordinal,
                        "raw_name_ocr": item.raw_name_ocr,
                        "displayed_name": item.displayed_name,
                        "identity_status": item.identity_status,
                        "canonical_identity_key": (
                            item.canonical_identity.key
                            if item.canonical_identity is not None
                            else None
                        ),
                        "canonical_name": item.canonical_name,
                        "ship_form": item.ship_form,
                        "morale": item.morale,
                        "recovery_per_hour": item.recovery_per_hour,
                    }
                    for item in (
                        attempt.snapshot.observations if attempt.snapshot else ()
                    )
                ),
            }
            for attempt in scan.attempts
        ),
    }


class PostgresDormMoraleRepository:
    def __init__(self, connection: Connection):
        self._connection = connection

    def append_scan(
        self, instance_id: UUID, scan: DormMoraleScanResult
    ) -> DormMoraleScanResult:
        if not isinstance(instance_id, UUID) or not isinstance(
            scan, DormMoraleScanResult
        ):
            raise StorageInvalidDataError("Dorm morale scan имеет неверный тип.")
        digest = payload_digest(_payload(scan))
        try:
            existing = (
                self._connection.execute(
                    select(dorm_morale_scan_run).where(
                        dorm_morale_scan_run.c.instance_id == instance_id,
                        dorm_morale_scan_run.c.idempotency_key == scan.idempotency_key,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if existing["payload_digest"] != digest:
                    raise StorageConflictError(
                        "Dorm scan idempotency key содержит другой payload."
                    )
                return self._hydrate(existing)
            attempts = {item.floor: item for item in scan.attempts}
            floor_1 = attempts[DormFloor.FLOOR_1]
            floor_2 = attempts[DormFloor.FLOOR_2]
            self._connection.execute(
                insert(dorm_morale_scan_run).values(
                    id=scan.id,
                    instance_id=instance_id,
                    idempotency_key=scan.idempotency_key,
                    payload_digest=digest,
                    started_at=scan.started_at,
                    finished_at=scan.finished_at,
                    status=scan.status.value,
                    source=scan.source,
                    catalog_fingerprint=scan.catalog_fingerprint,
                    floor_1_status=floor_1.status.value,
                    floor_1_observed_at=floor_1.observed_at,
                    floor_1_error_code=floor_1.error_code,
                    floor_2_status=floor_2.status.value,
                    floor_2_observed_at=floor_2.observed_at,
                    floor_2_error_code=floor_2.error_code,
                )
            )
            rows = [
                {
                    "scan_id": scan.id,
                    "instance_id": instance_id,
                    "floor": item.floor.value,
                    "ordinal": item.ordinal,
                    "raw_name_ocr": item.raw_name_ocr,
                    "displayed_name": item.displayed_name,
                    "identity_status": item.identity_status.value,
                    "canonical_identity_key": (
                        item.canonical_identity.key if item.canonical_identity else None
                    ),
                    "canonical_name": item.canonical_name,
                    "ship_form": item.ship_form.value if item.ship_form else None,
                    "morale": item.morale,
                    "recovery_per_hour": item.recovery_per_hour,
                }
                for item in scan.observations
            ]
            if rows:
                self._connection.execute(insert(dorm_morale_scan_observation), rows)
            return scan
        except StorageConflictError:
            raise
        except KeyError, TypeError, ValueError:
            raise StorageInvalidDataError(
                "PostgreSQL содержит некорректный Dorm morale scan."
            ) from None
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def latest(self, instance_id: UUID) -> DormMoraleScanResult | None:
        if not isinstance(instance_id, UUID):
            raise StorageInvalidDataError("Dorm morale latest request некорректен.")
        try:
            row = (
                self._connection.execute(
                    select(dorm_morale_scan_run)
                    .where(dorm_morale_scan_run.c.instance_id == instance_id)
                    .order_by(
                        dorm_morale_scan_run.c.finished_at.desc(),
                        dorm_morale_scan_run.c.id.desc(),
                    )
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            return None if row is None else self._hydrate(row)
        except KeyError, TypeError, ValueError:
            raise StorageInvalidDataError(
                "PostgreSQL содержит некорректный Dorm morale scan."
            ) from None
        except SQLAlchemyError as exc:
            raise translate_database_error(exc) from None

    def _hydrate(self, row: Mapping[str, object]) -> DormMoraleScanResult:
        child_rows = (
            self._connection.execute(
                select(dorm_morale_scan_observation)
                .where(dorm_morale_scan_observation.c.scan_id == row["id"])
                .order_by(
                    dorm_morale_scan_observation.c.floor,
                    dorm_morale_scan_observation.c.ordinal,
                )
            )
            .mappings()
            .all()
        )
        by_floor: dict[DormFloor, list[DormMoraleObservation]] = {
            DormFloor.FLOOR_1: [],
            DormFloor.FLOOR_2: [],
        }
        for item in child_rows:
            floor = DormFloor(item["floor"])
            by_floor[floor].append(
                DormMoraleObservation(
                    floor=floor,
                    ordinal=item["ordinal"],
                    raw_name_ocr=item["raw_name_ocr"],
                    displayed_name=item["displayed_name"],
                    identity_status=IdentityStatus(item["identity_status"]),
                    canonical_identity=(
                        CanonicalShipIdentity(item["canonical_identity_key"])
                        if item["canonical_identity_key"] is not None
                        else None
                    ),
                    canonical_name=item["canonical_name"],
                    ship_form=(
                        ShipForm(item["ship_form"])
                        if item["ship_form"] is not None
                        else None
                    ),
                    morale=item["morale"],
                    recovery_per_hour=item["recovery_per_hour"],
                )
            )
        attempts = []
        for floor, prefix in (
            (DormFloor.FLOOR_1, "floor_1"),
            (DormFloor.FLOOR_2, "floor_2"),
        ):
            status = DormFloorScanStatus(row[f"{prefix}_status"])
            snapshot = None
            if status is DormFloorScanStatus.SUCCEEDED:
                snapshot = DormFloorSnapshot(
                    floor=floor,
                    observations=tuple(by_floor[floor]),
                    catalog_fingerprint=row["catalog_fingerprint"],
                )
            elif by_floor[floor]:
                raise ValueError("Неуспешный floor scan содержит observations")
            attempts.append(
                DormFloorScanAttempt(
                    floor=floor,
                    status=status,
                    observed_at=row[f"{prefix}_observed_at"],
                    snapshot=snapshot,
                    error_code=row[f"{prefix}_error_code"],
                )
            )
        return DormMoraleScanResult(
            id=row["id"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            attempts=tuple(attempts),
            source=row["source"],
            idempotency_key=row["idempotency_key"],
        )


__all__ = ["PostgresDormMoraleRepository"]
