from __future__ import annotations

import argparse
import json
import re
import shutil
import struct
import subprocess
import tempfile
import time
import warnings
from pathlib import Path
from typing import Any

DEFAULT_REPORT = Path("artifacts/stage8a/device-acceptance.json")
ADB_CANDIDATES = (
    Path(".venv/Scripts/adb.exe"),
    Path(".venv/bin/adb"),
    Path("bin/adb/adb.exe"),
    Path("/usr/bin/adb"),
)

SERIAL_RE = re.compile(r"^[A-Za-z0-9._:\-\[\]%]+$")
NETWORK_SERIAL_RE = re.compile(
    r"^(?:\[[0-9A-Fa-f:.%]+\]|[A-Za-z0-9._-]+):(?P<port>\d{1,5})$"
)
PROFILE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
PACKAGE_RE = re.compile(r"^[A-Za-z0-9._]+$")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
    r"(?:-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|$)",
    re.S,
)
AUTHORIZATION_RE = re.compile(r"\bAuthorization:\s*(?:Bearer|Basic)\s+\S+", re.I)
CREDENTIAL_URL_RE = re.compile(
    r"\b(?P<scheme>(?:https?|ssh)://)[^/\s:@]+:[^@\s/]+@",
    re.I,
)
GITHUB_TOKEN_RE = re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b")
GENERIC_SECRET_RE = re.compile(
    r"\b(?P<key>password|passwd|token|api[_-]?key|secret)"
    r"(?P<separator>\s*(?:=|:)\s*|\s+)"
    r"(?P<value>[^\s,;]+)",
    re.I,
)
SSH_LOCATION_RE = re.compile(r"\b[^@\s:/]+@[^:\s]+:[^\s]+")
DANGEROUS_HTML_TAG_RE = re.compile(
    r"</?(?:script|iframe|object|embed|svg|style|link|meta|form|input)\b[^>]*>",
    re.I,
)
IPV4_RE = re.compile(
    r"(?<![\w.])(?:"
    r"(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1?\d?\d)"
    r"(?::\d{1,5})?(?![\w.])"
)
LOCALHOST_RE = re.compile(r"\blocalhost(?::\d{1,5})?\b", re.I)
WINDOWS_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:\\[^\r\n\t]+")
UNIX_PATH_RE = re.compile(r"(?<![:/A-Za-z0-9_])/(?:[^/\s]+/)+[^\s,;]*")
MAX_DIAGNOSTIC_CHARS = 16_384
PREVIEW_WAIT_SECONDS = 5.0
RECONNECT_WAIT_SECONDS = 60.0


class AcceptanceFailure(RuntimeError):
    pass


def _resolve_adb(explicit: str | None) -> str:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise AcceptanceFailure(f"ADB не найден по указанному пути: {path}")
        return str(path.resolve())
    for candidate in ADB_CANDIDATES:
        if candidate.is_file():
            return str(candidate.resolve())
    found = shutil.which("adb")
    if found:
        return str(Path(found).resolve())
    raise AcceptanceFailure(
        "ADB не найден. Укажите существующий исполняемый файл через --adb; "
        "runner не загружает platform-tools автоматически."
    )


def _run_adb(
    adb: str,
    serial: str,
    *args: str,
    timeout: float = 20,
    binary: bool = False,
) -> subprocess.CompletedProcess[Any]:
    command = [adb, "-s", serial, *args]
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        timeout=timeout,
        text=not binary,
        encoding=None if binary else "utf-8",
        errors=None if binary else "replace",
    )


def _run_adb_connect(
    adb: str,
    serial: str,
    timeout: float = 30,
) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        [adb, "connect", serial],
        check=False,
        capture_output=True,
        timeout=timeout,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _safe_text(value: str, serial: str = "") -> str:
    result = ANSI_RE.sub("", value)
    result = PRIVATE_KEY_RE.sub("<private-key>", result)
    result = AUTHORIZATION_RE.sub("Authorization: <credential>", result)
    result = CREDENTIAL_URL_RE.sub(r"\g<scheme><credential>@", result)
    result = GITHUB_TOKEN_RE.sub("<token>", result)
    result = GENERIC_SECRET_RE.sub(
        lambda match: f"{match.group('key')}{match.group('separator')}<credential>",
        result,
    )
    result = SSH_LOCATION_RE.sub("<ssh-location>", result)
    result = DANGEROUS_HTML_TAG_RE.sub("<html-redacted>", result)
    if serial:
        result = result.replace(serial, "<serial>")
    home = str(Path.home())
    if home:
        result = result.replace(home, "<home>")
    result = result.replace(str(Path.cwd()), "<project>")
    result = WINDOWS_PATH_RE.sub("<path>", result)
    result = UNIX_PATH_RE.sub("<path>", result)
    result = IPV4_RE.sub("<host>", result)
    result = LOCALHOST_RE.sub("<host>", result)
    result = "".join(
        character
        for character in result
        if character in "\n\t" or ord(character) >= 32
    )
    if len(result) > MAX_DIAGNOSTIC_CHARS:
        result = result[:MAX_DIAGNOSTIC_CHARS] + "\n<truncated>"
    return result


