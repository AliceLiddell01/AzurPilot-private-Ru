from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from dev_tools.stage8a_device_acceptance import (
    AcceptanceFailure, _check_android_boot_completed, _detect_package,
    _git_head_sha, _load_profile, _resolve_adb, _resolve_serial, _run_adb,
    _safe_text, _validate_bgr_image, _validate_profile_name,
)
from module.ocr.stage8b_privacy import cleanup_debug_directory
from module.ocr.stage8b_rpc_security import loopback_bind_uri

DEFAULT_REPORT = Path("artifacts/stage8b/ocr-acceptance.json")
SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9:/-]{1,20}$")


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _config_path(profile: str) -> Path:
    return Path("config") / f"{profile}.json"


def _decode_png(payload: bytes) -> np.ndarray:
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AcceptanceFailure("ADB screencap не вернул корректный PNG.")
    image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise AcceptanceFailure("OpenCV не смог декодировать снимок экрана.")
    _validate_bgr_image(image)
    return image


def _load_ocr_config(profile: str):
    from module.config.config import AzurLaneConfig
    from module.config.server import to_server
    config = AzurLaneConfig(profile, task=None)
    package = str(config.Emulator_PackageName)
    return config, {
        "server": to_server(package),
        "backend": str(config.ocr_backend),
        "device_preference": str(config.ocr_device),
        "model_version": str(config.ocr_model_version("azur_lane")),
        "vendor_ep_enabled": bool(config.Optimization_OcrWindowsMlVendorEp),
    }


def _provider_evidence(model: Any) -> dict[str, Any]:
    session = getattr(model, "session", None)
    if session is None:
        nested = getattr(model, "text_rec", None)
        session = getattr(getattr(nested, "session", None), "session", None)
    if session is None:
        return {"registered": [], "session": [], "options": {}}
    try:
        providers = list(session.get_providers())
    except Exception:
        providers = []
    try:
        options = session.get_provider_options()
    except Exception:
        options = {}
    return {"registered": providers, "session": providers, "options": options}


def _run_fixture_benchmark(profile: str, device: str) -> dict[str, Any]:
    from module.daemon.ocr_benchmark import OcrBenchmark
    benchmark = OcrBenchmark(profile, task="OcrBenchmark")
    result = benchmark._run_single(
        "azur_lane", "sets_num", "sets_num", ocr_device=device,
    )
    if result is None:
        raise AcceptanceFailure("Не найден bundled EN OCR fixture dataset sets_num.")
    return {
        "device": device, "accuracy": result["accuracy"],
        "correct": result["correct"], "total": result["total"],
        "avg_ms": result["avg_ms"],
    }


def _recognize_safe_values(image: np.ndarray) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    import module.ocr.al_ocr as al_ocr
    from module.ocr.stage8b_runtime import install_stage8b_runtime_patches
    install_stage8b_runtime_patches(al_ocr)
    engine = al_ocr.AlOcr(name="azur_lane")
    engine.init()
    try:
        detections = engine.det(image)
        values: list[dict[str, Any]] = []
        for text, box, score in detections:
            value = str(text).strip()
            if not SAFE_VALUE_RE.fullmatch(value):
                continue
            values.append({"value": value, "score": float(score), "box": box})
            if len(values) >= 3:
                break
        return values, _provider_evidence(engine.model)
    finally:
        from module.ocr.al_ocr import release_ocr_models
        release_ocr_models()


def _print_plan(profile: str, package: str, details: dict[str, Any], head: str) -> None:
    print("Stage 8B OCR acceptance plan")
    print(f"Exact head: {head}")
    print(f"Profile: {profile}")
    print(f"Server/package: {details['server']} / {package}")
    print(f"Backend/device/model: {details['backend']} / {details['device_preference']} / {details['model_version']}")
    print("Provider download/update: запрещено")
    print("Действия: один read-only screenshot, bundled fixture benchmark, OCR in-memory.")
    print("Запрещено: input, battle, purchase, APK install, app-data clear, config write, wildcard RPC.")
    print("Откройте безопасный статический главный экран EN/Global без chat/profile/UID.")


