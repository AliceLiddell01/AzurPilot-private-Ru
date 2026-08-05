from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import cv2
import numpy as np
import psutil

from tools.acceptance.device import (
    AcceptanceFailure,
    _check_android_boot_completed,
    _detect_package,
    _git_head_sha,
    _load_profile,
    _resolve_adb,
    _resolve_serial,
    _run_adb,
    _safe_text,
    _validate_bgr_image,
    _validate_profile_name,
)
from module.ocr.privacy import (
    OcrDebugOutputError,
    cleanup_debug_directory,
)
from module.ocr.rpc_security import loopback_bind_uri

DEFAULT_REPORT = Path("artifacts/acceptance/ocr.json")
SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9:/-]{1,20}$")
VALUE_PATTERNS = (
    ("counter", re.compile(r"^\d{1,6}/\d{1,6}$")),
    ("duration", re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")),
    ("stage", re.compile(r"^\d{1,2}-\d{1,2}$")),
    ("labeled_numeric", re.compile(r"^[A-Z]{2,8}:\d{1,10}$")),
    ("numeric", re.compile(r"^\d{1,10}$")),
)
PACKAGE_NAMES = (
    "rapidocr",
    "onnxruntime",
    "onnxruntime-windowsml",
    "windowsml",
    "ncnn",
    "numpy",
    "opencv-python",
)
PROVIDER_CACHE_CANDIDATES = (
    ("LOCALAPPDATA", "Microsoft/WindowsML"),
    ("LOCALAPPDATA", "onnxruntime"),
    ("TEMP", "WindowsML"),
)


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


def _environment_fingerprint() -> dict[str, Any]:
    versions: dict[str, str | None] = {}
    for name in PACKAGE_NAMES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "os": platform.platform(),
        "architecture": platform.machine(),
        "packages": versions,
    }


def _registered_provider_evidence() -> dict[str, Any]:
    try:
        import onnxruntime as ort
    except Exception as exc:  # noqa: BLE001 - acceptance records optional runtime state.
        return {"available": [], "devices": [], "error": _safe_text(str(exc))}
    try:
        available = list(ort.get_available_providers())
    except Exception as exc:  # noqa: BLE001
        available = []
        available_error = _safe_text(str(exc))
    else:
        available_error = None
    devices: list[dict[str, Any]] = []
    try:
        for device in ort.get_ep_devices():
            hardware = getattr(device, "device", None)
            devices.append(
                {
                    "ep_name": str(getattr(device, "ep_name", "")),
                    "hardware_type": str(getattr(hardware, "type", "")),
                    "vendor": str(getattr(hardware, "vendor", "")),
                    "metadata": dict(getattr(hardware, "metadata", {}) or {}),
                }
            )
    except Exception as exc:  # noqa: BLE001
        device_error = _safe_text(str(exc))
    else:
        device_error = None
    return {
        "available": available,
        "devices": devices,
        "available_error": available_error,
        "device_error": device_error,
    }


def _session_provider_evidence(model: Any) -> dict[str, Any]:
    session = getattr(model, "session", None)
    if session is None:
        nested = getattr(model, "text_rec", None)
        wrapper = getattr(nested, "session", None)
        session = getattr(wrapper, "session", wrapper)
    if session is None:
        return {"providers": [], "options": {}, "session_found": False}
    try:
        providers = list(session.get_providers())
    except Exception as exc:  # noqa: BLE001
        providers = []
        provider_error = _safe_text(str(exc))
    else:
        provider_error = None
    try:
        options = session.get_provider_options()
    except Exception as exc:  # noqa: BLE001
        options = {}
        options_error = _safe_text(str(exc))
    else:
        options_error = None
    return {
        "providers": providers,
        "options": options,
        "session_found": True,
        "provider_error": provider_error,
        "options_error": options_error,
    }


def _requested_provider_order(device: str) -> list[str]:
    from module.ocr.windows_ml import _vendor_execution_provider_names

    if device == "cpu":
        return ["CPUExecutionProvider"]
    order = list(_vendor_execution_provider_names(device))
    if device in {"gpu", "auto"}:
        order.append("DmlExecutionProvider")
    order.append("CPUExecutionProvider")
    return list(dict.fromkeys(order))


def _provider_cache_paths() -> list[Path]:
    result: list[Path] = []
    for variable, suffix in PROVIDER_CACHE_CANDIDATES:
        root = os.environ.get(variable)
        if root:
            result.append(Path(root) / suffix)
    result.extend(
        (
            Path.home() / ".cache" / "onnxruntime",
            Path.home() / ".cache" / "windowsml",
        )
    )
    return result