def _command_evidence(
    result: subprocess.CompletedProcess[Any],
    serial: str,
) -> dict[str, Any]:
    stdout = result.stdout
    stderr = result.stderr
    if isinstance(stdout, bytes):
        stdout = f"<binary:{len(stdout)} bytes>"
    if isinstance(stderr, bytes):
        stderr = f"<binary:{len(stderr)} bytes>"
    return {
        "returncode": result.returncode,
        "stdout": _safe_text(str(stdout or ""), serial),
        "stderr": _safe_text(str(stderr or ""), serial),
    }



def _git_head_sha() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    if completed.returncode != 0:
        raise AcceptanceFailure("Не удалось определить exact head SHA для acceptance.")
    sha = str(completed.stdout).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise AcceptanceFailure("Git вернул недопустимый head SHA.")
    return sha


def _external_backend_evidence(report: dict[str, Any]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = [
        {
            "backend": "ADB",
            "level": "REAL_ACCEPTANCE",
            "evidence": [
                "transport",
                "package_readiness",
                "png_screenshot_bgr",
                "target_explicit_reconnect",
            ],
            "limitations": "One configured target and package on this exact head.",
        }
    ]
    preview = report.get("live_preview", {})
    screenshot_backend = str(report.get("screenshot_backend", ""))
    if preview.get("mode") == "screenshot_fallback" and screenshot_backend:
        evidence.append(
            {
                "backend": screenshot_backend,
                "level": "REAL_ACCEPTANCE",
                "evidence": ["two_consecutive_bgr_frames", "webui_fallback"],
                "limitations": "Configured screenshot backend only.",
            }
        )
        scrcpy = preview.get("scrcpy", {})
        evidence.append(
            {
                "backend": "scrcpy",
                "level": "HANDSHAKE_ONLY",
                "evidence": ["server_start", "device_metadata", "resolution"],
                "limitations": str(scrcpy.get("reason", "no_raw_frame")),
            }
        )
    elif preview.get("mode") == "scrcpy":
        evidence.append(
            {
                "backend": "scrcpy",
                "level": "REAL_ACCEPTANCE",
                "evidence": ["server_start", "device_metadata", "first_video_chunk"],
                "limitations": "One configured device and one initial stream.",
            }
        )
    control = report.get("control", {})
    configured = control.get("configured_backend", {})
    backend = str(configured.get("backend", report.get("control_backend", "")))
    if backend:
        evidence.append(
            {
                "backend": backend,
                "level": "REAL_ACCEPTANCE_HANDSHAKE",
                "evidence": [str(configured.get("probe", "configured_backend_probe"))],
                "limitations": "Handshake only; no touch command was sent.",
            }
        )
    return evidence


def _load_profile(profile: str) -> dict[str, str]:
    from module.config.config import AzurLaneConfig

    config = AzurLaneConfig(profile, task=None)
    return {
        "serial": str(config.Emulator_Serial),
        "package": str(config.Emulator_PackageName),
        "screenshot_backend": str(config.Emulator_ScreenshotMethod),
        "control_backend": str(config.Emulator_ControlMethod),
    }


def _validate_profile_name(profile: str) -> None:
    if not PROFILE_RE.fullmatch(profile):
        raise AcceptanceFailure("Имя profile содержит недопустимые символы.")


def _validate_serial(serial: str) -> None:
    if len(serial) > 256 or not SERIAL_RE.fullmatch(serial):
        raise AcceptanceFailure(
            "Target serial содержит пробельные, управляющие или недопустимые символы."
        )


def _validate_package(package: str) -> None:
    if not PACKAGE_RE.fullmatch(package):
        raise AcceptanceFailure("Package name содержит недопустимые символы.")


def _resolve_serial(args: argparse.Namespace, profile: dict[str, str]) -> str:
    if args.serial and args.serial_from_config:
        raise AcceptanceFailure("Используйте только один способ выбора serial.")
    if args.serial_from_config:
        serial = profile["serial"].strip()
    else:
        serial = str(args.serial or "").strip()
    if not serial or serial.lower() == "auto":
        raise AcceptanceFailure(
            "Target serial должен быть задан однозначно; значение auto запрещено для acceptance."
        )
    _validate_serial(serial)
    return serial


def _list_targets(adb: str) -> list[tuple[str, str]]:
    completed = subprocess.run(
        [adb, "devices"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    if completed.returncode != 0:
        raise AcceptanceFailure(
            "Не удалось получить список устройств ADB; raw stderr оставлен только в локальном журнале."
        )
    targets: list[tuple[str, str]] = []
    for line in completed.stdout.splitlines()[1:]:
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) >= 2:
            targets.append((parts[0], parts[1]))
    return targets


def _detect_package(adb: str, serial: str, configured: str) -> str:
    if configured and configured.lower() != "auto":
        _validate_package(configured)
        result = _run_adb(adb, serial, "shell", "pm", "path", configured)
        if result.returncode != 0 or not str(result.stdout).strip().startswith("package:"):
            raise AcceptanceFailure(
                f"Настроенный пакет приложения не найден на выбранном target: {configured}"
            )
        return configured

    from module.config.server import VALID_CHANNEL_PACKAGE, VALID_PACKAGE

    result = _run_adb(adb, serial, "shell", "pm", "list", "packages")
    if result.returncode != 0:
        raise AcceptanceFailure("Не удалось получить список пакетов приложения.")
    installed = {
        line.removeprefix("package:").strip()
        for line in str(result.stdout).splitlines()
        if line.startswith("package:")
    }
    known = sorted(installed & (set(VALID_PACKAGE) | set(VALID_CHANNEL_PACKAGE)))
    if len(known) != 1:
        raise AcceptanceFailure(
            "Автоматическое определение пакета неоднозначно: "
            f"найдено подходящих пакетов {len(known)}."
        )
    _validate_package(known[0])
    return known[0]


def _validate_bgr_image(image: Any) -> dict[str, Any]:
    try:
        import numpy as np
    except ImportError as error:
        raise AcceptanceFailure(
            "Для проверки BGR-контракта требуются установленные зависимости проекта."
        ) from error
    if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[2] != 3:
        raise AcceptanceFailure("Нарушен контракт numpy.ndarray BGR.")
    return {
        "array_shape": list(image.shape),
        "array_dtype": str(image.dtype),
        "color_contract": "BGR",
    }


def _decode_screenshot(payload: bytes) -> dict[str, Any]:
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AcceptanceFailure("ADB screencap не вернул корректный PNG stream.")
    if len(payload) < 24:
        raise AcceptanceFailure("PNG stream снимка экрана усечён.")
    width, height = struct.unpack(">II", payload[16:24])
    try:
        import cv2
        import numpy as np
    except ImportError as error:
        raise AcceptanceFailure(
            "Для проверки BGR-контракта требуются установленные зависимости проекта."
        ) from error
    image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise AcceptanceFailure("OpenCV не удалось декодировать PNG снимка экрана.")
    metadata = _validate_bgr_image(image)
    metadata.update(
        {
            "png_bytes": len(payload),
            "png_width": width,
            "png_height": height,
        }
    )
    return metadata


def _confirm(expected: str, prompt: str, non_interactive: bool) -> bool:
    if non_interactive:
        return False
    entered = input(f"{prompt}\nВведите {expected}, чтобы продолжить: ").strip()
    return entered == expected


def _preview_dependencies():
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r".*invalid escape sequence.*",
            category=SyntaxWarning,
            module=r"module\.device\.method\.uiautomator_2",
        )
        from module.webui.api import (
            LiveScrcpySession,
            _get_ffmpeg_path,
            _init_live_screenshot_fallback,
        )
    return LiveScrcpySession, _get_ffmpeg_path, _init_live_screenshot_fallback


def _new_device(profile: str):
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r".*invalid escape sequence.*",
            category=SyntaxWarning,
            module=r"module\.device\.method\.uiautomator_2",
        )
        from module.config.config import AzurLaneConfig
        from module.device.device import Device
    return Device(AzurLaneConfig(profile, task=None))


