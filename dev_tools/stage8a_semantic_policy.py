from __future__ import annotations

import re

IMMUTABLE_STAGE8A_BASE_SHA = "d7b8b18c75c6c309523c1a041431d08885e68836"

STAGE8A_SCOPE_PREFIXES = ("module/device/",)
STAGE8A_SCOPE_FILES = ("module/webui/api.py",)

TRANSFER_POLICY = {
    "[设备-基准测试] 运行OCR设备基准测试": (
        "stage8b_ocr",
        "OCR benchmark принадлежит Stage 8B, хотя вызов находится в module/device/device.py.",
    ),
    "[设备-委托] 夜间委托出现": (
        "stage8c_scheduler",
        "Обработка ночной комиссии принадлежит task/scheduler runtime Stage 8C.",
    ),
}

TECHNICAL_VALUE_POLICY = {
    "MuMu Pro": "Название семейства эмулятора.",
    "AdbClient({0}, {1})": "Техническое представление adbutils AdbClient.",
    "Device(atx_agent_url={0})": "Техническое представление объекта uiautomator2 Device.",
    "u2.Device": "Технический идентификатор класса.",
    "customer.app_keptlive": "Техническое имя capability/метода.",
    "Aborted": "Неизменённый маркер ответа scrcpy-server.",
    "display ": "Фрагмент машинной команды ADB.",
}

TECHNICAL_WORDS = {
    "ADB", "HTTP", "WebUI", "API", "SDK", "ABI", "CPU", "GPU", "PID", "RGB",
    "BGR", "PNG", "TCP", "USB", "Wi-Fi", "IPv4", "IPv6", "scrcpy",
    "ws-scrcpy", "uiautomator2", "DroidCast", "aScreenCap", "NemuIpc",
    "LDOpenGL", "minitouch", "MaaTouch", "Hermit", "WSA", "MuMu", "MuMu12",
    "MuMuPlayer", "MuMuPlayer12", "MuMuPlayerGlobal", "LDPlayer", "BlueStacks",
    "Waydroid", "VMOS", "Electron", "ATX", "Hyper-V", "Android", "SSH", "IME",
    "FPS", "H264", "H.264", "OpenGL", "Windows", "macOS", "Alas", "Azur",
    "Lane", "APK", "Activity", "GitHub", "None", "null", "px", "nc", "netcat",
    "app", "package", "serial", "stdout", "stderr", "server", "client", "video", "control", "socket", "stream", "bytes",
    "code", "reason", "Beta", "Air", "Player", "Global", "Pro", "ID", "IPC", "offline", "online", "minicap", "Accelerator", "Genshin", "Impact", "auto", "platform-tools", "ScreenshotMethod", "ControlMethod", "nemu_ipc", "ldopengl", "HandleError", "chmod", "cmdargs", "EnableRemoteSSH", "RemoteSSHHost", "mumutool", "ping", "Aborted", "True", "False",
}

CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff]")
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")
MOJIBAKE_RE = re.compile(r"(?:[ÐÑ]|\ufffd|вЂ|в„|пїЅ)")
PLACEHOLDER_RE = re.compile(
    r"\{(?:…|[^{}]*)\}|%\([^)]+\)[#0 +\-]?(?:\d+|\*)?(?:\.\d+)?[diouxXeEfFgGcrs]"
    r"|%[#0+\-]?(?:\d+|\*)?(?:\.\d+)?[diouxXeEfFgGcrs%]"
)
IDENTIFIER_RE = re.compile(
    r"\b(?:[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+"
    r"|[A-Za-z_][A-Za-z0-9_]*_[A-Za-z0-9_]+"
    r"|[A-Z][A-Z0-9_]{1,})\b"
)
BACKTICK_RE = re.compile(r"`[^`]*`")
WINDOWS_REGISTRY_RE = re.compile(r"\bHKEY_[A-Z_]+\\[^\s]+")
MACHINE_KEY_RE = re.compile(
    r"\b(?:emulator|name|path|serial|color|display|result|orientation|version|"
    r"max_contact|max_x|max_y|max_pressure|ipc_dll|nemu_folder|atx_agent_url)="
)


def placeholder_signature(text: str) -> tuple[str, ...]:
    return tuple(PLACEHOLDER_RE.findall(text))


def has_ordinary_english(text: str) -> bool:
    """Return True only for ordinary English, not preserved technical identifiers."""
    value = BACKTICK_RE.sub(" ", text)
    value = WINDOWS_REGISTRY_RE.sub(" ", value)
    value = PLACEHOLDER_RE.sub(" ", value)
    value = MACHINE_KEY_RE.sub(" ", value)
    value = IDENTIFIER_RE.sub(" ", value)
    for word in sorted(TECHNICAL_WORDS, key=len, reverse=True):
        value = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(word)}(?![A-Za-z0-9_])", " ", value, flags=re.I)
    return bool(LATIN_WORD_RE.search(value))


def classify_message(
    *,
    path: str,
    function_owner: str,
    call_kind: str,
    arg_role: str,
    message: str,
) -> tuple[str, str, bool, str]:
    if path == "module/webui/api.py" and not is_device_owned_webui(function_owner):
        return (
            "stage8c_scheduler",
            "stage8c",
            False,
            "Shared WebUI/config API передан Stage 8C.",
        )
    transfer = TRANSFER_POLICY.get(message)
    if transfer is not None:
        classification, reason = transfer
        return classification, classification.split("_", 1)[0], False, reason
    if message in TECHNICAL_VALUE_POLICY:
        return (
            "technical_identifier",
            "stage8a",
            False,
            TECHNICAL_VALUE_POLICY[message],
        )
    if message == "<dynamic expression>":
        return (
            "raw_external_payload",
            "stage8a",
            False,
            "Runtime expression сохраняется без перевода; проверяется окружающий контекст.",
        )
    if CJK_RE.search(message):
        return (
            "stage8a_first_party_message",
            "stage8a",
            True,
            "First-party CJK-сообщение требует русификации.",
        )
    if CYRILLIC_RE.search(message):
        if has_ordinary_english(message):
            return (
                "stage8a_first_party_message",
                "stage8a",
                True,
                "Русский контекст содержит обычное английское first-party предложение.",
            )
        return (
            "stage8a_first_party_message",
            "stage8a",
            False,
            "First-party контекст на русском; Latin-фрагменты являются техническими идентификаторами.",
        )
    if not LATIN_WORD_RE.search(message):
        return (
            "technical_identifier",
            "stage8a",
            False,
            "Нейтральный структурный или машинный фрагмент.",
        )
    return (
        "stage8a_first_party_message",
        "stage8a",
        True,
        "Обычное английское first-party сообщение требует русификации.",
    )


def is_device_owned_webui(owner: str) -> bool:
    allowed = (
        "_ws_scrcpy",
        "LiveWsScrcpySession.",
        "LiveScrcpySession.",
        "ws_live_screenshot",
        "_ws_live_scrcpy",
        "_ws_live_ws_scrcpy",
        "_ws_live_raw_scrcpy",
        "_ws_live_screenshot_fallback",
        "ws_live_control",
    )
    return any(owner == prefix or owner.startswith(prefix) for prefix in allowed)
