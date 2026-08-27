"""Реальная Windows/MuMu-приёмка Dorm Morale Stage 2."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import cv2
import numpy as np
from sqlalchemy import delete, func, select, text

from module.application.errors import StorageConflictError
from module.application.fleet_state import FleetStateObservation
from module.application.instance_identity import runtime_instance_identity
from module.application.morale_reconciliation import (
    MoraleReconciliationResult,
    MoraleReconciliationService,
)
from module.config.config import AzurLaneConfig
from module.device.device import Device
from module.dock_inventory.model import IdentityStatus
from module.dorm.morale_controller import DormMoraleController
from module.dorm.morale_model import (
    DormFloor,
    DormFloorScanStatus,
    DormMoraleScanResult,
    DormMoraleScanStatus,
)
from module.formation.model import FleetSelection
from module.persistence.config import DEFAULT_BACKEND_MARKER_PATH, DatabaseSettings
from module.persistence.database import LazyEngine, StorageHealthChecker
from module.persistence.local_environment import (
    DEFAULT_LOCAL_ENV_PATH,
    read_local_postgres_environment,
)
from module.persistence.schema import (
    dorm_morale_scan_observation,
    dorm_morale_scan_run,
    formation_surface_fleet_morale_observation,
)
from module.persistence.unit_of_work import PostgresUnitOfWork
from module.ui.page import page_main
from tools.acceptance.device import (
    AcceptanceFailure,
    _git_head_sha,
    _load_profile,
    _resolve_serial,
    _safe_text,
    _validate_profile_name,
)

DEFAULT_REPORT = Path("artifacts/acceptance/dorm-morale-stage2.json")
DEFAULT_EVIDENCE_DIR = Path("artifacts/acceptance/dorm-morale-stage2-evidence")
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _safe_label(value: str) -> str:
    safe = re.sub(r"[^0-9A-Za-zА-Яа-я._-]+", "-", value).strip("-")
    return safe[:80] or "frame"


class _EvidenceDevice(Device):
    """Штатный Device с пассивной записью уже полученных кадров."""

    def __init__(self, config: AzurLaneConfig, evidence_dir: Path):
        self._evidence_dir = evidence_dir
        self._frames_dir = evidence_dir / "frames"
        self._frames_dir.mkdir(parents=True, exist_ok=True)
        self._timeline_path = evidence_dir / "timeline.jsonl"
        self._frame_index = 0
        self._phase = "preflight"
        super().__init__(config=config)

    def set_evidence_phase(self, phase: str) -> None:
        self._phase = _safe_label(phase)

    def _save_frame(self, frame: np.ndarray, *, label: str, extension: str) -> Path:
        self._frame_index += 1
        filename = f"{self._frame_index:04d}-{_safe_label(label)}.{extension}"
        destination = self._frames_dir / filename
        if extension == "jpg":
            ok, encoded = cv2.imencode(
                ".jpg",
                frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), 90],
            )
            if not ok:
                raise AcceptanceFailure("Не удалось закодировать диагностический JPEG.")
            destination.write_bytes(encoded.tobytes())
        elif not cv2.imwrite(str(destination), frame):
            raise AcceptanceFailure("Не удалось сохранить диагностический PNG.")
        payload = {
            "captured_at_utc": datetime.now(UTC).isoformat(),
            "phase": self._phase,
            "label": label,
            "filename": str(destination.name),
            "shape": list(frame.shape),
        }
        with self._timeline_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return destination

    def screenshot(self, *args, **kwargs):
        result = super().screenshot(*args, **kwargs)
        frame = self.image
        if isinstance(frame, np.ndarray):
            self._save_frame(frame, label=self._phase, extension="jpg")
        return result

    def save_checkpoint(self, label: str) -> Path | None:
        frame = self.image
        if not isinstance(frame, np.ndarray):
            return None
        return self._save_frame(frame, label=label, extension="png")


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _exception_text(error: BaseException) -> str:
    return _safe_text(str(error) or type(error).__name__)


def _build_storage() -> tuple[LazyEngine, Callable[[], PostgresUnitOfWork]]:
    marker_path = _REPOSITORY_ROOT / DEFAULT_BACKEND_MARKER_PATH
    settings = DatabaseSettings.from_backend_marker(marker_path)
    local_environment = read_local_postgres_environment(
        _REPOSITORY_ROOT / DEFAULT_LOCAL_ENV_PATH
    )
    if local_environment is not None:
        local_environment.require_app_runtime_match(settings)
        local_environment.install(role="app")

    engine = LazyEngine(settings)
    try:
        StorageHealthChecker(engine).require_ready()
    except BaseException:
        engine.dispose()
        raise

    def uow_factory() -> PostgresUnitOfWork:
        return PostgresUnitOfWork(engine)

    return engine, uow_factory


def _require_database_privileges(engine: LazyEngine) -> dict[str, tuple[str, ...]]:
    requirements = {
        dorm_morale_scan_run: ("SELECT", "INSERT", "DELETE"),
        dorm_morale_scan_observation: ("SELECT", "INSERT"),
        formation_surface_fleet_morale_observation: ("SELECT", "INSERT", "DELETE"),
    }
    verified: dict[str, tuple[str, ...]] = {}
    statement = text(
        "SELECT has_table_privilege(current_user, :table_name, :privilege)"
    )
    with engine.get().connect() as connection:
        for table, privileges in requirements.items():
            missing = tuple(
                privilege
                for privilege in privileges
                if not bool(
                    connection.execute(
                        statement,
                        {"table_name": table.fullname, "privilege": privilege},
                    ).scalar_one()
                )
            )
            if missing:
                raise AcceptanceFailure(
                    "Штатная роль приложения не имеет обязательных прав PostgreSQL "
                    f"на {table.fullname}: {', '.join(missing)}. "
                    "Приёмка остановлена до изменения UI и данных."
                )
            verified[table.fullname] = privileges
    return verified


def _saved_fleet_state(
    uow_factory: Callable[[], PostgresUnitOfWork],
    profile: str,
    selection: FleetSelection,
) -> tuple[UUID, tuple[FleetStateObservation, ...]]:
    _digest, instance_id = runtime_instance_identity(profile)
    with uow_factory() as uow:
        formations = uow.fleet_state.latest(instance_id, selection)

    expected = selection.fleet_indices
    actual = tuple(item.fleet_index for item in formations)
    if actual != expected:
        raise AcceptanceFailure(
            "Для приёмки нужен уже сохранённый полный Fleet State всех флотов. "
            f"Ожидались {expected}, найдены {actual}. Новый Formation scan не запускается."
        )

    incomplete = tuple(
        item.fleet_index for item in formations if not item.snapshot.complete
    )
    if incomplete:
        raise AcceptanceFailure(
            "Сохранённый Fleet State содержит неполные снимки флотов: "
            + ", ".join(map(str, incomplete))
        )

    invalid_slots = []
    for formation in formations:
        for slot in formation.snapshot.slots:
            if not slot.occupied:
                continue
            if (
                slot.identity_status is not IdentityStatus.MATCHED
                or slot.canonical_identity is None
                or slot.canonical_name is None
                or slot.ship_form is None
            ):
                invalid_slots.append(
                    f"{formation.fleet_index}:{slot.side.value}:{slot.position}"
                )
    if invalid_slots:
        raise AcceptanceFailure(
            "Сохранённый Fleet State содержит занятые слоты без точной идентичности и формы: "
            + ", ".join(invalid_slots)
        )

    fingerprints = {item.snapshot.catalog_fingerprint for item in formations}
    if len(fingerprints) != 1:
        raise AcceptanceFailure(
            "Сохранённые снимки Fleet State используют разные отпечатки каталога."
        )
    return instance_id, formations


def _scan_payload(scan: DormMoraleScanResult) -> dict[str, Any]:
    return {
        "id": str(scan.id),
        "status": scan.status.value,
        "complete": scan.complete,
        "source": scan.source,
        "idempotency_key": scan.idempotency_key,
        "catalog_fingerprint": scan.catalog_fingerprint,
        "started_at": scan.started_at,
        "finished_at": scan.finished_at,
        "attempts": [
            {
                "floor": attempt.floor.value,
                "status": attempt.status.value,
                "observed_at": attempt.observed_at,
                "error_code": attempt.error_code,
                "observations": []
                if attempt.snapshot is None
                else [
                    {
                        "ordinal": observation.ordinal,
                        "raw_name_ocr": observation.raw_name_ocr,
                        "displayed_name": observation.displayed_name,
                        "identity_status": observation.identity_status.value,
                        "canonical_identity": (
                            None
                            if observation.canonical_identity is None
                            else observation.canonical_identity.key
                        ),
                        "canonical_name": observation.canonical_name,
                        "ship_form": (
                            None
                            if observation.ship_form is None
                            else observation.ship_form.value
                        ),
                        "morale": str(observation.morale),
                        "recovery_per_hour": str(observation.recovery_per_hour),
                    }
                    for observation in attempt.snapshot.observations
                ],
            }
            for attempt in scan.attempts
        ],
    }


def _reconciliation_payload(result: MoraleReconciliationResult) -> dict[str, Any]:
    return {
        "dorm_scan_id": str(result.dorm_scan_id),
        "complete_scan": result.complete_scan,
        "exact_observations": result.exact_observations,
        "outside_dorm_observations": result.outside_dorm_observations,
        "ambiguous_observations": result.ambiguous_observations,
        "unresolved_observations": result.unresolved_observations,
        "unmatched_observations": result.unmatched_observations,
        "stale_fleet_indices": list(result.stale_fleet_indices),
    }


def _validate_scan(scan: DormMoraleScanResult) -> None:
    if scan.status is not DormMoraleScanStatus.SUCCEEDED or not scan.complete:
        errors = ", ".join(
            f"{attempt.floor.value}:{attempt.status.value}:{attempt.error_code or '-'}"
            for attempt in scan.attempts
        )
        raise AcceptanceFailure(
            "Сканирование Dorm не завершило оба этажа успешно: " + errors
        )

    attempts = {attempt.floor: attempt for attempt in scan.attempts}
    for floor in (DormFloor.FLOOR_1, DormFloor.FLOOR_2):
        attempt = attempts[floor]
        if (
            attempt.status is not DormFloorScanStatus.SUCCEEDED
            or attempt.snapshot is None
        ):
            raise AcceptanceFailure(
                f"Dorm {floor.value} не содержит успешного штатного снимка."
            )
        unresolved = tuple(
            observation.ordinal
            for observation in attempt.snapshot.observations
            if observation.identity_status is not IdentityStatus.MATCHED
            or observation.canonical_identity is None
        )
        if unresolved:
            raise AcceptanceFailure(
                f"Dorm {floor.value} содержит неразрешённые слоты карточек: {unresolved}."
            )


def _validate_reconciliation(
    scan: DormMoraleScanResult,
    result: MoraleReconciliationResult,
) -> None:
    if result.dorm_scan_id != scan.id:
        raise AcceptanceFailure("Сопоставление вернуло другой идентификатор сканирования Dorm.")
    if not result.complete_scan:
        raise AcceptanceFailure("Сопоставление не признало сканирование Dorm полным.")
    if result.stale_fleet_indices:
        raise AcceptanceFailure(
            "Fleet State устарел относительно сканирования Dorm: "
            + ", ".join(map(str, result.stale_fleet_indices))
        )
    if result.unresolved_observations:
        raise AcceptanceFailure(
            f"Сопоставление содержит нераспознанных наблюдений: {result.unresolved_observations}."
        )
    if result.ambiguous_observations:
        raise AcceptanceFailure(
            f"Сопоставление содержит неоднозначных наблюдений: {result.ambiguous_observations}."
        )
    if result.unmatched_observations:
        raise AcceptanceFailure(
            f"Сопоставление содержит несопоставленных наблюдений: {result.unmatched_observations}."
        )
    if result.exact_observations != len(scan.observations):
        raise AcceptanceFailure(
            "Число точных наблюдений morale не совпадает с числом наблюдений Dorm."
        )


def _persisted_counts(
    engine: LazyEngine,
    instance_id: UUID,
    scan_id: UUID,
) -> dict[str, int]:
    with engine.get().connect() as connection:
        scan_rows = connection.execute(
            select(func.count())
            .select_from(dorm_morale_scan_run)
            .where(
                dorm_morale_scan_run.c.instance_id == instance_id,
                dorm_morale_scan_run.c.id == scan_id,
            )
        ).scalar_one()
        dorm_rows = connection.execute(
            select(func.count())
            .select_from(dorm_morale_scan_observation)
            .where(
                dorm_morale_scan_observation.c.instance_id == instance_id,
                dorm_morale_scan_observation.c.scan_id == scan_id,
            )
        ).scalar_one()
        morale_rows = connection.execute(
            select(func.count())
            .select_from(formation_surface_fleet_morale_observation)
            .where(
                formation_surface_fleet_morale_observation.c.instance_id
                == instance_id,
                formation_surface_fleet_morale_observation.c.dorm_scan_id == scan_id,
            )
        ).scalar_one()
    return {
        "scan_rows": int(scan_rows),
        "dorm_observation_rows": int(dorm_rows),
        "morale_rows": int(morale_rows),
    }


def _validate_persistence(
    uow_factory: Callable[[], PostgresUnitOfWork],
    engine: LazyEngine,
    instance_id: UUID,
    selection: FleetSelection,
    scan: DormMoraleScanResult,
    result: MoraleReconciliationResult,
) -> dict[str, Any]:
    with uow_factory() as uow:
        stored_scan = uow.dorm_morale.latest(instance_id)
        morale_rows = uow.morale.latest(instance_id, selection)

    if stored_scan is None or stored_scan.id != scan.id:
        raise AcceptanceFailure("Повторное чтение PostgreSQL не вернуло текущее сканирование Dorm.")
    if stored_scan.idempotency_key != scan.idempotency_key:
        raise AcceptanceFailure(
            "Повторное чтение PostgreSQL изменило семантический ключ идемпотентности Dorm."
        )
    if stored_scan.observations != scan.observations:
        raise AcceptanceFailure(
            "Повторное чтение PostgreSQL изменило семантику наблюдений Dorm."
        )

    rows_from_scan = tuple(row for row in morale_rows if row.dorm_scan_id == scan.id)
    expected_morale_rows = (
        result.exact_observations + result.outside_dorm_observations
    )
    if len(rows_from_scan) != expected_morale_rows:
        raise AcceptanceFailure(
            "Повторное чтение PostgreSQL вернуло неверное число записей morale текущего сканирования Dorm: "
            f"ожидалось {expected_morale_rows}, получено {len(rows_from_scan)}."
        )

    counts = _persisted_counts(engine, instance_id, scan.id)
    if counts["scan_rows"] != 1:
        raise AcceptanceFailure("В PostgreSQL должна существовать ровно одна строка текущего сканирования Dorm.")
    if counts["dorm_observation_rows"] != len(scan.observations):
        raise AcceptanceFailure(
            "Количество сохранённых наблюдений Dorm не совпадает с результатом сканирования."
        )
    if counts["morale_rows"] != expected_morale_rows:
        raise AcceptanceFailure(
            "Количество сохранённых наблюдений morale не совпадает с результатом сопоставления."
        )

    return {
        "counts": counts,
        "stored_scan_id": str(stored_scan.id),
        "stored_idempotency_key": stored_scan.idempotency_key,
        "latest_morale_rows_from_scan": len(rows_from_scan),
    }


def _validate_retry_and_conflict(
    service: MoraleReconciliationService,
    engine: LazyEngine,
    instance_id: UUID,
    profile: str,
    selection: FleetSelection,
    scan: DormMoraleScanResult,
) -> dict[str, Any]:
    before = _persisted_counts(engine, instance_id, scan.id)
    retry_result = service.reconcile(profile, selection, scan)
    after_retry = _persisted_counts(engine, instance_id, scan.id)
    if retry_result.dorm_scan_id != scan.id or after_retry != before:
        raise AcceptanceFailure(
            "Повтор с тем же ключом изменил число сохранённых записей или происхождение сканирования."
        )

    conflicting_scan = replace(
        scan,
        source="acceptance:dorm-morale-conflict",
    )
    conflict = False
    try:
        service.reconcile(profile, selection, conflicting_scan)
    except StorageConflictError:
        conflict = True
    if not conflict:
        raise AcceptanceFailure(
            "Другие данные с тем же ключом идемпотентности Dorm не дали StorageConflictError."
        )

    after_conflict = _persisted_counts(engine, instance_id, scan.id)
    if after_conflict != before:
        raise AcceptanceFailure(
            "Путь конфликта изменил сохранённые записи исходного сканирования Dorm."
        )
    return {
        "same_key_retry": "PASS",
        "different_payload_conflict": "PASS",
        "counts_before": before,
        "counts_after_retry": after_retry,
        "counts_after_conflict": after_conflict,
    }


def _cleanup_database(
    engine: LazyEngine,
    instance_id: UUID,
    scan_id: UUID,
) -> dict[str, Any]:
    with engine.get().begin() as connection:
        morale_result = connection.execute(
            delete(formation_surface_fleet_morale_observation).where(
                formation_surface_fleet_morale_observation.c.instance_id
                == instance_id,
                formation_surface_fleet_morale_observation.c.dorm_scan_id == scan_id,
            )
        )
        scan_result = connection.execute(
            delete(dorm_morale_scan_run).where(
                dorm_morale_scan_run.c.instance_id == instance_id,
                dorm_morale_scan_run.c.id == scan_id,
            )
        )
    remaining = _persisted_counts(engine, instance_id, scan_id)
    if any(remaining.values()):
        raise AcceptanceFailure(
            "Очистка не удалила все записи состояния, созданные этим сканированием Dorm."
        )
    return {
        "morale_rows_deleted": int(morale_result.rowcount or 0),
        "scan_rows_deleted": int(scan_result.rowcount or 0),
        "remaining": remaining,
    }


def _cleanup_ui(controller: DormMoraleController) -> None:
    controller.ui_ensure(page_main)
    controller.device.screenshot()
    if not controller.is_in_main():
        raise AcceptanceFailure("Очистка не подтвердила возврат на Main.")


def _finalize_acceptance(
    *,
    controller: DormMoraleController | None,
    cleanup_ui_required: bool,
    engine: LazyEngine | None,
    database_cleanup_required: bool,
    instance_id: UUID | None,
    scan_id: UUID | None,
    config_path: Path,
    config_before: str,
    cleanup: dict[str, Any],
    primary: BaseException | None,
) -> None:
    cleanup_errors: list[BaseException] = []

    if controller is not None and cleanup_ui_required:
        try:
            if isinstance(controller.device, _EvidenceDevice):
                controller.device.set_evidence_phase("cleanup")
            _cleanup_ui(controller)
            cleanup["ui"] = "PASS"
        except BaseException as error:  # noqa: BLE001 - остальные этапы очистки обязательны.
            cleanup["ui"] = "FAIL"
            cleanup["ui_error"] = _exception_text(error)
            cleanup_errors.append(error)
    else:
        cleanup["ui"] = "SKIPPED_NO_UI_ACTION"

    if (
        engine is not None
        and database_cleanup_required
        and instance_id is not None
        and scan_id is not None
    ):
        try:
            cleanup["database"] = _cleanup_database(engine, instance_id, scan_id)
        except BaseException as error:  # noqa: BLE001 - проверка конфигурации и dispose всё равно обязательны.
            cleanup["database"] = "FAIL"
            cleanup["database_error"] = _exception_text(error)
            cleanup_errors.append(error)
    else:
        cleanup["database"] = "SKIPPED_NO_DATABASE_WRITE"

    try:
        config_after = _sha256(config_path)
        if config_after != config_before:
            raise AcceptanceFailure(
                "Приёмка Dorm Morale изменила постоянную конфигурацию профиля."
            )
        cleanup["config_unchanged"] = True
    except BaseException as error:  # noqa: BLE001 - dispose всё равно обязателен.
        cleanup["config_unchanged"] = False
        cleanup["config_error"] = _exception_text(error)
        cleanup_errors.append(error)

    if engine is not None:
        engine.dispose()

    if primary is not None:
        for error in cleanup_errors:
            primary.add_note("Ошибка очистки: " + _exception_text(error))
        return

    if cleanup_errors:
        error = AcceptanceFailure("Приёмка завершилась с ошибкой очистки.")
        for cleanup_error in cleanup_errors:
            error.add_note(_exception_text(cleanup_error))
        raise error


def _failure_payload(
    *,
    error: BaseException,
    head: str,
    profile: str,
    cleanup: dict[str, Any],
    database_privileges: dict[str, tuple[str, ...]] | None,
    scan: DormMoraleScanResult | None,
) -> dict[str, Any]:
    return {
        "status": "FAIL",
        "title": "Приёмка Dorm Morale Stage 2: FAIL",
        "head_sha": head,
        "profile": profile,
        "error_type": type(error).__name__,
        "error": _exception_text(error),
        "notes": [_safe_text(note) for note in getattr(error, "__notes__", ())],
        "database_privileges": database_privileges,
        "scan": None if scan is None else _scan_payload(scan),
        "cleanup": cleanup,
    }


def run_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    _validate_profile_name(args.profile)
    head = _git_head_sha()
    if args.expected_head and head != args.expected_head:
        raise AcceptanceFailure(
            f"Приёмка Dorm Morale: ожидался head {args.expected_head}, получен {head}."
        )

    config_path = _REPOSITORY_ROOT / "config" / f"{args.profile}.json"
    config_before = _sha256(config_path)
    if config_before is None:
        raise AcceptanceFailure(f"Файл профиля не найден: {config_path}")

    selection = FleetSelection.all()
    engine: LazyEngine | None = None
    controller: DormMoraleController | None = None
    instance_id: UUID | None = None
    scan: DormMoraleScanResult | None = None
    reconciliation: MoraleReconciliationResult | None = None
    persistence: dict[str, Any] | None = None
    retry_conflict: dict[str, Any] | None = None
    database_privileges: dict[str, tuple[str, ...]] | None = None
    primary: BaseException | None = None
    cleanup: dict[str, Any] = {}
    cleanup_ui_required = False
    database_cleanup_required = False
    formations: tuple[FleetStateObservation, ...] = ()

    try:
        engine, uow_factory = _build_storage()
        database_privileges = _require_database_privileges(engine)
        instance_id, formations = _saved_fleet_state(
            uow_factory,
            args.profile,
            selection,
        )
        profile = _load_profile(args.profile)
        serial = _resolve_serial(args, profile)

        print("Приёмка Dorm Morale Stage 2")
        print(f"Точный head: {head}")
        print(f"Профиль: {args.profile}")
        print("Formation не сканируется: используется уже сохранённый Fleet State 1..6.")
        print("Штатный путь: Main -> Dorm -> Train -> 1F -> 2F -> сопоставление -> PostgreSQL.")

        config = AzurLaneConfig(args.profile)
        if config.SERVER != "en":
            raise AcceptanceFailure("Приёмка Dorm Morale поддерживает только EN/Global.")
        config.override(Emulator_Serial=serial)
        device = _EvidenceDevice(config, args.evidence_dir)
        controller = DormMoraleController(config, device=device)
        device.set_evidence_phase("preflight-main")
        controller.device.screenshot()
        if not controller.is_in_main():
            raise AcceptanceFailure(
                "Перед запуском должна быть подтверждена страница Main; "
                "предварительная проверка не исправляет UI."
            )

        cleanup_ui_required = True
        device.set_evidence_phase("dorm")
        scan = controller.scan_both_floors(source="acceptance:dorm-morale")
        device.save_checkpoint("dorm-scan-returned")
        _validate_scan(scan)

        service = MoraleReconciliationService(uow_factory)
        database_cleanup_required = True
        reconciliation = service.reconcile(args.profile, selection, scan)
        _validate_reconciliation(scan, reconciliation)
        persistence = _validate_persistence(
            uow_factory,
            engine,
            instance_id,
            selection,
            scan,
            reconciliation,
        )
        retry_conflict = _validate_retry_and_conflict(
            service,
            engine,
            instance_id,
            args.profile,
            selection,
            scan,
        )
    except BaseException as error:  # noqa: BLE001 - итог должен сохранить исходную ошибку.
        primary = error
        if controller is not None and cleanup_ui_required:
            try:
                controller.device.save_checkpoint("failure")
            except Exception as evidence_error:
                primary.add_note(
                    "Не удалось сохранить диагностический кадр ошибки: "
                    + _exception_text(evidence_error)
                )
    finally:
        try:
            _finalize_acceptance(
                controller=controller,
                cleanup_ui_required=cleanup_ui_required,
                engine=engine,
                database_cleanup_required=database_cleanup_required,
                instance_id=instance_id,
                scan_id=None if scan is None else scan.id,
                config_path=config_path,
                config_before=config_before,
                cleanup=cleanup,
                primary=primary,
            )
        except BaseException as error:  # noqa: BLE001 - ошибка очистки тоже входит в отчёт.
            if primary is None:
                primary = error
            else:
                primary.add_note("Ошибка финализации: " + _exception_text(error))

    if primary is not None:
        return _failure_payload(
            error=primary,
            head=head,
            profile=args.profile,
            cleanup=cleanup,
            database_privileges=database_privileges,
            scan=scan,
        )

    if scan is None or reconciliation is None or persistence is None or retry_conflict is None:
        raise AcceptanceFailure("Приёмка завершилась без полного набора результатов.")

    return {
        "status": "PASS",
        "title": "Приёмка Dorm Morale Stage 2: PASS",
        "head_sha": head,
        "profile": args.profile,
        "server": controller.config.SERVER,
        "selection": list(selection.fleet_indices),
        "fleet_state_observed_at": [item.observed_at for item in formations],
        "database_privileges": database_privileges,
        "scan": _scan_payload(scan),
        "reconciliation": _reconciliation_payload(reconciliation),
        "persistence": persistence,
        "retry_conflict": retry_conflict,
        "cleanup": cleanup,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Реальная Windows/MuMu-приёмка Dorm Morale Stage 2"
    )
    parser.add_argument("--profile", required=True)
    serial_group = parser.add_mutually_exclusive_group(required=True)
    serial_group.add_argument("--serial")
    serial_group.add_argument("--serial-from-config", action="store_true")
    parser.add_argument("--expected-head")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    args = parser.parse_args(argv)

    try:
        report = run_acceptance(args)
    except Exception as exc:  # noqa: BLE001 - ранний preflight тоже должен дать отчёт.
        report = {
            "status": "FAIL",
            "title": "Приёмка Dorm Morale Stage 2: FAIL",
            "error_type": type(exc).__name__,
            "error": _safe_text(str(exc)),
        }

    try:
        _write_json_report(args.report, report)
    except Exception as report_exc:  # noqa: BLE001 - исходный результат выводится в stderr.
        print(
            "Приёмка Dorm Morale: FAIL — не удалось записать отчёт: "
            f"{_safe_text(str(report_exc))}",
            file=sys.stderr,
        )
        return 1

    if report.get("status") != "PASS":
        print(
            f"Приёмка Dorm Morale: FAIL — {report.get('error', 'неизвестная ошибка')}",
            file=sys.stderr,
        )
        print(f"Отчёт: {args.report}", file=sys.stderr)
        return 1

    print("Приёмка Dorm Morale Stage 2: PASS")
    print(f"Отчёт: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
