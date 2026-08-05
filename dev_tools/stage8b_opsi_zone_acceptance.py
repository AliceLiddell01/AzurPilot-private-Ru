"""Interactive Windows/MuMu acceptance for EN Operation Siren zone OCR.

The runner safely reaches the local Operation Siren map, captures repeated
MAP_NAME observations, verifies the real OCR routing and zone lookup chain,
and asks the operator to confirm the mapped zone. It never checks out a
mission, starts auto-search, calls ``os_init``/``zone_init`` or uses the globe
fallback that the production timeout path would normally trigger.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
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
from module.base.utils import extract_letters
from module.exception import ScriptError
from module.ocr.ocr import Ocr
from module.os.assets import MAP_NAME
from module.os.operation_siren import OperationSiren
from module.ui.page import page_os

DEFAULT_REPORT = Path("artifacts/ocr/opsi-zone-acceptance.json")
DEFAULT_ARTIFACT_DIR = Path("artifacts/ocr/opsi-zone-acceptance")
_OLD_GIBBERISH_FINGERPRINT = "ma0656s6s6fsa162868"
_SPACE_RE = re.compile(r"\s+")


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_default(value: Any):
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, Path):
        return value.as_posix()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _write_json_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )


def _prepare_artifact_dir(path: Path) -> list[str]:
    path.mkdir(parents=True, exist_ok=True)
    removed: list[str] = []
    for pattern in ("*.png", "*.tmp"):
        for candidate in sorted(path.glob(pattern)):
            if candidate.is_file():
                candidate.unlink()
                removed.append(candidate.name)
    return removed


def _png_for_cv2(image: np.ndarray) -> np.ndarray:
    """Convert in-memory RGB/RGBA evidence to OpenCV's BGR/BGRA order."""

    array = np.clip(np.asarray(image), 0, 255).astype(np.uint8, copy=False)
    if array.ndim == 3 and array.shape[2] == 3:
        array = cv2.cvtColor(array, cv2.COLOR_RGB2BGR)
    elif array.ndim == 3 and array.shape[2] == 4:
        array = cv2.cvtColor(array, cv2.COLOR_RGBA2BGRA)
    return np.ascontiguousarray(array)


def _write_png(path: Path, image: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), _png_for_cv2(image)):
        raise AcceptanceFailure(f"Не удалось сохранить PNG: {path}")


def _safe_crop(image: np.ndarray, area) -> np.ndarray:
    x1, y1, x2, y2 = [int(value) for value in area]
    x1 = max(0, min(x1, image.shape[1]))
    x2 = max(0, min(x2, image.shape[1]))
    y1 = max(0, min(y1, image.shape[0]))
    y2 = max(0, min(y2, image.shape[0]))
    if x2 <= x1 or y2 <= y1:
        raise AcceptanceFailure(f"Недопустимая область MAP_NAME: {area}")
    return np.ascontiguousarray(image[y1:y2, x1:x2])


def _compact_text(value: str) -> str:
    return _SPACE_RE.sub("", str(value or "")).lower()


def _sample_is_suspicious(sample: dict[str, Any]) -> bool:
    raw_compact = _compact_text(sample.get("raw_text", ""))
    return bool(
        not sample.get("raw_text")
        or not sample.get("processed_name")
        or sample.get("zone_id") is None
        or raw_compact == _OLD_GIBBERISH_FINGERPRINT
        or sum(char.isdigit() for char in raw_compact) >= 3
    )


def evaluate_samples(samples: list[dict[str, Any]]) -> list[str]:
    """Return fail-closed findings for repeated local-map observations."""

    findings: list[str] = []
    if len(samples) < 3:
        findings.append("Для проверки стабильности требуется не менее трёх снимков.")

    for sample in samples:
        prefix = f"sample #{sample.get('id', '?')}"
        if not sample.get("raw_text"):
            findings.append(f"{prefix}: OCR вернул пустую строку.")
        if not sample.get("processed_name"):
            findings.append(f"{prefix}: нормализованное имя зоны пусто.")
        if sample.get("zone_id") is None:
            findings.append(
                f"{prefix}: имя {sample.get('processed_name')!r} не сопоставлено с Zone."
            )
        if _compact_text(sample.get("raw_text", "")) == _OLD_GIBBERISH_FINGERPRINT:
            findings.append(f"{prefix}: воспроизведён старый OCR-мусор.")
        digit_count = sum(
            char.isdigit() for char in _compact_text(sample.get("raw_text", ""))
        )
        if digit_count >= 3:
            findings.append(
                f"{prefix}: в названии зоны подозрительно много цифр ({digit_count})."
            )

    zone_ids = {
        int(sample["zone_id"])
        for sample in samples
        if sample.get("zone_id") is not None
    }
    if len(zone_ids) > 1:
        findings.append(
            "Последовательные снимки сопоставлены с разными зонами: "
            + ", ".join(str(zone_id) for zone_id in sorted(zone_ids))
            + "."
        )
    if samples and not zone_ids:
        findings.append("Ни один снимок не сопоставлен с объектом Zone.")
    return findings