def _wait_for_scrcpy_chunk(
    session: Any,
    timeout: float = PREVIEW_WAIT_SECONDS,
) -> bytes | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = session.read_video()
        if chunk is None:
            continue
        if chunk == b"":
            return None
        return bytes(chunk)
    return None


def _check_preview(profile: str, screenshot_backend: str) -> dict[str, Any]:
    LiveScrcpySession, get_ffmpeg_path, init_screenshot_fallback = _preview_dependencies()
    session = None
    scrcpy_reason = "not_started"
    try:
        session = LiveScrcpySession.acquire(
            profile,
            fps=15,
            width=640,
            bitrate_scale=0.5,
        )
        if session.alive:
            first_chunk = _wait_for_scrcpy_chunk(session)
            if first_chunk:
                return {
                    "status": "PASS",
                    "mode": "scrcpy",
                    "resolution": list(session.resolution),
                    "first_video_chunk_bytes": len(first_chunk),
                }
            scrcpy_reason = "no_frame_after_handshake"
        else:
            scrcpy_reason = "session_not_alive"
    except Exception as error:
        scrcpy_reason = f"startup_{type(error).__name__}"
    finally:
        if session is not None:
            LiveScrcpySession.release(profile, session=session)

    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        raise AcceptanceFailure(
            "Raw scrcpy не передал видеоблок, а ffmpeg для резервного preview не найден."
        )

    try:
        device, first = init_screenshot_fallback(profile)
        first_metadata = _validate_bgr_image(first)
        second = device.screenshot()
        second_metadata = _validate_bgr_image(second)
    except Exception as error:
        raise AcceptanceFailure(
            "Не удалось подтвердить ни raw scrcpy, ни резервный preview через "
            f"настроенный backend {screenshot_backend}: {type(error).__name__}."
        ) from error

    return {
        "status": "PASS",
        "mode": "screenshot_fallback",
        "configured_screenshot_backend": screenshot_backend,
        "ffmpeg_available": True,
        "frames_verified": 2,
        "first_frame": first_metadata,
        "second_frame": second_metadata,
        "scrcpy": {
            "status": "UNAVAILABLE",
            "reason": scrcpy_reason,
        },
    }


