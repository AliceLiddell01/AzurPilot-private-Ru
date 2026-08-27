"""Ручной end-to-end smoke Stage 2 Dorm morale на реальном устройстве."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from module.application.instance_identity import resolve_runtime_instance
from module.application.morale import (
    MoraleKnowledge,
    MoraleLocation,
    MoraleRecoveryProfile,
)
from module.application.morale_reconciliation import MoraleReconciliationResult
from module.config.config import AzurLaneConfig
from module.config.utils import DEFAULT_CONFIG_NAME
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
from module.persistence.runtime import (
    RuntimeDormMoraleContext,
    build_runtime_dorm_morale_context,
    dispose_runtime_storage,
)
from module.ui.page import page_main


class SmokePreflightError(RuntimeError):
    """Локальное окружение не готово к реальному smoke."""


class SmokeAcceptanceError(RuntimeError):
    """Production-путь нарушил acceptance contract smoke."""


@dataclass(frozen=True, slots=True)
class SmokeMetadata:
    repository: str
    branch: str
    head: str
    config_name: str


class SmokeLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _redact(value: object) -> str:
        text = str(value)
        text = re.sub(
            r"(?i)(postgres(?:ql)?(?:\+\w+)?://[^:/\s]+:)[^@\s]+(@)",
            r"\1<скрыто>\2",
            text,
        )
        text = re.sub(
            r"(?i)\b(password|token|secret|authorization|cookie)=([^\s]+)",
            r"\1=<скрыто>",
            text,
        )
        return text

    def write(self, message: str) -> None:
        timestamp = datetime.now(UTC).isoformat()
        line = f"[{timestamp}] {self._redact(message)}"
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")
        print(line, flush=True)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeAcceptanceError(message)


def _validate_scan(scan: DormMoraleScanResult, log: SmokeLog) -> None:
    _require(
        scan.status is DormMoraleScanStatus.SUCCEEDED and scan.complete,
        f"Dorm scan не завершён полностью: status={scan.status.value}",
    )
    attempts = {attempt.floor: attempt for attempt in scan.attempts}
    for floor in (DormFloor.FLOOR_1, DormFloor.FLOOR_2):
        attempt = attempts[floor]
        _require(
            attempt.status is DormFloorScanStatus.SUCCEEDED,
            f"Скан {floor.value} завершился неуспешно: {attempt.error_code}",
        )
        _require(
            attempt.snapshot is not None,
            f"Успешный скан {floor.value} не содержит snapshot.",
        )
        log.write(
            f"Floor detector {floor.value}: подтверждён production controller; "
            f"observations={len(attempt.snapshot.observations)}"
        )
        for observation in attempt.snapshot.observations:
            _require(
                bool(observation.raw_name_ocr.strip()),
                f"{floor.value} slot {observation.ordinal}: raw_name_ocr пуст.",
            )
            _require(
                observation.identity_status is IdentityStatus.MATCHED,
                f"{floor.value} slot {observation.ordinal}: identity="
                f"{observation.identity_status.value}",
            )
            _require(
                observation.canonical_identity is not None,
                f"{floor.value} slot {observation.ordinal}: canonical identity отсутствует.",
            )
            _require(
                Decimal(0) <= observation.morale <= Decimal(150),
                f"{floor.value} slot {observation.ordinal}: morale вне domain range.",
            )
            _require(
                Decimal(0) <= observation.recovery_per_hour <= Decimal(1500),
                f"{floor.value} slot {observation.ordinal}: recovery вне domain range.",
            )
            _require(
                observation.floor is floor,
                f"{floor.value} slot {observation.ordinal}: observation другого этажа.",
            )


def _load_fleet_state(
    context: RuntimeDormMoraleContext,
    config_name: str,
    selection: FleetSelection,
):
    with context.uow_factory() as uow:
        instance_id = resolve_runtime_instance(uow, config_name)
        formations = uow.fleet_state.latest(instance_id, selection)
    if not formations:
        raise SmokePreflightError(
            "Для текущего профиля нет сохранённого Fleet State. "
            "Сначала должен существовать реальный Formation scan."
        )
    incomplete = tuple(
        observation.fleet_index
        for observation in formations
        if not observation.snapshot.complete
    )
    if incomplete:
        raise SmokePreflightError(
            "Последний Fleet State неполный для флотов: "
            + ", ".join(str(index) for index in incomplete)
        )
    return instance_id, formations


def _verify_persistence(
    context: RuntimeDormMoraleContext,
    config_name: str,
    selection: FleetSelection,
    scan: DormMoraleScanResult,
    reconciliation: MoraleReconciliationResult,
    log: SmokeLog,
) -> None:
    with context.uow_factory() as uow:
        instance_id = resolve_runtime_instance(uow, config_name)
        latest_scan = uow.dorm_morale.latest(instance_id)
        morale_rows = uow.morale.latest(instance_id, selection)

    _require(latest_scan is not None, "Dorm scan не читается обратно из PostgreSQL.")
    _require(latest_scan.id == scan.id, "latest Dorm scan имеет другой id.")
    _require(
        latest_scan.idempotency_key == scan.idempotency_key,
        "Semantic idempotency key не пережил PostgreSQL round-trip.",
    )
    _require(
        latest_scan.observations == scan.observations,
        "Dorm observations изменились после PostgreSQL round-trip.",
    )

    persisted = tuple(row for row in morale_rows if row.dorm_scan_id == scan.id)
    expected_count = (
        reconciliation.exact_observations
        + reconciliation.outside_dorm_observations
    )
    _require(
        len(persisted) == expected_count,
        "Количество перечитанных morale rows не совпадает с reconciliation summary.",
    )
    slot_keys = tuple((row.fleet_index, row.side, row.position) for row in persisted)
    _require(
        len(slot_keys) == len(set(slot_keys)),
        "Один physical Fleet slot получил несколько morale rows текущего scan.",
    )

    outside_profile = MoraleRecoveryProfile.outside_dorm_base()
    exact_count = 0
    outside_count = 0
    for row in persisted:
        if row.location in {
            MoraleLocation.DORM_FLOOR_1,
            MoraleLocation.DORM_FLOOR_2,
        }:
            _require(
                row.knowledge is MoraleKnowledge.EXACT and row.baseline is not None,
                "Dorm-matched slot не сохранил exact baseline.",
            )
            _require(
                row.recovery.source.startswith("dorm_ui:"),
                "Dorm-matched slot не сохранил UI recovery provenance.",
            )
            exact_count += 1
        elif row.location is MoraleLocation.OUTSIDE_DORM:
            _require(
                row.knowledge is MoraleKnowledge.UNKNOWN and row.baseline is None,
                "Outside-Dorm slot получил fake morale baseline.",
            )
            _require(
                row.recovery == outside_profile,
                "Outside-Dorm slot не использует базовый recovery profile.",
            )
            outside_count += 1
        else:
            raise SmokeAcceptanceError(
                f"Morale row текущего Dorm scan имеет недопустимую location: "
                f"{row.location.value}"
            )

    _require(
        exact_count == reconciliation.exact_observations,
        "Persisted exact count не совпадает с reconciliation summary.",
    )
    _require(
        outside_count == reconciliation.outside_dorm_observations,
        "Persisted outside-Dorm count не совпадает с reconciliation summary.",
    )
    log.write(
        "PostgreSQL round-trip: PASS; "
        f"scan_id={scan.id}; semantic_key=PASS; morale_rows={len(persisted)}"
    )


def _prepare_runtime(metadata: SmokeMetadata, log: SmokeLog):
    try:
        context = build_runtime_dorm_morale_context(require_ready=True)
        selection = FleetSelection.all()
        instance_id, formations = _load_fleet_state(
            context,
            metadata.config_name,
            selection,
        )
        config = AzurLaneConfig(metadata.config_name)
        device = Device(config)
        controller = DormMoraleController(config, device=device)
    except SmokePreflightError:
        dispose_runtime_storage()
        raise
    except BaseException as error:
        dispose_runtime_storage()
        raise SmokePreflightError(
            f"Не удалось подготовить production runtime: {type(error).__name__}: {error}"
        ) from error

    log.write(
        f"Fleet State: instance={str(instance_id)[:8]}…; "
        f"fleets={','.join(str(item.fleet_index) for item in formations)}"
    )
    return context, selection, device, controller


def run_smoke(metadata: SmokeMetadata, log: SmokeLog) -> None:
    log.write(
        f"Старт smoke: repository={metadata.repository}; branch={metadata.branch}; "
        f"head={metadata.head}; profile={metadata.config_name}"
    )
    context, selection, device, controller = _prepare_runtime(metadata, log)
    primary_error: BaseException | None = None
    cleanup_error: BaseException | None = None

    try:
        device.screenshot()
        _require(
            controller.ui_page_appear(page_main),
            "Smoke должен начинаться с подтверждённого Main menu.",
        )
        log.write("Стартовая страница: Main подтверждена.")

        scan = controller.scan_both_floors(source="acceptance:dorm_morale_stage2")
        _validate_scan(scan, log)
        log.write(
            f"Dorm scan: status={scan.status.value}; observations={len(scan.observations)}"
        )

        reconciliation = context.reconciliation_service.reconcile(
            metadata.config_name,
            selection,
            scan,
        )
        _require(
            reconciliation.complete_scan,
            "Reconciliation не считает Dorm scan полным.",
        )
        _require(
            not reconciliation.stale_fleet_indices,
            "Fleet State стал stale для флотов: "
            + ", ".join(str(index) for index in reconciliation.stale_fleet_indices),
        )
        _require(
            reconciliation.unresolved_observations == 0,
            "Scanner оставил unresolved Dorm observations.",
        )
        log.write(
            "Reconciliation: "
            f"exact={reconciliation.exact_observations}; "
            f"outside={reconciliation.outside_dorm_observations}; "
            f"ambiguous={reconciliation.ambiguous_observations}; "
            f"unresolved={reconciliation.unresolved_observations}; "
            f"unmatched={reconciliation.unmatched_observations}; "
            f"stale={list(reconciliation.stale_fleet_indices)}"
        )
        _verify_persistence(
            context,
            metadata.config_name,
            selection,
            scan,
            reconciliation,
            log,
        )
    except BaseException as error:
        primary_error = error
    finally:
        try:
            controller.ui_ensure(page_main)
            device.screenshot()
            if not controller.ui_page_appear(page_main):
                raise SmokeAcceptanceError(
                    "Cleanup не подтвердил возврат игры в Main menu."
                )
            log.write("Cleanup: Main подтверждён.")
        except BaseException as error:
            cleanup_error = error
            log.write(f"Cleanup: FAIL: {type(error).__name__}: {error}")
        finally:
            dispose_runtime_storage()

    if primary_error is not None:
        if cleanup_error is not None:
            log.write("Основная ошибка сохранена; cleanup error приведён отдельно выше.")
        raise primary_error
    if cleanup_error is not None:
        raise cleanup_error

    log.write("PASS")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Реальный smoke Stage 2 Dorm morale reconciliation.",
    )
    parser.add_argument("--log-path", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--config-name", default=DEFAULT_CONFIG_NAME)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    log = SmokeLog(Path(args.log_path))
    metadata = SmokeMetadata(
        repository=args.repository,
        branch=args.branch,
        head=args.head,
        config_name=args.config_name,
    )
    try:
        run_smoke(metadata, log)
    except SmokePreflightError as error:
        log.write(f"FAIL preflight: {type(error).__name__}: {error}")
        return 2
    except BaseException as error:
        log.write(f"FAIL acceptance: {type(error).__name__}: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