def _ensure_local_os_map(runner: OperationSiren) -> None:
    """Reach the local map without running Operation Siren automation."""

    runner.device.screenshot()
    if runner.is_in_globe():
        runner.os_globe_goto_map()
    elif not runner.is_in_map():
        runner.ui_ensure(page_os)
        runner.device.screenshot()
        if runner.is_in_globe():
            runner.os_globe_goto_map()

    runner.device.screenshot()
    if not runner.is_in_map():
        raise AcceptanceFailure(
            "Не удалось безопасно подтвердить локальную карту Operation Siren."
        )


def _capture_sample(
    runner: OperationSiren,
    ocr: Ocr,
    sample_id: int,
    artifact_dir: Path,
) -> dict[str, Any]:
    runner.device.screenshot()
    image = np.asarray(runner.device.image)

    screen_path = artifact_dir / f"sample-{sample_id:02d}-screen.png"
    crop_path = artifact_dir / f"sample-{sample_id:02d}-map-name.png"
    letters_path = artifact_dir / f"sample-{sample_id:02d}-map-name-letters.png"
    _write_png(screen_path, image)

    map_name_crop = _safe_crop(image, MAP_NAME.area)
    _write_png(crop_path, map_name_crop)
    letters = extract_letters(
        map_name_crop,
        letter=(206, 223, 247),
        threshold=96,
    )
    _write_png(letters_path, letters)

    started = time.perf_counter()
    raw_text = str(ocr.ocr(image)).strip()
    raw_elapsed = time.perf_counter() - started

    started = time.perf_counter()
    processed_name = str(runner.get_zone_name()).strip()
    production_elapsed = time.perf_counter() - started

    zone = None
    zone_error = None
    try:
        zone = runner.name_to_zone(processed_name)
    except ScriptError as exc:
        zone_error = _safe_text(str(exc))

    sample = {
        "id": int(sample_id),
        "raw_text": raw_text,
        "processed_name": processed_name,
        "zone_id": int(zone.zone_id) if zone is not None else None,
        "zone_name": str(zone.en) if zone is not None else None,
        "zone_repr": str(zone) if zone is not None else None,
        "zone_error": zone_error,
        "raw_ocr_seconds": round(float(raw_elapsed), 6),
        "production_ocr_seconds": round(float(production_elapsed), 6),
        "screen": screen_path.as_posix(),
        "screen_sha256": _sha256(screen_path),
        "map_name_crop": crop_path.as_posix(),
        "map_name_crop_sha256": _sha256(crop_path),
        "map_name_letters": letters_path.as_posix(),
        "map_name_letters_sha256": _sha256(letters_path),
    }
    sample["suspicious"] = _sample_is_suspicious(sample)
    return sample


def _confirm_zone(
    zone_id: int,
    zone_name: str,
    args: argparse.Namespace,
) -> str:
    expected = f"MATCH ZONE {zone_id}"
    print(f"\nСопоставленная зона: [{zone_id}|{zone_name}]")
    if args.non_interactive:
        raw = f"MATCH ZONE {args.confirmed_zone_id}"
    else:
        raw = input(
            "Сверьте название с экраном MuMu или PNG. "
            f"Введите {expected}: "
        ).strip()
    if raw.upper() != expected:
        raise AcceptanceFailure(
            f"Зона не подтверждена точной командой {expected}."
        )
    return expected


