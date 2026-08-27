"""Ручная сквозная проверка Stage 2 Dorm morale на реальном устройстве."""

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

from module.application.fleet_state import FleetScanBatchResult, FleetScanRunStatus
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
from module.formation.navigation import FormationFleetController
from module.persistence.runtime import (
    RuntimeDormMoraleContext,
    build_runtime_dorm_morale_context,
    build_runtime_fleet_state_context,
    dispose_runtime_storage,
)
from module.ui.page import page_main


class SmokePreflightError(RuntimeError):
    """Локальное окружение не готово к реальному проверочному запуску."""


class SmokeAcceptanceError(RuntimeError):
    """Production-путь нарушил контракт приёмочной проверки."""


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


def _validate_fleet_scan(batch: FleetScanBatchResult, log: SmokeLog) -> None:
    _require(
        batch.status is FleetScanRunStatus.SUCCEEDED,
        "Скан состояния флотов завершился неуспешно: "
        f"status={batch.status.value}, failed_fleet={batch.failed_fleet_index}, "
        f"error={batch.failure_code}",
    )
    _require(
        tuple(item.fleet_index for item in batch.observations)
        == batch.selection.fleet_indices,
        "Скан состояния флотов не вернул все выбранные флоты.",
    )
    incomplete = tuple(
        item.fleet_index for item in batch.observations if not item.snapshot.complete
    )
    _require(
        not incomplete,
        "Скан состояния флотов содержит неполное сопоставление для флотов: "
        + ", ".join(str(index) for index in incomplete),
    )
    log.write(
        "Скан состояния флотов: успешно; "
        f"run_id={batch.run_id}; флоты="
        + ",".join(str(item.fleet_index) for item in batch.observations)
    )


def _verify_fleet_persistence(
    context: RuntimeDormMoraleContext,
    config_name: str,
    batch: FleetScanBatchResult,
    log: SmokeLog,
) -> None:
    with context.uow_factory() as uow:
        instance_id = resolve_runtime_instance(uow, config_name)
        persisted = uow.fleet_state.latest(instance_id, batch.selection)

    _require(
        tuple(item.id for item in persisted)
        == tuple(item.id for item in batch.observations),
        "Свежий Fleet State не читается обратно из PostgreSQL без изменений.",
    )
    _require(
        tuple(item.snapshot for item in persisted)
        == tuple(item.snapshot for item in batch.observations),
        "Состав флотов изменился после обратного чтения из PostgreSQL.",
    )
    log.write(
        "Обратное чтение состояния флотов из PostgreSQL: успешно; "
        f"instance={str(instance_id)[:8]}…"
    )


def _validate_dorm_scan(scan: DormMoraleScanResult, log: SmokeLog) -> None:
    _require(
        scan.status is DormMoraleScanStatus.SUCCEEDED and scan.complete,
        f"Скан Dorm не завершён полностью: status={scan.status.value}",
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
            f"Детектор этажа {floor.value}: состояние подтверждено; "
            f"наблюдений={len(attempt.snapshot.observations)}"
        )
        for observation in attempt.snapshot.observations:
            _require(
                bool(observation.raw_name_ocr.strip()),
                f"{floor.value}, слот {observation.ordinal}: raw_name_ocr пуст.",
            )
            _require(
                observation.identity_status is IdentityStatus.MATCHED,
                f"{floor.value}, слот {observation.ordinal}: идентификация="
                f"{observation.identity_status.value}",
            )
            _require(
                observation.canonical_identity is not None,
                f"{floor.value}, слот {observation.ordinal}: "
                "каноническая идентичность отсутствует.",
            )
            _require(
                Decimal(0) <= observation.morale <= Decimal(150),
                f"{floor.value}, слот {observation.ordinal}: morale вне диапазона.",
            )
            _require(
                Decimal(0) <= observation.recovery_per_hour <= Decimal(1500),
                f"{floor.value}, слот {observation.ordinal}: "
                "скорость восстановления вне диапазона.",
            )
            _require(
                observation.floor is floor,
                f"{floor.value}, слот {observation.ordinal}: наблюдение другого этажа.",
            )