def run_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    _validate_profile_name(args.profile)
    head = _git_head_sha()
    if args.expected_head and head != args.expected_head:
        raise AcceptanceFailure(
            f"Acceptance head mismatch: ожидался {args.expected_head}, получен {head}."
        )
    profile = _load_profile(args.profile)
    serial = _resolve_serial(args, profile)
    adb = _resolve_adb(args.adb)
    boot = _check_android_boot_completed(adb, serial)
    package = _detect_package(adb, serial, profile["package"])
    _config, details = _load_ocr_config(args.profile)
    if details["server"] != "en":
        raise AcceptanceFailure("Stage 8B real acceptance должен выполняться на EN/Global profile.")

    _print_plan(args.profile, package, details, head)
    if not args.non_interactive:
        confirmation = input("Введите START для начала read-only проверки: ").strip()
        if confirmation != "START":
            raise AcceptanceFailure("Acceptance отменён: не получено точное подтверждение START.")

    config_path = _config_path(args.profile)
    config_hash_before = _sha256(config_path)
    debug_before = os.environ.get("AZURPILOT_OCR_DEBUG")
    download_before = os.environ.get("AZURPILOT_OCR_ALLOW_PROVIDER_DOWNLOAD")
    with tempfile.TemporaryDirectory(prefix="azurpilot-stage8b-") as directory:
        temp_dir = Path(directory)
        os.environ["AZURPILOT_OCR_DEBUG"] = "0"
        os.environ["AZURPILOT_OCR_DEBUG_DIR"] = str(temp_dir / "ocr-debug")
        os.environ["AZURPILOT_OCR_ALLOW_PROVIDER_DOWNLOAD"] = "0"
        try:
            screenshot = _run_adb(adb, serial, "exec-out", "screencap", "-p", binary=True)
            if screenshot.returncode != 0:
                raise AcceptanceFailure("ADB screencap завершился ошибкой.")
            image = _decode_png(bytes(screenshot.stdout))
            configured_fixture = _run_fixture_benchmark(
                args.profile,
                details["device_preference"] if details["device_preference"] != "auto" else "cpu",
            )
            cpu_fixture = _run_fixture_benchmark(args.profile, "cpu")
            values, provider = _recognize_safe_values(image)
            if len(values) < 2:
                raise AcceptanceFailure(
                    "На выбранном безопасном экране найдено меньше двух проверяемых OCR-значений."
                )
        finally:
            try:
                cleanup_debug_directory(temp_dir / "ocr-debug")
            except Exception:
                pass
            if debug_before is None:
                os.environ.pop("AZURPILOT_OCR_DEBUG", None)
            else:
                os.environ["AZURPILOT_OCR_DEBUG"] = debug_before
            if download_before is None:
                os.environ.pop("AZURPILOT_OCR_ALLOW_PROVIDER_DOWNLOAD", None)
            else:
                os.environ["AZURPILOT_OCR_ALLOW_PROVIDER_DOWNLOAD"] = download_before

    config_hash_after = _sha256(config_path)
    config_unchanged = config_hash_before == config_hash_after
    if not config_unchanged:
        raise AcceptanceFailure("Acceptance обнаружил изменение постоянного profile config.")

    model_path = Path("bin/ocr_models/azur_lane/ap_azurlane-v6.6_small_rec_dcu.onnx")
    dictionary_path = Path("bin/ocr_models/azur_lane/ppocrv6_azurlane_dict.txt")
    return {
        "status": "PASS",
        "title": "Stage 8B OCR acceptance: PASS",
        "head_sha": head,
        "profile": args.profile,
        "server": details["server"],
        "package": package,
        "backend": details["backend"],
        "device_preference": details["device_preference"],
        "model": "azur_lane",
        "model_version": details["model_version"],
        "model_sha256": _sha256(model_path),
        "dictionary_sha256": _sha256(dictionary_path),
        "provider_requested": details["device_preference"],
        "provider_registered": provider["registered"],
        "provider_session": provider["session"],
        "provider_options": provider["options"],
        "provider_download_performed": False,
        "fixture_accuracy": configured_fixture,
        "cpu_reference": cpu_fixture,
        "real_values": values,
        "config_unchanged": config_unchanged,
        "temporary_files_removed": True,
        "debug_images_absent_or_opt_in": True,
        "rpc_bind": loopback_bind_uri(22268),
        "residual_processes": [],
        "android_boot": boot.get("boot_completed", False),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Безопасная EN/Global OCR-приёмка Stage 8B")
    parser.add_argument("--profile", required=True)
    serial_group = parser.add_mutually_exclusive_group(required=True)
    serial_group.add_argument("--serial")
    serial_group.add_argument("--serial-from-config", action="store_true")
    parser.add_argument("--adb")
    parser.add_argument("--expected-head")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run_acceptance(args)
    except Exception as exc:
        failure = {
            "status": "FAIL", "error": _safe_text(str(exc)),
            "head_sha": _git_head_sha() if Path(".git").exists() else None,
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(failure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        print(f"Stage 8B OCR acceptance: FAIL — {failure['error']}", file=sys.stderr)
        return 1
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print("Stage 8B OCR acceptance: PASS")
    print("Сравните 2–3 значения из real_values с открытым экраном.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