def run_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    _validate_profile_name(args.profile)
    head = _git_head_sha()
    if args.expected_head and head != args.expected_head:
        raise AcceptanceFailure(
            f"Acceptance head mismatch: ожидался {args.expected_head}, получен {head}."
        )
    if args.samples < 3:
        raise AcceptanceFailure("--samples должен быть не меньше 3.")
    if args.sample_interval < 0:
        raise AcceptanceFailure("--sample-interval не может быть отрицательным.")

    config_path = Path("config") / f"{args.profile}.json"
    config_before = _sha256(config_path)
    if config_before is None:
        raise AcceptanceFailure(f"Файл профиля не найден: {config_path}")

    print("Operation Siren zone OCR acceptance plan")
    print(f"Exact head: {head}")
    print(f"Profile: {args.profile}")
    print("Действия: открыть локальную карту OS и прочитать MAP_NAME серией снимков.")
    print("Запрещено: checkout миссии, os_init/zone_init, auto-search и globe fallback.")
    if (
        not args.non_interactive
        and input("Введите START для начала: ").strip() != "START"
    ):
        raise AcceptanceFailure("Acceptance отменён: не получено точное START.")

    device = args.serial if args.serial else None
    runner = OperationSiren(args.profile, device=device, task="OpsiDaily")
    if runner.config.SERVER != "en":
        raise AcceptanceFailure(
            "Operation Siren zone OCR acceptance поддерживает только EN/Global."
        )

    removed_stale_artifacts = _prepare_artifact_dir(args.artifact_dir)
    _ensure_local_os_map(runner)

    ready_screen = args.artifact_dir / "local-map-ready.png"
    _write_png(ready_screen, np.asarray(runner.device.image))

    ocr = Ocr(
        MAP_NAME,
        lang="azur_lane",
        letter=(206, 223, 247),
        threshold=96,
        name="OCR_OS_MAP_NAME",
    )
    selected_model = ocr.cnocr
    model_type = type(selected_model).__name__
    model_name = str(getattr(selected_model, "name", ""))
    if model_type != "GeneralEnglishOcr" or model_name != "english_text":
        raise AcceptanceFailure(
            "OCR_OS_MAP_NAME направлен не в general English recognizer: "
            f"type={model_type}, name={model_name!r}."
        )

    # Model warm-up is recorded separately and excluded from stability samples.
    warmup_started = time.perf_counter()
    warmup_raw = str(ocr.ocr(np.asarray(runner.device.image))).strip()
    warmup_seconds = time.perf_counter() - warmup_started

    samples: list[dict[str, Any]] = []
    for sample_id in range(1, args.samples + 1):
        samples.append(_capture_sample(runner, ocr, sample_id, args.artifact_dir))
        if sample_id != args.samples and args.sample_interval:
            time.sleep(args.sample_interval)

    findings = evaluate_samples(samples)
    if findings:
        raise AcceptanceFailure("; ".join(findings))

    zone_ids = {int(sample["zone_id"]) for sample in samples}
    zone_id = zone_ids.pop()
    zone_name = str(samples[0]["zone_name"])

    # Repeat the exact production OCR -> normalization -> Zone lookup chain,
    # but intentionally skip get_current_zone() because it also changes runtime
    # map-detection configuration after a successful lookup.
    final_processed_name = str(runner.get_zone_name()).strip()
    production_zone = runner.name_to_zone(final_processed_name)
    if int(production_zone.zone_id) != zone_id:
        raise AcceptanceFailure(
            "Финальная production-цепочка не согласована с серией снимков: "
            f"series={zone_id}, production={production_zone.zone_id}."
        )

    confirmation = _confirm_zone(zone_id, zone_name, args)

    config_after = _sha256(config_path)
    if config_before != config_after:
        raise AcceptanceFailure(
            "Acceptance обнаружил изменение постоянного profile config."
        )

    return {
        "status": "PASS",
        "title": "Operation Siren zone OCR acceptance: PASS",
        "head_sha": head,
        "profile": args.profile,
        "server": runner.config.SERVER,
        "public_runtime_namespace": "azur_lane",
        "selected_model_type": model_type,
        "selected_model_name": model_name,
        "warmup_raw_text": warmup_raw,
        "warmup_seconds": round(float(warmup_seconds), 6),
        "sample_count": len(samples),
        "sample_interval_seconds": float(args.sample_interval),
        "automatic_findings": findings,
        "stable_zone_id": zone_id,
        "stable_zone_name": zone_name,
        "final_processed_name": final_processed_name,
        "production_zone_id": int(production_zone.zone_id),
        "production_zone_name": str(production_zone.en),
        "samples": samples,
        "manual_confirmation": confirmation,
        "config_unchanged": True,
        "removed_stale_artifacts": removed_stale_artifacts,
        "local_map_ready_screen": ready_screen.as_posix(),
        "local_map_ready_screen_sha256": _sha256(ready_screen),
        "map_name_area": [int(value) for value in MAP_NAME.area],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Реальная Windows/MuMu-приёмка OCR имени зоны Operation Siren"
    )
    parser.add_argument("--profile", required=True)
    serial_group = parser.add_mutually_exclusive_group(required=True)
    serial_group.add_argument("--serial")
    serial_group.add_argument("--serial-from-config", action="store_true")
    parser.add_argument("--expected-head")
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--sample-interval", type=float, default=0.2)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--confirmed-zone-id", type=int)
    args = parser.parse_args(argv)

    if args.non_interactive and args.confirmed_zone_id is None:
        parser.error("--non-interactive требует --confirmed-zone-id")

    try:
        report = run_acceptance(args)
        _write_json_report(args.report, report)
    except Exception as exc:  # noqa: BLE001 - acceptance always emits a report.
        failure = {
            "status": "FAIL",
            "error": _safe_text(str(exc)),
            "head_sha": _git_head_sha() if Path(".git").exists() else None,
        }
        try:
            _write_json_report(args.report, failure)
        except Exception as report_exc:  # noqa: BLE001
            print(
                "Operation Siren zone OCR acceptance: FAIL — "
                f"не удалось записать отчёт: {_safe_text(str(report_exc))}",
                file=sys.stderr,
            )
        print(
            f"Operation Siren zone OCR acceptance: FAIL — {failure['error']}",
            file=sys.stderr,
        )
        return 1

    print("Operation Siren zone OCR acceptance: PASS")
    print(f"Отчёт: {args.report}")
    print(f"Снимки и кропы: {args.artifact_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
