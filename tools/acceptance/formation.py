"""Реальная Windows/MuMu-приёмка Formation Fleet Scanner."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from module.formation.navigation import FormationFleetController
from module.formation.model import FormationFleetSnapshot
from tools.acceptance.device import (
    AcceptanceFailure,
    _git_head_sha,
    _load_profile,
    _resolve_serial,
    _safe_text,
    _validate_profile_name,
)

DEFAULT_REPORT = Path("artifacts/acceptance/formation-fleet.json")


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
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _snapshot_payload(snapshot: FormationFleetSnapshot) -> dict[str, Any]:
    return {
        "fleet_index": snapshot.fleet_index,
        "occupied_count": snapshot.occupied_count,
        "complete": snapshot.complete,
        "catalog_fingerprint": snapshot.catalog_fingerprint,
        "slots": [
            {
                "side": slot.side.value,
                "position": slot.position,
                "occupied": slot.occupied,
                "identity_status": (
                    slot.identity_status.value
                    if slot.identity_status is not None
                    else None
                ),
                "displayed_name": slot.displayed_name,
                "canonical_id": (
                    slot.canonical_identity.key
                    if slot.canonical_identity is not None
                    else None
                ),
                "canonical_name": slot.canonical_name,
            }
            for slot in snapshot.slots
        ],
    }


def _print_snapshot(snapshot: FormationFleetSnapshot) -> None:
    status_labels = {
        "matched": "сопоставлен",
        "ambiguous": "неоднозначно",
        "unresolved": "не распознано",
    }
    print(f"\nFormation Fleet {snapshot.fleet_index} — результат сканирования:")
    for slot in snapshot.slots:
        label = f"{slot.side.value}:{slot.position}"
        if not slot.occupied:
            print(f"  {label:<12} ПУСТО")
            continue
        status_value = slot.identity_status.value if slot.identity_status is not None else "unresolved"
        status = status_labels.get(status_value, "неизвестно")
        displayed_name = slot.displayed_name or "<не распознано>"
        if slot.canonical_name and slot.canonical_name != displayed_name:
            name = f"{displayed_name} -> {slot.canonical_name}"
        else:
            name = slot.canonical_name or displayed_name
        print(f"  {label:<12} {name} [{status}]")


def _confirm_snapshot(snapshot: FormationFleetSnapshot, args: argparse.Namespace) -> str:
    if not snapshot.complete:
        unresolved = [
            f"{slot.side.value}:{slot.position}={slot.displayed_name!r}"
            for slot in snapshot.slots
            if slot.occupied and slot.canonical_identity is None
        ]
        raise AcceptanceFailure(
            "Скан Formation содержит неоднозначные или нераспознанные занятые слоты: "
            + ", ".join(unresolved)
        )

    _print_snapshot(snapshot)
    if args.non_interactive:
        answer = str(args.confirmed_match or "").strip()
    else:
        answer = input(
            "\nСверьте все шесть слотов с открытым Formation Info и введите MATCH: "
        ).strip()
    if answer.upper() != "MATCH":
        raise AcceptanceFailure("Состав флота не подтверждён точной командой MATCH.")
    return "MATCH"


def _exception_text(error: BaseException) -> str:
    return _safe_text(str(error) or type(error).__name__)


def _close_info_without_masking(
    runner: FormationFleetController,
    primary: BaseException | None,
) -> None:
    try:
        runner.device.screenshot()
        if runner.formation_state.info_opened(runner.device.image):
            runner._close_info()
    except BaseException as close_error:  # noqa: BLE001 - не маскируем исходную ошибку.
        if primary is not None:
            primary.add_note(
                "Дополнительно не удалось закрыть Formation Info: "
                + _exception_text(close_error)
            )
            return
        if not isinstance(close_error, Exception):
            raise
        raise AcceptanceFailure(
            "Не удалось восстановить Formation после приёмки: "
            + _exception_text(close_error)
        ) from close_error


def _finalize_acceptance(
    *,
    runner: FormationFleetController | None,
    config_path: Path,
    config_before: str,
    primary: BaseException | None,
) -> None:
    """Безусловно восстановить UI и доказать неизменность profile config."""

    cleanup_error: BaseException | None = None
    if runner is not None:
        try:
            _close_info_without_masking(runner, primary)
        except BaseException as error:  # noqa: BLE001 - проверка config всё равно обязательна.
            cleanup_error = error

    config_error: BaseException | None = None
    try:
        config_after = _sha256(config_path)
    except BaseException as error:  # noqa: BLE001 - финализация не должна маскировать primary.
        if isinstance(error, Exception):
            config_error = AcceptanceFailure(
                "Не удалось проверить неизменность постоянного config профиля: "
                + _exception_text(error)
            )
        else:
            config_error = error
    else:
        if config_before != config_after:
            config_error = AcceptanceFailure(
                "Приёмка Formation изменила постоянный config профиля."
            )

    if primary is not None:
        if cleanup_error is not None:
            primary.add_note(
                "Дополнительно не удалось восстановить Formation: "
                + _exception_text(cleanup_error)
            )
        if config_error is not None:
            primary.add_note(
                "Дополнительно не завершена проверка config: "
                + _exception_text(config_error)
            )
        return

    if cleanup_error is not None:
        if config_error is not None:
            cleanup_error.add_note(
                "Дополнительно не завершена проверка config: "
                + _exception_text(config_error)
            )
        raise cleanup_error

    if config_error is not None:
        raise config_error


def run_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    _validate_profile_name(args.profile)
    head = _git_head_sha()
    if args.expected_head and head != args.expected_head:
        raise AcceptanceFailure(
            f"Приёмка Formation: ожидался head {args.expected_head}, получен {head}."
        )

    config_path = Path("config") / f"{args.profile}.json"
    config_before = _sha256(config_path)
    if config_before is None:
        raise AcceptanceFailure(f"Файл профиля не найден: {config_path}")

    runner: FormationFleetController | None = None
    primary: BaseException | None = None
    snapshot: FormationFleetSnapshot | None = None
    confirmation: str | None = None
    try:
        profile = _load_profile(args.profile)
        serial = _resolve_serial(args, profile)

        print("Приёмка Formation Fleet Scanner")
        print(f"Точный head: {head}")
        print(f"Профиль: {args.profile}")
        print(f"Флот: {args.fleet}")
        print("Действия: открыть Formation, выбрать флот, открыть Info и прочитать шесть слотов.")
        print("Состав флота не изменяется.")
        if not args.non_interactive:
            if input("Введите START для начала: ").strip() != "START":
                raise AcceptanceFailure("Приёмка отменена: не получено точное START.")

        runner = FormationFleetController(args.profile, device=serial)
        if runner.config.SERVER != "en":
            raise AcceptanceFailure("Приёмка Formation поддерживает только EN/Global.")

        snapshot = runner.scan_surface_fleet(args.fleet, close_info=False)
        confirmation = _confirm_snapshot(snapshot, args)
    except BaseException as error:  # noqa: BLE001 - cleanup должен сохранить исходную ошибку.
        primary = error
        raise
    finally:
        _finalize_acceptance(
            runner=runner,
            config_path=config_path,
            config_before=config_before,
            primary=primary,
        )

    if snapshot is None or runner is None:
        raise AcceptanceFailure("Приёмка Formation завершилась без снимка состава флота.")

    return {
        "status": "PASS",
        "title": "Приёмка Formation Fleet Scanner: PASS",
        "head_sha": head,
        "profile": args.profile,
        "server": runner.config.SERVER,
        "confirmation": confirmation,
        "config_unchanged": True,
        "snapshot": _snapshot_payload(snapshot),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Реальная Windows/MuMu-приёмка Formation Fleet Scanner"
    )
    parser.add_argument("--profile", required=True)
    parser.add_argument("--fleet", required=True, type=int, choices=range(1, 7))
    serial_group = parser.add_mutually_exclusive_group(required=True)
    serial_group.add_argument("--serial")
    serial_group.add_argument("--serial-from-config", action="store_true")
    parser.add_argument("--expected-head")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument(
        "--confirmed-match",
        help="Точное значение MATCH для --non-interactive после внешней сверки.",
    )
    args = parser.parse_args(argv)

    try:
        report = run_acceptance(args)
        _write_json_report(args.report, report)
    except Exception as exc:  # noqa: BLE001 - приёмка всегда пишет итоговый отчёт.
        failure = {
            "status": "FAIL",
            "error": _safe_text(str(exc)),
            "head_sha": _git_head_sha() if Path(".git").exists() else None,
        }
        try:
            _write_json_report(args.report, failure)
        except Exception as report_exc:  # noqa: BLE001
            print(
                "Приёмка Formation: FAIL — не удалось записать отчёт: "
                f"{_safe_text(str(report_exc))}",
                file=sys.stderr,
            )
        print(f"Приёмка Formation: FAIL — {failure['error']}", file=sys.stderr)
        return 1

    print("Приёмка Formation Fleet Scanner: PASS")
    print(f"Отчёт: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