def _provider_cache_snapshot() -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for root in _provider_cache_paths():
        key = str(root)
        if not root.exists():
            snapshot[key] = {"exists": False, "files": 0, "digest": None}
            continue
        digest = hashlib.sha256()
        count = 0
        try:
            for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
                if not path.is_file():
                    continue
                stat_result = path.stat()
                relative = path.relative_to(root).as_posix()
                digest.update(relative.encode("utf-8", errors="replace"))
                digest.update(str(stat_result.st_size).encode("ascii"))
                digest.update(str(stat_result.st_mtime_ns).encode("ascii"))
                count += 1
                if count >= 5000:
                    break
        except OSError as exc:
            snapshot[key] = {
                "exists": True,
                "files": count,
                "digest": None,
                "error": _safe_text(str(exc)),
            }
        else:
            snapshot[key] = {
                "exists": True,
                "files": count,
                "digest": digest.hexdigest(),
            }
    return snapshot


def _child_process_snapshot() -> dict[int, dict[str, Any]]:
    current = psutil.Process()
    result: dict[int, dict[str, Any]] = {}
    for child in current.children(recursive=True):
        try:
            result[child.pid] = {
                "pid": child.pid,
                "name": child.name(),
                "create_time": child.create_time(),
                "status": child.status(),
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return result


def _run_fixture_benchmark(
    config: Any,
    device: str,
    model_version: str,
) -> dict[str, Any]:
    from module.daemon.ocr_benchmark import OcrBenchmark
    from module.ocr import al_ocr

    config.override(
        Optimization_OcrDevice=device,
        Optimization_OcrModelVersionEnglish=model_version,
        Optimization_OcrWindowsMlVendorEp=False,
    )
    benchmark = OcrBenchmark(config, task="OcrBenchmark")
    with patch.object(al_ocr, "config", config):
        al_ocr.reset_ocr_model()
        try:
            result = benchmark._run_single(
                "azur_lane",
                model_version,
                "sets_num",
                "sets_num",
                ocr_device=device,
                inference_count=20,
            )
        finally:
            al_ocr.release_ocr_models()
    if result is None:
        raise AcceptanceFailure("Не найден bundled EN OCR fixture dataset sets_num.")
    return {
        "model": result["model"],
        "model_version": result["model_version"],
        "model_path": result["model_path"],
        "dictionary_path": result["dictionary_path"],
        "backend": result["backend"],
        "device": result["device"],
        "accuracy": result["accuracy"],
        "correct": result["correct"],
        "total": result["total"],
        "avg_ms": result["avg_ms"],
    }


def _classify_value(value: str) -> str | None:
    for category, pattern in VALUE_PATTERNS:
        if pattern.fullmatch(value):
            return category
    return None


def _crop_hash(image: np.ndarray, box: list[list[float]]) -> str | None:
    points = np.asarray(box, dtype=np.float32)
    x1 = max(0, int(np.floor(points[:, 0].min())))
    y1 = max(0, int(np.floor(points[:, 1].min())))
    x2 = min(image.shape[1], int(np.ceil(points[:, 0].max())) + 1)
    y2 = min(image.shape[0], int(np.ceil(points[:, 1].max())) + 1)
    if x2 <= x1 or y2 <= y1:
        return None
    crop = np.ascontiguousarray(image[y1:y2, x1:x2])
    digest = hashlib.sha256()
    digest.update(str(crop.shape).encode("ascii"))
    digest.update(crop.tobytes(order="C"))
    return digest.hexdigest()


def _recognize_safe_values(
    image: np.ndarray,
    config: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from module.ocr import al_ocr

    config.override(Optimization_OcrWindowsMlVendorEp=False)
    with patch.object(al_ocr, "config", config):
        al_ocr.reset_ocr_model()
        engine = al_ocr.AlOcr(name="azur_lane")
        engine.init()
        try:
            detections = engine.det(image)
            values: list[dict[str, Any]] = []
            ordered = sorted(
                detections,
                key=lambda row: (
                    min(point[1] for point in row[1]),
                    min(point[0] for point in row[1]),
                ),
            )
            for text, box, score in ordered:
                value = str(text).strip()
                if not SAFE_VALUE_RE.fullmatch(value):
                    continue
                category = _classify_value(value)
                if category is None:
                    continue
                values.append(
                    {
                        "id": len(values) + 1,
                        "category": category,
                        "value": value,
                        "score": float(score),
                        "box": box,
                        "crop_sha256": _crop_hash(image, box),
                    }
                )
                if len(values) >= 6:
                    break
            return values, _session_provider_evidence(engine.model)
        finally:
            al_ocr.release_ocr_models()


def _parse_confirmed_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    try:
        values = [int(part.strip()) for part in raw.split(",") if part.strip()]
    except ValueError as exc:
        raise AcceptanceFailure("confirmed-value-ids должен быть списком целых ID.") from exc
    return list(dict.fromkeys(values))


def _confirm_real_values(values: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    if len(values) < 2:
        raise AcceptanceFailure(
            "На выбранном безопасном экране найдено меньше двух проверяемых OCR-значений."
        )
    print("Распознанные безопасные значения для визуального сравнения:")
    for row in values:
        print(
            f"  {row['id']}: {row['category']} = {row['value']} "
            f"(score={row['score']:.4f}, box={row['box']})"
        )
    if args.non_interactive:
        confirmed_ids = _parse_confirmed_ids(args.confirmed_value_ids)
    else:
        confirmation = input(
            "Сравните значения с экраном и введите MATCH 1,2 (минимум два ID): "
        ).strip()
        if not confirmation.startswith("MATCH "):
            raise AcceptanceFailure("Не получено визуальное подтверждение MATCH.")
        confirmed_ids = _parse_confirmed_ids(confirmation[6:])
    if len(confirmed_ids) < 2:
        raise AcceptanceFailure("Нужно подтвердить минимум два OCR-значения.")
    by_id = {row["id"]: row for row in values}
    unknown = [value for value in confirmed_ids if value not in by_id]
    if unknown:
        raise AcceptanceFailure(f"Неизвестные ID подтверждения: {unknown}")
    confirmed = [by_id[value] for value in confirmed_ids]
    categories = {row["category"] for row in confirmed}
    if not categories & {"numeric", "counter", "duration", "stage", "labeled_numeric"}:
        raise AcceptanceFailure("Подтверждение не содержит числового OCR-значения.")
    return confirmed


def _print_plan(profile: str, package: str, details: dict[str, Any], head: str) -> None:
    print("Stage 8B OCR acceptance plan")
    print(f"Exact head: {head}")
    print(f"Profile: {profile}")
    print(f"Server/package: {details['server']} / {package}")
    print(
        "Backend/device/model: "
        f"{details['backend']} / {details['device_preference']} / "
        f"{details['model_version']}"
    )
    print("Provider download/update: запрещено и проверяется снимками cache state.")
    print("Действия: один read-only screenshot, bundled fixture benchmark, OCR in-memory.")
    print(
        "Запрещено: input, battle, purchase, APK install, app-data clear, "
        "config write, wildcard RPC."
    )
    print("Откройте безопасный статический экран EN/Global без chat/profile/UID.")


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
    config, details = _load_ocr_config(args.profile)
    if details["server"] != "en":
        raise AcceptanceFailure("Stage 8B real acceptance выполняется только на EN/Global profile.")

    _print_plan(args.profile, package, details, head)
    if not args.non_interactive:
        confirmation = input("Введите START для начала read-only проверки: ").strip()
        if confirmation != "START":
            raise AcceptanceFailure("Acceptance отменён: не получено точное подтверждение START.")

    config_path = _config_path(args.profile)
    config_hash_before = _sha256(config_path)
    provider_cache_before = _provider_cache_snapshot()
    children_before = _child_process_snapshot()
    environment = _environment_fingerprint()
    registered_provider = _registered_provider_evidence()
    env_names = (
        "AZURPILOT_OCR_DEBUG",
        "AZURPILOT_OCR_DEBUG_DIR",
        "AZURPILOT_OCR_ALLOW_PROVIDER_DOWNLOAD",
    )
    environment_before = {name: os.environ.get(name) for name in env_names}
    temp_root = Path(tempfile.mkdtemp(prefix="azurpilot-ocr-acceptance-"))
    debug_dir = temp_root / "ocr-debug"
    temporary_files_removed = False
    try:
        os.environ["AZURPILOT_OCR_DEBUG"] = "0"
        os.environ["AZURPILOT_OCR_DEBUG_DIR"] = str(debug_dir)
        os.environ["AZURPILOT_OCR_ALLOW_PROVIDER_DOWNLOAD"] = "0"
        screenshot = _run_adb(
            adb,
            serial,
            "exec-out",
            "screencap",
            "-p",
            binary=True,
        )
        if screenshot.returncode != 0:
            raise AcceptanceFailure("ADB screencap завершился ошибкой.")
        image = _decode_png(bytes(screenshot.stdout))

        requested_version = details["model_version"]
        if requested_version == "auto":
            from module.ocr.al_ocr import DEFAULT_ONNX_MODEL_VERSION

            requested_version = DEFAULT_ONNX_MODEL_VERSION["azur_lane"]
        preferred = details["device_preference"]
        candidate_device = "gpu" if preferred == "auto" else preferred
        candidate_fixture = _run_fixture_benchmark(config, candidate_device, requested_version)
        cpu_reference = _run_fixture_benchmark(config, "cpu", requested_version)
        resolved_device = (
            candidate_device
            if candidate_fixture["accuracy"] >= 100.0
            else "cpu"
        )
        config.override(
            Optimization_OcrDevice=resolved_device,
            Optimization_OcrModelVersionEnglish=requested_version,
            Optimization_OcrWindowsMlVendorEp=False,
        )
        values, session_provider = _recognize_safe_values(image, config)
        user_confirmed_values = _confirm_real_values(values, args)

        from module.ocr import al_ocr

        model_path, dictionary_path, _ocr_version = al_ocr.ONNX_MODEL_PARAMS["azur_lane"][requested_version]
        fixture_archive = next(
            (
                Path("module/daemon") / f"sets_num{suffix}"
                for suffix in (".zip", ".tar", ".tar.xz", ".tar.gz")
                if (Path("module/daemon") / f"sets_num{suffix}").is_file()
            ),
            None,
        )
        if fixture_archive is None:
            raise AcceptanceFailure("Bundled fixture archive sets_num не найден.")
    finally:
        cleanup_failure: Exception | None = None
        try:
            cleanup_debug_directory(debug_dir)
        except (OcrDebugOutputError, OSError) as exc:
            cleanup_failure = exc
        try:
            shutil.rmtree(temp_root, ignore_errors=False)
        except OSError as exc:
            if cleanup_failure is None:
                cleanup_failure = exc
        finally:
            temporary_files_removed = not temp_root.exists()
            for name, value in environment_before.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        if cleanup_failure is not None:
            raise AcceptanceFailure(
                f"Не удалось безопасно очистить временные OCR-данные: {cleanup_failure}"
            ) from cleanup_failure

    if not temporary_files_removed:
        raise AcceptanceFailure("Временный acceptance-каталог остался на диске.")
    config_hash_after = _sha256(config_path)
    config_unchanged = config_hash_before == config_hash_after
    if not config_unchanged:
        raise AcceptanceFailure("Acceptance обнаружил изменение постоянного profile config.")

    provider_cache_after = _provider_cache_snapshot()
    provider_download_performed = provider_cache_before != provider_cache_after
    if provider_download_performed:
        raise AcceptanceFailure("Во время acceptance изменилось состояние provider cache.")

    children_after = _child_process_snapshot()
    residual_processes = [
        details
        for pid, details in children_after.items()
        if pid not in children_before
    ]
    if residual_processes:
        raise AcceptanceFailure(
            "После acceptance остались дочерние процессы: "
            + ", ".join(f"{row['pid']}:{row['name']}" for row in residual_processes)
        )

    debug_images_absent_or_opt_in = not debug_dir.exists()
    if not debug_images_absent_or_opt_in:
        raise AcceptanceFailure("После acceptance остался debug image directory.")

    rpc_port = int(getattr(__import__("module.webui.setting", fromlist=["State"]).State.deploy_config, "OcrServerPort", 22268))
    return {
        "status": "PASS",
        "title": "Stage 8B OCR acceptance: PASS",
        "head_sha": head,
        "profile": args.profile,
        "server": details["server"],
        "package": package,
        "environment": environment,
        "backend": details["backend"],
        "device_preference": details["device_preference"],
        "device_resolved": resolved_device,
        "model": "azur_lane",
        "model_version": requested_version,
        "model_path": model_path,
        "dictionary_path": dictionary_path,
        "model_sha256": _sha256(Path(model_path)),
        "dictionary_sha256": _sha256(Path(dictionary_path)),
        "fixture_archive_sha256": _sha256(fixture_archive),
        "provider_requested_order": _requested_provider_order(resolved_device),
        "provider_registered": registered_provider,
        "provider_session": session_provider["providers"],
        "provider_options": session_provider["options"],
        "vendor_ep_enabled_during_acceptance": False,
        "provider_download_policy_disabled": True,
        "provider_download_performed": provider_download_performed,
        "provider_cache_before": provider_cache_before,
        "provider_cache_after": provider_cache_after,
        "fixture_accuracy": candidate_fixture,
        "cpu_reference": cpu_reference,
        "real_values": values,
        "user_confirmed_values": user_confirmed_values,
        "confirmation_method": (
            "non_interactive_confirmed_ids" if args.non_interactive else "interactive_MATCH"
        ),
        "config_unchanged": config_unchanged,
        "temporary_files_removed": temporary_files_removed,
        "debug_images_absent_or_opt_in": debug_images_absent_or_opt_in,
        "rpc_bind": loopback_bind_uri(rpc_port),
        "residual_processes": residual_processes,
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
    parser.add_argument(
        "--confirmed-value-ids",
        help="Comma-separated IDs visually confirmed on screen; required with --non-interactive.",
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
        print(f"Stage 8B OCR acceptance: FAIL — {failure['error']}", file=sys.stderr)
        return 1
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("Stage 8B OCR acceptance: PASS")
    print("Визуально подтверждённые значения сохранены в user_confirmed_values.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