def _close_minitouch_probe(device: Any) -> None:
    client = getattr(device, "_minitouch_client", None)
    if client is not None:
        try:
            client.close()
        except Exception:
            pass
    port = int(getattr(device, "_minitouch_port", 0) or 0)
    if port:
        try:
            device.adb_forward_remove(f"tcp:{port}")
        except Exception:
            pass


def _check_configured_control_backend(profile: str, backend: str) -> dict[str, Any]:
    if backend == "ADB":
        return {
            "status": "PASS",
            "backend": backend,
            "probe": "target_explicit_adb_transport",
        }
    if backend != "minitouch":
        raise AcceptanceFailure(
            f"Acceptance-probe для настроенного control backend {backend} не реализован."
        )

    device = None
    try:
        device = _new_device(profile)
        device.minitouch_init()
        return {
            "status": "PASS",
            "backend": backend,
            "probe": "handshake_without_touch",
            "max_x": int(device.max_x),
            "max_y": int(device.max_y),
        }
    except Exception as error:
        raise AcceptanceFailure(
            "Не удалось подтвердить handshake настроенного backend minitouch: "
            f"{type(error).__name__}."
        ) from error
    finally:
        if device is not None:
            _close_minitouch_probe(device)


def _is_network_serial(serial: str) -> bool:
    match = NETWORK_SERIAL_RE.fullmatch(serial)
    if match is None:
        return False
    port = int(match.group("port"))
    return 0 < port <= 65_535


