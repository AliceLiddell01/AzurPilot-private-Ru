"""Interactive Windows/MuMu acceptance for EN commission OCR.

The runner navigates to the Commission page, scans the Daily and Urgent tabs,
saves full-row and name crops, validates the parsed Commission objects and then
requires the operator to visually confirm every recognized row. It never
claims rewards and never starts a commission.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from dev_tools.stage8a_device_acceptance import (
    AcceptanceFailure,
    _git_head_sha,
    _safe_text,
    _validate_profile_name,
)
from module.commission.commission import COMMISSION_SWITCH, RewardCommission
from module.ui.page import page_commission

DEFAULT_REPORT = Path("artifacts/ocr/commission-acceptance.json")
DEFAULT_ARTIFACT_DIR = Path("artifacts/ocr/commission-acceptance")
_GIBBERISH_RE = re.compile(r"^[A-Z0-9:]+$")


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_crop(image: np.ndarray, area) -> np.ndarray:
    x1, y1, x2, y2 = [int(value) for value in area]
    x1 = max(0, min(x1, image.shape[1]))
    x2 = max(0, min(x2, image.shape[1]))
    y1 = max(0, min(y1, image.shape[0]))
    y2 = max(0, min(y2, image.shape[0]))
    if x2 <= x1 or y2 <= y1:
        raise AcceptanceFailure(f"Недопустимая область OCR-кропа: {area}")
    return np.ascontiguousarray(image[y1:y2, x1:x2])


def _write_png(path: Path, image: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), image):
        raise AcceptanceFailure(f"Не удалось сохранить PNG: {path}")


def _capture_screen(runner: RewardCommission, path: Path) -> str:
    runner.device.screenshot()
    _write_png(path, np.asarray(runner.device.image))
    return path.as_posix()


def _commission_row(comm, mode: str, row_id: int, artifact_dir: Path) -> dict[str, Any]:
    image = np.asarray(comm.image)
    row_path = artifact_dir / f"{row_id:02d}-{mode}-row.png"
    name_path = artifact_dir / f"{row_id:02d}-{mode}-name.png"
    _write_png(row_path, _safe_crop(image, comm.area))
    _write_png(name_path, _safe_crop(image, comm.button.area))

    duration_seconds = int(comm.duration.total_seconds())
    expire_seconds = int(comm.expire.total_seconds()) if comm.expire else 0
    name = str(comm.name).strip()
    genre = str(comm.genre).strip()
    suspicious_gibberish = bool(
        _GIBBERISH_RE.fullmatch(name)
        and " " not in name
        and len(name) >= 6
        and not genre
    )
    return {
        "id": row_id,
        "mode": mode,
        "name": name,
        "genre": genre,
        "valid": bool(comm.valid),
        "status": str(comm.status),
        "duration_seconds": duration_seconds,
        "duration": str(comm.duration),
        "expire_seconds": expire_seconds,
        "expire": str(comm.expire),
        "suffix_hash": str(comm.suffix_hash),
        "row_area": list(comm.area),
        "name_area": list(comm.button.area),
        "row_crop": row_path.as_posix(),
        "name_crop": name_path.as_posix(),
        "row_crop_sha256": _sha256(row_path),
        "name_crop_sha256": _sha256(name_path),
        "suspicious_gibberish": suspicious_gibberish,
    }


def _is_blank_commission(comm) -> bool:
    """Return whether a detector result is the single empty-tab sentinel.

    The EN empty Urgent page can expose one decorative separator to
    ``lines_detect``. That creates one Commission object with no name, no genre,
    no duration and no suffix. This predicate is deliberately strict so a real
    card with even one recognized field remains a blocking OCR failure.
    """

    duration_seconds = int(comm.duration.total_seconds())
    return bool(
        not comm.valid
        and not str(comm.name).strip()
        and not str(comm.genre).strip()
        and duration_seconds == 0
        and not str(comm.suffix_hash).strip()
    )


def _is_single_blank_scan(commissions: list[Any]) -> bool:
    return len(commissions) == 1 and _is_blank_commission(commissions[0])


def evaluate_rows(rows: list[dict[str, Any]]) -> list[str]:
    """Return fail-closed automatic acceptance findings."""

    findings: list[str] = []
    if len(rows) < 2:
        findings.append("На обеих вкладках найдено меньше двух комиссий суммарно.")
    for row in rows:
        prefix = f"#{row['id']} {row['mode']}"
        if not row["valid"]:
            findings.append(f"{prefix}: Commission.valid=False.")
        if not row["genre"]:
            findings.append(f"{prefix}: тип комиссии не классифицирован.")
        if row["duration_seconds"] <= 0:
            findings.append(f"{prefix}: длительность не распознана.")
        if not any(char.isalpha() for char in row["name"]):
            findings.append(f"{prefix}: в названии нет букв.")
        if row["suspicious_gibberish"]:
            findings.append(f"{prefix}: название похоже на OCR-мусор: {row['name']!r}.")
    return findings


def _ensure_mode_active(runner: RewardCommission, mode: str) -> None:
    """Ensure the requested Commission tab is the observed active state.

    ``Switch.set`` returns whether it clicked, not whether the requested state is
    active. An already-active tab therefore returns ``False`` even though the
    operation succeeded. Acceptance validates the observed selector state
    instead of interpreting that change flag as success/failure.
    """

    runner._commission_ensure_mode(mode)
    current = COMMISSION_SWITCH.get(main=runner)
    if current != mode:
        runner.device.screenshot()
        current = COMMISSION_SWITCH.get(main=runner)
    if current != mode:
        raise AcceptanceFailure(
            "Не удалось подтвердить вкладку комиссий "
            f"{mode}; текущее состояние: {current}."
        )


def _scan_mode(runner: RewardCommission, mode: str):
    _ensure_mode_active(runner, mode)
    runner._commission_swipe_to_top()
    rows = runner._commission_scan_list()
    if mode == "urgent":
        rows.call("convert_to_night")
    return list(rows)


def _scan_urgent_with_retry(
    runner: RewardCommission,
    artifact_dir: Path,
) -> tuple[list[Any], bool, dict[str, Any]]:
    """Scan lazy-loaded Urgent twice before offering an empty-tab confirmation."""

    first = _scan_mode(runner, "urgent")
    first_screen = _capture_screen(runner, artifact_dir / "urgent-page-first.png")
    evidence: dict[str, Any] = {
        "first_raw_count": len(first),
        "first_blank_sentinel": _is_single_blank_scan(first),
        "first_screen": first_screen,
        "first_screen_sha256": _sha256(Path(first_screen)),
    }
    if not _is_single_blank_scan(first):
        return first, False, evidence

    # Force the same Daily -> Urgent refresh used by the production scanner.
    _ensure_mode_active(runner, "daily")
    _ensure_mode_active(runner, "urgent")
    second = _scan_mode(runner, "urgent")
    second_screen = _capture_screen(runner, artifact_dir / "urgent-page-retry.png")
    evidence.update(
        {
            "second_raw_count": len(second),
            "second_blank_sentinel": _is_single_blank_scan(second),
            "second_screen": second_screen,
            "second_screen_sha256": _sha256(Path(second_screen)),
        }
    )
    if _is_single_blank_scan(second):
        return [], True, evidence
    return second, False, evidence


def _confirm_empty_urgent(
    empty_urgent: bool,
    evidence: dict[str, Any],
    args: argparse.Namespace,
) -> list[str]:
    if not empty_urgent:
        return []

    screen = evidence.get("second_screen") or evidence.get("first_screen")
    print("\nUrgent дал один полностью пустой detector-объект после двух сканов.")
    print(f"Проверьте, что на вкладке Urgent действительно нет карточек: {screen}")
    if args.non_interactive:
        raw = args.confirmed_empty_modes or ""
    else:
        raw = input(
            "Введите EMPTY URGENT только если вкладка визуально пуста; "
            "любое другое значение завершит тест с FAIL: "
        ).strip()
    if raw.upper() != "EMPTY URGENT":
        raise AcceptanceFailure(
            "Пустая вкладка Urgent не подтверждена точной командой EMPTY URGENT."
        )
    return ["urgent"]


def _confirm_rows(rows: list[dict[str, Any]], args: argparse.Namespace) -> list[int]:
    print("\nCommission OCR — результат автоматического сканирования:")
    for row in rows:
        print(
            f"  {row['id']:>2}. {row['mode']:<6} | {row['name']} | "
            f"{row['genre']} | {row['duration']} | {row['status']}"
        )
        print(f"      crop: {row['name_crop']}")

    expected = [row["id"] for row in rows]
    if args.non_interactive:
        raw = args.confirmed_ids or ""
    else:
        raw = input(
            "\nСверьте КАЖДОЕ название с MuMu или PNG-кропом. "
            "Введите MATCH ALL либо MATCH 1,2,3,...: "
        ).strip()

    if raw.upper() == "MATCH ALL":
        return expected
    if not raw.upper().startswith("MATCH "):
        raise AcceptanceFailure("Не получено ручное подтверждение MATCH.")
    try:
        confirmed = [
            int(part.strip())
            for part in raw[6:].split(",")
            if part.strip()
        ]
    except ValueError as exc:
        raise AcceptanceFailure("После MATCH должен идти список целых ID.") from exc
    confirmed = list(dict.fromkeys(confirmed))
    if confirmed != expected:
        raise AcceptanceFailure(
            "Ручная проверка должна подтвердить все строки по порядку; "
            f"ожидалось {expected}, получено {confirmed}."
        )
    return confirmed


def run_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    _validate_profile_name(args.profile)
    head = _git_head_sha()
    if args.expected_head and head != args.expected_head:
        raise AcceptanceFailure(
            f"Acceptance head mismatch: ожидался {args.expected_head}, получен {head}."
        )

    config_path = Path("config") / f"{args.profile}.json"
    config_before = _sha256(config_path)
    if config_before is None:
        raise AcceptanceFailure(f"Файл профиля не найден: {config_path}")

    print("Commission OCR acceptance plan")
    print(f"Exact head: {head}")
    print(f"Profile: {args.profile}")
    print("Действия: открыть Commission, прочитать Daily/Urgent, сохранить кропы.")
    print("Запрещено: получение наград, запуск комиссий, изменение фильтра/расписания.")
    if not args.non_interactive:
        if input("Введите START для начала: ").strip() != "START":
            raise AcceptanceFailure("Acceptance отменён: не получено точное START.")

    device = args.serial if args.serial else None
    runner = RewardCommission(args.profile, device=device, task="Commission")
    if runner.config.SERVER != "en":
        raise AcceptanceFailure("Commission OCR acceptance поддерживает только EN/Global.")

    args.artifact_dir.mkdir(parents=True, exist_ok=True)

    runner.ui_ensure(page_commission)
    initial_screen = _capture_screen(
        runner,
        args.artifact_dir / "commission-page-initial.png",
    )

    # Urgent is lazy-loaded in production. Warm it before scanning Daily.
    _ensure_mode_active(runner, "urgent")
    urgent_warmup_screen = _capture_screen(
        runner,
        args.artifact_dir / "urgent-page-warmup.png",
    )
    _ensure_mode_active(runner, "daily")

    daily = _scan_mode(runner, "daily")
    daily_final_screen = _capture_screen(
        runner,
        args.artifact_dir / "daily-page-final.png",
    )
    urgent, empty_urgent, urgent_evidence = _scan_urgent_with_retry(
        runner,
        args.artifact_dir,
    )
    confirmed_empty_modes = _confirm_empty_urgent(
        empty_urgent,
        urgent_evidence,
        args,
    )
    _ensure_mode_active(runner, "daily")

    rows: list[dict[str, Any]] = []
    for mode, commissions in (("daily", daily), ("urgent", urgent)):
        for comm in commissions:
            rows.append(_commission_row(comm, mode, len(rows) + 1, args.artifact_dir))

    findings = evaluate_rows(rows)
    if findings:
        raise AcceptanceFailure("; ".join(findings))
    confirmed_ids = _confirm_rows(rows, args)

    config_after = _sha256(config_path)
    if config_before != config_after:
        raise AcceptanceFailure("Acceptance обнаружил изменение постоянного profile config.")

    return {
        "status": "PASS",
        "title": "Commission OCR acceptance: PASS",
        "head_sha": head,
        "profile": args.profile,
        "server": runner.config.SERVER,
        "public_runtime_namespace": "azur_lane",
        "text_recognizer": "english_text/PP-OCRv6",
        "compact_recognizer": str(runner.config.ocr_model_version("azur_lane")),
        "automatic_findings": findings,
        "commission_count": len(rows),
        "daily_count": len(daily),
        "urgent_count": len(urgent),
        "empty_modes": ["urgent"] if empty_urgent else [],
        "user_confirmed_empty_modes": confirmed_empty_modes,
        "urgent_empty_evidence": urgent_evidence,
        "rows": rows,
        "user_confirmed_ids": confirmed_ids,
        "confirmation_method": (
            "non_interactive_MATCH" if args.non_interactive else "interactive_MATCH"
        ),
        "config_unchanged": True,
        "screens": {
            "initial": initial_screen,
            "urgent_warmup": urgent_warmup_screen,
            "daily_final": daily_final_screen,
        },
        "initial_screen_sha256": _sha256(Path(initial_screen)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Реальная Windows/MuMu-приёмка OCR английских комиссий"
    )
    parser.add_argument("--profile", required=True)
    serial_group = parser.add_mutually_exclusive_group(required=True)
    serial_group.add_argument("--serial")
    serial_group.add_argument("--serial-from-config", action="store_true")
    parser.add_argument("--expected-head")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument(
        "--confirmed-ids",
        help="Use 'MATCH ALL' or 'MATCH 1,2,...' with --non-interactive.",
    )
    parser.add_argument(
        "--confirmed-empty-modes",
        help="Use exact 'EMPTY URGENT' with --non-interactive when Urgent is empty.",
    )
    args = parser.parse_args(argv)

    try:
        report = run_acceptance(args)
    except Exception as exc:  # noqa: BLE001 - acceptance always emits a report.
        failure = {
            "status": "FAIL",
            "error": _safe_text(str(exc)),
            "head_sha": _git_head_sha() if Path(".git").exists() else None,
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(failure, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Commission OCR acceptance: FAIL — {failure['error']}", file=sys.stderr)
        return 1

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("Commission OCR acceptance: PASS")
    print(f"Отчёт: {args.report}")
    print(f"Кропы: {args.artifact_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())