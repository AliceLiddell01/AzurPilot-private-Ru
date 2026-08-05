"""Interactive Windows/MuMu acceptance for EN commission OCR.

The runner navigates to the Commission page, scans the Daily and Urgent tabs,
saves full-row and name crops, validates the parsed Commission objects and then
requires the operator to visually confirm every recognized row.  It never
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
from module.commission.commission import RewardCommission
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


def _scan_mode(runner: RewardCommission, mode: str):
    if not runner._commission_ensure_mode(mode):
        raise AcceptanceFailure(f"Не удалось переключить вкладку комиссий на {mode}.")
    runner._commission_swipe_to_top()
    rows = runner._commission_scan_list()
    if mode == "urgent":
        rows.call("convert_to_night")
    return list(rows)


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
    runner.device.screenshot()
    initial_screen = args.artifact_dir / "commission-page-initial.png"
    _write_png(initial_screen, np.asarray(runner.device.image))

    daily = _scan_mode(runner, "daily")
    urgent = _scan_mode(runner, "urgent")
    runner._commission_ensure_mode("daily")

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
        "rows": rows,
        "user_confirmed_ids": confirmed_ids,
        "confirmation_method": (
            "non_interactive_MATCH" if args.non_interactive else "interactive_MATCH"
        ),
        "config_unchanged": True,
        "initial_screen": initial_screen.as_posix(),
        "initial_screen_sha256": _sha256(initial_screen),
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