def _wait_for_target_device(
    adb: str,
    serial: str,
    timeout: float = RECONNECT_WAIT_SECONDS,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    attempts = 0
    last_state: dict[str, Any] = {
        "returncode": None,
        "stdout": "",
        "stderr": "Проверка состояния ещё не выполнялась.",
    }
    while time.monotonic() < deadline:
        state = _run_adb(adb, serial, "get-state", timeout=10)
        attempts += 1
        last_state = _command_evidence(state, serial)
        if state.returncode == 0 and str(state.stdout).strip() == "device":
            return {
                "restored": True,
                "attempts": attempts,
                "last_state": last_state,
            }
        time.sleep(1)
    return {
        "restored": False,
        "attempts": attempts,
        "last_state": last_state,
    }


def _check_reconnect(adb: str, serial: str, non_interactive: bool) -> dict[str, Any]:
    network_target = _is_network_serial(serial)
    prompt = (
        "Будет выполнен target-explicit `adb -s <serial> reconnect`. "
        "ADB server не перезапускается."
    )
    if network_target:
        prompt += (
            " Для TCP target затем будет выполнен explicit `adb connect <serial>`, "
            "чтобы восстановить тот же сетевой transport."
        )
    if not _confirm("RECONNECT", prompt, non_interactive):
        return {"status": "SKIPPED", "reason": "Пользователь не подтвердил reconnect."}

    result = _run_adb(adb, serial, "reconnect", timeout=30)
    evidence = _command_evidence(result, serial)
    evidence["network_target"] = network_target
    if result.returncode != 0:
        raise AcceptanceFailure("Команда безопасного reconnect завершилась ошибкой.")

    if network_target:
        connect_result = _run_adb_connect(adb, serial, timeout=30)
        evidence["explicit_connect"] = _command_evidence(connect_result, serial)
        evidence["recovery_mode"] = "explicit_tcp_connect"
    else:
        evidence["recovery_mode"] = "target_reconnect"

    restored = _wait_for_target_device(adb, serial)
    evidence["state_checks"] = restored["attempts"]
    evidence["last_state"] = restored["last_state"]
    if restored["restored"]:
        evidence["transport_restored"] = True
        evidence["status"] = "PASS"
        return evidence

    if network_target:
        raise AcceptanceFailure(
            "ADB TCP transport не восстановился после target-explicit reconnect, "
            "explicit connect и ожидания в течение 60 секунд."
        )
    raise AcceptanceFailure(
        "ADB transport не восстановился после reconnect за 60 секунд."
    )


def _check_control(
    profile: str,
    backend: str,
    adb: str,
    serial: str,
    non_interactive: bool,
) -> dict[str, Any]:
    backend_probe = _check_configured_control_backend(profile, backend)
    action = "Android KEYCODE_BACK (однократно)"
    if not _confirm(
        "BACK",
        f"Backend {backend} подтверждён без касания экрана. "
        f"Будет отправлено безопасное действие управления: {action}. "
        "Текст, покупки, бой и расход ресурсов не затрагиваются.",
        non_interactive,
    ):
        return {
            "status": "SERIALIZATION_ONLY",
            "configured_backend": backend_probe,
            "action": action,
            "serialized_command": [
                "adb",
                "-s",
                "<serial>",
                "shell",
                "input",
                "keyevent",
                "4",
            ],
        }
    result = _run_adb(adb, serial, "shell", "input", "keyevent", "4")
    evidence = _command_evidence(result, serial)
    evidence["configured_backend"] = backend_probe
    evidence["action"] = action
    evidence["action_transport"] = "target_explicit_adb_keyevent"
    if result.returncode != 0:
        raise AcceptanceFailure("Безопасное действие управления завершилось ошибкой.")
    evidence["status"] = "PASS"
    return evidence


def run_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    _validate_profile_name(args.profile)
    profile = _load_profile(args.profile)
    serial = _resolve_serial(args, profile)
    args.resolved_serial = serial
    adb = _resolve_adb(args.adb)
    args.resolved_adb = adb
    targets = _list_targets(adb)
    matches = [status for target, status in targets if target == serial]
    if len(matches) != 1:
        raise AcceptanceFailure(
            "Выбранный target отсутствует или неоднозначен в `adb devices`."
        )
    if matches[0] != "device":
        raise AcceptanceFailure(
            f"ADB transport выбранного target не готов: status={matches[0]}"
        )
    if profile["serial"].lower() != "auto" and profile["serial"] != serial:
        raise AcceptanceFailure(
            "Target serial не совпадает с serial профиля; runner не подменяет конфигурацию."
        )

    package = _detect_package(adb, serial, profile["package"])
    selected = {
        "profile": args.profile,
        "serial": serial,
        "package": package,
        "screenshot_backend": profile["screenshot_backend"],
        "control_backend": profile["control_backend"],
        "adb": adb,
    }
    print("Выбран acceptance target:")
    for key, value in selected.items():
        print(f"- {key}: {value}")
    print(
        "Гарантированно не выполняются: установка APK, очистка app data, запуск task queue, "
        "покупки, бой, чтение clipboard и ввод пользовательского текста."
    )
    if not _confirm(
        "START",
        "Проверьте profile, serial, package и backend выше.",
        args.non_interactive,
    ):
        raise AcceptanceFailure(
            "Acceptance отменён до выполнения снимка экрана, preview, control и reconnect."
        )

    report: dict[str, Any] = {
        "status": "RUNNING",
        "stage": "8A",
        "head_sha": _git_head_sha(),
        "profile": args.profile,
        "target_serial": "<serial>",
        "package": package,
        "screenshot_backend": profile["screenshot_backend"],
        "control_backend": profile["control_backend"],
        "forbidden_actions": [
            "install_apk",
            "clear_app_data",
            "purchase",
            "combat",
            "task_queue",
            "clipboard_read",
            "user_text_input",
            "adb_kill_server",
        ],
    }
    args.partial_report = report

    temp_path: Path | None = None
    try:
        state = _run_adb(adb, serial, "get-state")
        report["adb_transport"] = _command_evidence(state, serial)
        if state.returncode != 0 or str(state.stdout).strip() != "device":
            raise AcceptanceFailure("ADB get-state не подтвердил transport=device.")

        package_check = _run_adb(adb, serial, "shell", "pm", "path", package)
        report["package_readiness"] = _command_evidence(package_check, serial)
        if package_check.returncode != 0 or not str(package_check.stdout).strip().startswith(
            "package:"
        ):
            raise AcceptanceFailure("Пакет приложения недоступен после transport readiness.")

        screenshot = _run_adb(
            adb,
            serial,
            "exec-out",
            "screencap",
            "-p",
            binary=True,
            timeout=30,
        )
        if screenshot.returncode != 0:
            raise AcceptanceFailure(
                "Создание одного снимка экрана через ADB завершилось ошибкой."
            )
        if not isinstance(screenshot.stdout, bytes):
            raise AcceptanceFailure("ADB screencap вернул неожиданный текстовый payload.")
        with tempfile.NamedTemporaryFile(
            prefix="stage8a-",
            suffix=".png",
            delete=False,
        ) as temporary:
            temporary.write(screenshot.stdout)
            temp_path = Path(temporary.name)
        payload = temp_path.read_bytes()
        report["screenshot"] = _decode_screenshot(payload)
        temp_path.unlink(missing_ok=True)
        temp_path = None

        report["live_preview"] = (
            _check_preview(args.profile, profile["screenshot_backend"])
            if args.check_preview
            else {"status": "SKIPPED", "reason": "Флаг --check-preview не задан."}
        )
        report["control"] = (
            _check_control(
                args.profile,
                profile["control_backend"],
                adb,
                serial,
                args.non_interactive,
            )
            if args.check_control
            else {"status": "SKIPPED", "reason": "Флаг --check-control не задан."}
        )
        if args.check_control and report["control"]["status"] != "PASS":
            raise AcceptanceFailure("Проверка управления не была полностью подтверждена.")

        report["reconnect"] = (
            _check_reconnect(adb, serial, args.non_interactive)
            if args.check_reconnect
            else {"status": "SKIPPED", "reason": "Флаг --check-reconnect не задан."}
        )
        if args.check_reconnect and report["reconnect"]["status"] != "PASS":
            raise AcceptanceFailure("Проверка reconnect не была полностью подтверждена.")

        report["external_backend_evidence"] = _external_backend_evidence(report)
        report["status"] = "PASS"
        return report
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Безопасный real-device/emulator acceptance Stage 8A"
    )
    parser.add_argument("--profile", default="alas")
    serial_group = parser.add_mutually_exclusive_group(required=True)
    serial_group.add_argument("--serial")
    serial_group.add_argument("--serial-from-config", action="store_true")
    parser.add_argument("--adb")
    parser.add_argument("--check-preview", action="store_true")
    parser.add_argument("--check-control", action="store_true")
    parser.add_argument("--check-reconnect", action="store_true")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)

    try:
        report = run_acceptance(args)
    except Exception as error:
        error_text = str(error)
        resolved_adb = str(getattr(args, "resolved_adb", ""))
        if resolved_adb:
            error_text = error_text.replace(resolved_adb, "<adb>")
        resolved_serial = str(getattr(args, "resolved_serial", args.serial or ""))
        report = dict(getattr(args, "partial_report", {}))
        report.update(
            {
                "status": "FAIL",
                "stage": "8A",
                "error": _safe_text(error_text, resolved_serial),
                "target_serial": "<serial>",
            }
        )

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Sanitized report: {args.report}")
    if report["status"] == "PASS":
        print("Stage 8A device acceptance: PASS")
        return 0
    print(
        "Stage 8A device acceptance: FAIL — "
        f"{report.get('error', 'неизвестная ошибка')}"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