def _verify_dorm_persistence(
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

    _require(latest_scan is not None, "Скан Dorm не читается обратно из PostgreSQL.")
    _require(latest_scan.id == scan.id, "Последний скан Dorm имеет другой id.")
    _require(
        latest_scan.idempotency_key == scan.idempotency_key,
        "Семантический idempotency key не пережил обратное чтение PostgreSQL.",
    )
    _require(
        latest_scan.observations == scan.observations,
        "Наблюдения Dorm изменились после обратного чтения PostgreSQL.",
    )

    persisted = tuple(row for row in morale_rows if row.dorm_scan_id == scan.id)
    expected_count = (
        reconciliation.exact_observations
        + reconciliation.outside_dorm_observations
    )
    _require(
        len(persisted) == expected_count,
        "Количество перечитанных строк morale не совпадает с итогом сопоставления.",
    )
    slot_keys = tuple((row.fleet_index, row.side, row.position) for row in persisted)
    _require(
        len(slot_keys) == len(set(slot_keys)),
        "Один физический слот флота получил несколько строк morale текущего скана.",
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
                "Слот из Dorm не сохранил точное базовое значение morale.",
            )
            _require(
                row.recovery.source.startswith("dorm_ui:"),
                "Слот из Dorm не сохранил происхождение скорости восстановления из UI.",
            )
            exact_count += 1
        elif row.location is MoraleLocation.OUTSIDE_DORM:
            _require(
                row.knowledge is MoraleKnowledge.UNKNOWN and row.baseline is None,
                "Слот вне Dorm получил выдуманное базовое значение morale.",
            )
            _require(
                row.recovery == outside_profile,
                "Слот вне Dorm не использует базовый профиль восстановления.",
            )
            outside_count += 1
        else:
            raise SmokeAcceptanceError(
                "Строка morale текущего скана Dorm имеет недопустимое местоположение: "
                f"{row.location.value}"
            )

    _require(
        exact_count == reconciliation.exact_observations,
        "Сохранённое число точных наблюдений не совпадает с итогом сопоставления.",
    )
    _require(
        outside_count == reconciliation.outside_dorm_observations,
        "Сохранённое число наблюдений вне Dorm не совпадает с итогом сопоставления.",
    )
    log.write(
        "Обратное чтение Dorm morale из PostgreSQL: успешно; "
        f"scan_id={scan.id}; семантический_ключ=успешно; строк_morale={len(persisted)}"
    )


def _prepare_runtime(metadata: SmokeMetadata):
    try:
        dorm_context = build_runtime_dorm_morale_context(require_ready=True)
        selection = FleetSelection.all()
        config = AzurLaneConfig(metadata.config_name)
        device = Device(config)
        formation_controller = FormationFleetController(config, device=device)
        fleet_context = build_runtime_fleet_state_context(
            lambda: formation_controller,
            require_ready=True,
        )
        dorm_controller = DormMoraleController(config, device=device)
    except BaseException as error:
        dispose_runtime_storage()
        raise SmokePreflightError(
            "Не удалось подготовить production runtime: "
            f"{type(error).__name__}: {error}"
        ) from error

    return dorm_context, fleet_context, selection, device, dorm_controller


def _confirm_main(controller: DormMoraleController, message: str) -> None:
    controller.device.screenshot()
    _require(controller.ui_page_appear(page_main), message)


def run_smoke(metadata: SmokeMetadata, log: SmokeLog) -> None:
    log.write(
        f"Старт реальной проверки: репозиторий={metadata.repository}; "
        f"ветка={metadata.branch}; head={metadata.head}; профиль={metadata.config_name}"
    )
    (
        dorm_context,
        fleet_context,
        selection,
        device,
        dorm_controller,
    ) = _prepare_runtime(metadata)
    primary_error: BaseException | None = None
    cleanup_error: BaseException | None = None

    try:
        _confirm_main(
            dorm_controller,
            "Проверка должна начинаться с подтверждённой страницы Main.",
        )
        log.write("Стартовая страница Main подтверждена.")

        fleet_batch = fleet_context.state_service.scan(
            metadata.config_name,
            selection,
            source="acceptance:dorm_morale_stage2_fleet",
        )
        _validate_fleet_scan(fleet_batch, log)
        _verify_fleet_persistence(
            dorm_context,
            metadata.config_name,
            fleet_batch,
            log,
        )

        dorm_controller.ui_ensure(page_main)
        _confirm_main(
            dorm_controller,
            "После скана флотов не удалось подтвердить возврат на Main.",
        )
        log.write("После скана флотов страница Main подтверждена.")

        scan = dorm_controller.scan_both_floors(
            source="acceptance:dorm_morale_stage2"
        )
        _validate_dorm_scan(scan, log)
        log.write(
            f"Скан Dorm: status={scan.status.value}; наблюдений={len(scan.observations)}"
        )

        reconciliation = dorm_context.reconciliation_service.reconcile(
            metadata.config_name,
            selection,
            scan,
        )
        _require(
            reconciliation.complete_scan,
            "Сопоставление не считает скан Dorm полным.",
        )
        _require(
            not reconciliation.stale_fleet_indices,
            "Состояние флотов стало устаревшим для флотов: "
            + ", ".join(str(index) for index in reconciliation.stale_fleet_indices),
        )
        _require(
            reconciliation.unresolved_observations == 0,
            "Сканер оставил неразрешённые наблюдения Dorm.",
        )
        log.write(
            "Сопоставление morale: "
            f"точных={reconciliation.exact_observations}; "
            f"вне_Dorm={reconciliation.outside_dorm_observations}; "
            f"неоднозначных={reconciliation.ambiguous_observations}; "
            f"неразрешённых={reconciliation.unresolved_observations}; "
            f"без_совпадения={reconciliation.unmatched_observations}; "
            f"устаревшие_флоты={list(reconciliation.stale_fleet_indices)}"
        )
        _verify_dorm_persistence(
            dorm_context,
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
            dorm_controller.ui_ensure(page_main)
            device.screenshot()
            if not dorm_controller.ui_page_appear(page_main):
                raise SmokeAcceptanceError(
                    "Очистка не подтвердила возврат игры на страницу Main."
                )
            log.write("Очистка: страница Main подтверждена.")
        except BaseException as error:
            cleanup_error = error
            log.write(f"Очистка: ОШИБКА: {type(error).__name__}: {error}")
        finally:
            dispose_runtime_storage()

    if primary_error is not None:
        if cleanup_error is not None:
            log.write(
                "Основная ошибка сохранена; ошибка очистки приведена отдельно выше."
            )
        raise primary_error
    if cleanup_error is not None:
        raise cleanup_error

    log.write("УСПЕХ")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Реальная проверка Stage 2 Dorm morale reconciliation.",
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
        log.write(f"ОШИБКА подготовки: {type(error).__name__}: {error}")
        return 2
    except BaseException as error:
        log.write(f"ОШИБКА приёмки: {type(error).__name__}: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
