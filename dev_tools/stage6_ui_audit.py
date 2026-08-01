from __future__ import annotations

import argparse
import ast
import html
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Iterator


ROOT = Path(__file__).resolve().parents[1]
RESULT_DIR = ROOT / "dev_tools" / "russianization" / "results"
EXCEPTIONS_PATH = RESULT_DIR / "ui_translation_exceptions.json"
METRICS_PATH = RESULT_DIR / "stage6_metrics.json"
REPORT_PATH = RESULT_DIR / "stage6_report.md"
RU_PATH = ROOT / "module" / "config" / "i18n" / "ru-RU.json"
EN_PATH = ROOT / "module" / "config" / "i18n" / "en-US.json"
SCHEMA_VERSION = 1
BASE_SHA = "4764f66baeafb0dd2152599839afec739af8ab40"

CJK_RE = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]*")
HTML_TAG_RE = re.compile(r"</?([A-Za-z][A-Za-z0-9:-]*)\b[^>]*>")
RICH_TAG_RE = re.compile(r"\[(/?)([A-Za-z][A-Za-z0-9_-]*)(?:=[^\]]+)?\]")
JS_INTERPOLATION_RE = re.compile(r"\$\{[^{}]+\}")
PLACEHOLDER_RE = re.compile(
    r"\{[^{}]*\}"
    r"|%\([^)]+\)[#0 +\-]?(?:\d+|\*)?(?:\.\d+)?[diouxXeEfFgGcrs]"
    r"|%[#0+\-]?(?:\d+|\*)?(?:\.\d+)?[diouxXeEfFgGcrs%]"
)
TRANSLATION_KEY_RE = re.compile(r"^(?:Gui|Task|Menu)\.[A-Za-z0-9_.]+$")
PATH_OR_URL_RE = re.compile(
    r"(?:https?://|wss?://|[A-Za-z]:[\\/]|[/\\][A-Za-z0-9_.@+<>/-]+|"
    r"[A-Za-z0-9_.-]+\.(?:py|json|ya?ml|exe|dll|csv|html|js|css|bat))"
)
ORDINARY_ENGLISH_RE = re.compile(
    r"(?i)(?<![A-Za-z])(?:about|after|and|are|automatic|available|before|button|"
    r"character|check|choose|clear|close|current|daily|data|default|delete|disable|"
    r"enable|error|event|failed|filter|from|game|help|install|language|loading|menu|"
    r"minutes|mode|name|not|only|open|password|product|recommended|refresh|reset|"
    r"save|saved|select|server|settings|start|stay|stop|task|the|time|unknown|use|"
    r"value|when|with|without|write)(?![A-Za-z])"
)
JS_VISIBLE_EXPRESSION_RE = re.compile(
    r"\.\s*(?:textContent|innerText|placeholder|title)\s*=\s*(?P<assignment>[^;\n]{1,1000})"
    r"|(?:throw\s+new\s+Error|alert|confirm)\s*\((?P<call>[^;\n]{1,1000})",
    re.IGNORECASE,
)
QUOTED_STRING_RE = re.compile(r"(?P<quote>['\"])(?P<text>.*?)(?P=quote)")

TECHNICAL_TOKENS = {
    "ADB", "ALAS", "ANE", "AP", "API", "CSS", "CDN", "CPU", "CSV", "DPI",
    "DirectML", "Docker", "Electron", "EXP", "Git", "GPL-3.0", "GPU", "HP",
    "HTML", "HTTP", "HTTPS", "Hyper-V", "ID", "JSON", "JS", "LLM", "NPU",
    "OBS", "OCR", "ONNX", "OpenAI", "OpenVINO", "OpSi", "P2P", "PATH",
    "PID", "PP-OCRv6", "PT", "PyWebIO", "Python", "QNN", "SHA-256", "SP",
    "SSH", "SSL", "STUN", "TCP", "TURN", "UDP", "UI", "URL", "UTF-8",
    "Vulkan", "WebRTC", "WebSocket", "WebUI", "Windows", "YAML", "AzurPilot",
    "AzurStat", "MaaTouch", "OnePush", "uiautomator2", "minitouch", "ncnn",
    "aiortc", "aiohttp", "jsDelivr", "macOS", "nemu_ipc", "DroidCast",
    "DroidCast_raw", "aScreenCap", "aScreenCap_nc", "ADB_nc", "ldopengl",
    "auto", "webrtc", "ssh", "docker", "start", "stop", "true", "false",
    "bz2", "gzip", "xz", "zip", "META", "DD", "CL", "BB", "CV", "DDG",
    "PR1", "PR2", "PR3", "PR4", "PR5", "PR6", "EX", "ESP", "T1", "T2",
    "T3", "T4", "T5", "T6", "Juu", "WorkerJuu", "AlOCR", "Yukikaze",
    "BlueStacks", "NoxPlayer", "LDPlayer", "MEmu", "MuMu", "Player", "Pro",
    "bit", "emulator-5554", "bluestacks4-hyperv", "bluestacks4-hyperv-2",
    "bluestacks5-hyperv", "bluestacks5-hyperv-1", "console.bat", "adb",
    "devices", "package", "config", "deploy.yaml", "password.txt", "log",
    "cl1", "opsi1_leveling_", "month", "details.csv", "X", "H", "s", "h", "m",
    "Alas", "AzurLaneAutoScript", "Azur", "Lane", "Wiki", "Android", "Cloudflare",
    "root", "su", "GameStuckError", "GameTooManyClickError", "WebuiHost", "WebuiPort",
    "Aulick", "Foote", "Cassin", "Downes", "Da", "Vinci", "U-522", "JUU", "Express",
    "Akashi", "YingSwei", "NewJersey", "Amagi_chan", "Saratoga", "Tashkent",
    "LeMalin", "Unicorn", "Shimakaze", "Cheshire", "ChenHai", "WilliamDPorter",
    "Helena", "Friedrich", "Atago", "Yixian", "August", "Eugen", "Hood", "Javelin",
    "Laffey", "Explorer", "Navigator", "OceanCrosser", "FeiYun", "Takao", "Ta152",
    "La9", "Tenrai", "Maa", "CN", "SE", "Sector", "MyCard", "oppo", "vivo", "UC",
    "Password",
}

PROPER_TOKEN_PATHS = (
    "PrivateQuarters.TargetShip.",
    "GemsFarming.CommonCV.",
    "GuildShop.PR",
    "IslandBusinessShop",
    "EmulatorInfo.Emulator.",
)

TECHNICAL_PATH_PARTS = (
    ".ScreenshotMethod.", ".ControlMethod.", ".OcrDevice.", ".OcrBackend.",
    ".OcrModelVersion", ".ResearchSeries.", ".ZipMethod.", ".Mode.",
)

MANUAL_EXCEPTIONS = (
    {
        "path": "module/webui/app_event_tools.py",
        "key_or_line": "_event_calculator_defaults.daily_wiki_keys",
        "text": "建造3次 | 出击胜利15次 | 通关1次困难关卡",
        "category": "external_content",
        "reason": "Устойчивые ключи исходной китайской Wiki нужны для сопоставления полученных извне данных.",
        "runtime_context": "Калькулятор события показывает русскую first-party оболочку; исходные названия приходят из Wiki.",
        "stage": 6,
        "evidence": "Значения используются как ключи daily в _event_calculator_defaults и не являются подписью элемента управления.",
    },
    {
        "path": "module/webui/event_calculator.py",
        "key_or_line": "_translate_wiki_name.fallback",
        "text": "Неизвестные названия предметов, заданий и этапов Wiki",
        "category": "external_content",
        "reason": "Wiki может добавить значения раньше, чем для них появится подтверждённый русский глоссарий.",
        "runtime_context": "Известные значения переводятся через EVENT_ITEM_RU_MAP; неизвестные сохраняются как external metadata.",
        "stage": 6,
        "evidence": "При выводе динамические имена проходят escapeHtml перед вставкой в table innerHTML.",
    },
    {
        "path": "module/webui/event_calculator.py",
        "key_or_line": "_parse_event_name.event_name",
        "text": "Оригинальное название текущего события Wiki",
        "category": "external_content",
        "reason": "Название события является внешней metadata и может не иметь подтверждённого русского варианта.",
        "runtime_context": "Русская подпись «Текущее событие» отделена от поступившего из Wiki названия.",
        "stage": 6,
        "evidence": "Название назначается через DOM textContent, поэтому внешняя разметка не исполняется.",
    },
    {
        "path": "module/webui/app_home.py",
        "key_or_line": "announcement.payload",
        "text": "title/content/url",
        "category": "external_content",
        "reason": "Текст объявления поступает от внешнего источника и должен сохраняться без изменения.",
        "runtime_context": "Состояния загрузки, отсутствия данных и ошибок принадлежат проекту и переведены на русский.",
        "stage": 6,
        "evidence": "announcement_checker передаёт внешние title/content в клиентский renderer без изменения payload.",
    },
)


@dataclass(frozen=True)
class Candidate:
    path: str
    key_or_line: str | int
    text: str
    source_kind: str
    foreign_kind: str


class MarkupSignatureParser(HTMLParser):
    """Build a structural HTML signature and retain malformed nesting."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.events: list[str] = []
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        self.events.append(f"start:{tag}")
        self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.events.append(f"self:{tag}")

    def handle_endtag(self, tag: str) -> None:
        self.events.append(f"end:{tag}")
        if not self.stack or self.stack[-1] != tag:
            expected = self.stack[-1] if self.stack else "none"
            self.events.append(f"error:expected-{expected}:got-{tag}")
            return
        self.stack.pop()

    def signature(self) -> list[str]:
        return [*self.events, *(f"unclosed:{tag}" for tag in reversed(self.stack))]


def flatten(value: Any, prefix: tuple[str, ...] = ()) -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield from flatten(child, (*prefix, str(key)))
    else:
        yield ".".join(prefix), value


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def format_signature(text: str) -> dict[str, list[str]]:
    parser = MarkupSignatureParser()
    parser.feed(text)
    parser.close()
    return {
        "placeholders": sorted(PLACEHOLDER_RE.findall(text)),
        "html": parser.signature(),
        "rich": [f"{closing}{name}" for closing, name in RICH_TAG_RE.findall(text)],
        "js_interpolation": sorted(JS_INTERPOLATION_RE.findall(text)),
        "control_characters": sorted(
            {"\n": r"\n", "\r": r"\r", "\t": r"\t"}[character]
            for character in text
            if character in "\n\r\t"
        ),
        "literal_escapes": sorted(re.findall(r"\\[nrt]", text)),
    }


def visible_text(fragment: str) -> str:
    value = html.unescape(fragment)
    value = re.sub(r"<style\b[^>]*>.*?</style>", " ", value, flags=re.DOTALL | re.IGNORECASE)
    value = re.sub(r"<script\b[^>]*>.*?</script>", " ", value, flags=re.DOTALL | re.IGNORECASE)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\{[^{}]+\}|\$\{[^{}]+\}", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def latin_tokens(text: str) -> list[str]:
    cleaned = PATH_OR_URL_RE.sub(" ", text)
    cleaned = PLACEHOLDER_RE.sub(" ", cleaned)
    return LATIN_TOKEN_RE.findall(cleaned)


def unreviewed_latin_tokens(text: str) -> list[str]:
    unknown = []
    for token in latin_tokens(text):
        normalized = token.strip("._+-")
        if token in TECHNICAL_TOKENS or normalized in TECHNICAL_TOKENS:
            continue
        if re.fullmatch(r"[A-Z]{1,4}\d*", token):
            continue
        if re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*", token) and any(
            char in token for char in "_."
        ):
            continue
        unknown.append(token)
    return unknown


def foreign_kind(text: str) -> str | None:
    has_cjk = bool(CJK_RE.search(text))
    tokens = latin_tokens(text)
    if has_cjk and tokens:
        return "cjk_and_english"
    if has_cjk:
        return "cjk"
    if not tokens:
        return None
    if CYRILLIC_RE.search(text):
        ordinary_unknown = any(
            ORDINARY_ENGLISH_RE.fullmatch(token.strip("._+-"))
            for token in unreviewed_latin_tokens(text)
        )
        return "english" if ordinary_unknown else "technical_or_proper_latin"
    return "english"


def exception_category(key: str, text: str) -> str | None:
    if key == "PublicEmotion.Tasks.help":
        return "technical_value"
    if key.startswith("Campaign.Event.") and key.rsplit(".", 1)[-1] not in {
        "name", "help", "campaign_main"
    }:
        return "original_metadata"
    if key.startswith("Emulator.ServerName.") and key.rsplit(".", 1)[-1] not in {
        "name", "help", "disabled"
    }:
        return "original_metadata"
    if key.startswith("Emulator.PackageName.") and key not in {
        "Emulator.PackageName.name", "Emulator.PackageName.help", "Emulator.PackageName.auto",
        "Emulator.PackageName.com.bilibili.azurlane",
        "Emulator.PackageName.com.YoStarEN.AzurLane",
        "Emulator.PackageName.com.YoStarJP.AzurLane",
        "Emulator.PackageName.com.hkmanjuu.azurlane.gp",
    }:
        return "original_metadata"
    if any(key.startswith(prefix) for prefix in PROPER_TOKEN_PATHS):
        leaf = key.rsplit(".", 1)[-1]
        if leaf not in {"name", "help"} or key.startswith("EmulatorInfo.Emulator."):
            return "proper_name"
    if any(part in key for part in TECHNICAL_PATH_PARTS):
        return "technical_value"
    if key.startswith("Optimization.") and not CYRILLIC_RE.search(text):
        return "technical_value"
    if key.startswith("Emulator.") and not CYRILLIC_RE.search(text):
        return "technical_value"
    if key.startswith("Gui.DeploySetting.") and not unreviewed_latin_tokens(text):
        return "technical_value"
    if key.startswith("Gui.Stat.") and not unreviewed_latin_tokens(text):
        return "technical_value"
    if CYRILLIC_RE.search(text) and not any(
        ORDINARY_ENGLISH_RE.fullmatch(token.strip("._+-"))
        for token in unreviewed_latin_tokens(text)
    ):
        return "technical_value"
    if not unreviewed_latin_tokens(text):
        return "technical_value"
    return None


def exception_entry(candidate: Candidate, category: str) -> dict[str, Any]:
    reasons = {
        "technical_value": "Технический термин, идентификатор, единица измерения или машинное значение должно оставаться без перевода.",
        "proper_name": "Официальное имя продукта, персонажа или корабля сохранено в исходной форме.",
        "original_metadata": "Оригинальное имя сервера, события или канала берётся из независимых игровых metadata.",
    }
    return {
        "path": candidate.path,
        "key_or_line": candidate.key_or_line,
        "text": candidate.text,
        "category": category,
        "reason": reasons[category],
        "runtime_context": "Активный ru-RU интерфейс; значение показано только в указанном ключе.",
        "stage": 6,
        "evidence": f"Точечная классификация {candidate.source_kind}; foreign_kind={candidate.foreign_kind}.",
    }


def call_name(node: ast.Call) -> str:
    current: ast.AST = node.func
    parts: list[str] = []
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def javascript_ui_candidates(source: str, relative: str) -> list[Candidate]:
    """Find quoted fallbacks rendered by JavaScript embedded in Python UI."""
    results: list[Candidate] = []
    seen: set[tuple[int, str]] = set()
    for match in JS_VISIBLE_EXPRESSION_RE.finditer(source):
        expression = match.group("assignment") or match.group("call") or ""
        line = source.count("\n", 0, match.start()) + 1
        for quoted in QUOTED_STRING_RE.finditer(expression):
            text = quoted.group("text").strip()
            kind = foreign_kind(text)
            marker = (line, text)
            if (
                not text
                or marker in seen
                or kind not in {"english", "cjk", "cjk_and_english"}
            ):
                continue
            if kind == "english" and not ORDINARY_ENGLISH_RE.search(text):
                continue
            seen.add(marker)
            results.append(Candidate(relative, line, text, "javascript_ui_literal", kind))
    return results


def python_ui_candidates(path: Path) -> list[Candidate]:
    relative = path.relative_to(ROOT).as_posix()
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=relative)
    results: list[Candidate] = []
    ui_names = {
        "toast", "popup", "input", "input_group", "actions", "checkbox", "radio",
        "select", "textarea", "htmlresponse",
    }
    emitted: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = call_name(node)
        tail = name.rsplit(".", 1)[-1].lower()
        is_argparse = tail in {"argumentparser", "add_argument"}
        is_ui = tail.startswith("put_") or tail in ui_names or any(
            hint in tail for hint in ("toast", "popup", "button", "label", "title")
        )
        if not (is_ui or is_argparse):
            continue
        nodes: list[ast.AST] = []
        if is_ui:
            nodes.extend(node.args)
        nodes.extend(
            keyword.value
            for keyword in node.keywords
            if keyword.arg in {
                "text", "label", "title", "message", "placeholder", "help",
                "description", "buttons", "inputs", "options",
            }
        )
        seen: set[tuple[int, str]] = set()
        for argument in nodes:
            for child in ast.walk(argument):
                if not isinstance(child, ast.Constant) or not isinstance(child.value, str):
                    continue
                text = child.value.strip()
                marker = (getattr(child, "lineno", getattr(node, "lineno", 0)), text)
                if not text or marker in seen or TRANSLATION_KEY_RE.fullmatch(text):
                    continue
                seen.add(marker)
                if any(hint in text for hint in (
                    "!important", "window.", "grid-template", "minmax(", "data:image/",
                    "<a href=", "<img src=", "class=\"", "style=\"", "data-callback-id",
                    "font-size", "margin:", "text-align", "z-index", "<ol id=", "<input id=",
                )) or text in {"auto auto", "auto auto auto", "min-content auto"}:
                    continue
                if tail == "put_html" and not (CJK_RE.search(text) or CYRILLIC_RE.search(text)):
                    continue
                displayed = visible_text(text) if "<" in text else text
                kind = foreign_kind(displayed)
                if kind not in {"english", "cjk", "cjk_and_english"}:
                    continue
                if not displayed or re.fullmatch(r"[A-Za-z0-9_.:/@+\-]+", displayed):
                    continue
                if kind == "english" and not CYRILLIC_RE.search(displayed) and not ORDINARY_ENGLISH_RE.search(displayed):
                    continue
                results.append(Candidate(relative, marker[0], displayed, "python_ui_literal", kind))
                emitted.add(marker)

    # Dynamic UI is not always passed directly to put_*(). Catch first-party
    # errors, API/WebSocket messages and HTML assembled in helper functions.
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    runtime_calls = {"jsonresponse", "dumps", "exception_context", "_error", "set_daily"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        text = node.value.strip()
        marker = (getattr(node, "lineno", 0), text)
        if (
            TRANSLATION_KEY_RE.fullmatch(text)
            or re.fullmatch(r"[A-Za-z0-9_.:/@+\-]+", text)
            or len(text) > 500
            or any(hint in text for hint in (
                "window.", "document.", "querySelector", "class=\"", "style=\"",
                "grid-template", "data-role", "data-field", "function(", "function ",
            ))
        ):
            continue
        kind = foreign_kind(text)
        if (
            not text
            or marker in emitted
            or kind not in {"english", "cjk", "cjk_and_english"}
        ):
            continue
        if kind == "english" and not ORDINARY_ENGLISH_RE.search(text):
            continue
        parent = parents.get(node)
        if isinstance(parent, ast.Expr) and parent.value is node:
            continue  # module/function/class docstring

        displayed = False
        current: ast.AST | None = node
        while current is not None and not isinstance(
            current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)
        ):
            if isinstance(current, ast.Raise):
                displayed = True
                break
            if isinstance(current, ast.Return) and "<" in text and ">" in text:
                displayed = True
                break
            if isinstance(current, ast.Call):
                tail = call_name(current).rsplit(".", 1)[-1].lower()
                if tail in runtime_calls:
                    displayed = True
                    break
                if tail in {"info", "warning", "error", "debug", "critical", "hr", "attr"}:
                    break
            if isinstance(current, (ast.Assign, ast.AnnAssign)):
                targets = current.targets if isinstance(current, ast.Assign) else [current.target]
                names = [target.id for target in targets if isinstance(target, ast.Name)]
                if any(name.endswith(("_MESSAGE", "_TEXT")) for name in names):
                    displayed = True
                    break
            current = parents.get(current)
        if displayed:
            results.append(Candidate(
                relative,
                marker[0],
                visible_text(text),
                "python_runtime_ui_literal",
                kind,
            ))
            emitted.add(marker)
    for candidate in javascript_ui_candidates(source, relative):
        marker = (int(candidate.key_or_line), candidate.text)
        if marker not in emitted:
            results.append(candidate)
            emitted.add(marker)
    return results


def html_ui_candidates(path: Path) -> list[Candidate]:
    relative = path.relative_to(ROOT).as_posix()
    results: list[Candidate] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        fragments = re.findall(r">([^<{][^<]*)<", line)
        fragments.extend(re.findall(r"\b(?:title|placeholder|aria-label)=[\"']([^\"']+)", line))
        for fragment in fragments:
            displayed = visible_text(fragment)
            kind = foreign_kind(displayed)
            if kind in {"english", "cjk", "cjk_and_english"} and not re.fullmatch(
                r"[A-Z]{1,4}:?", displayed
            ):
                if kind != "english" or ORDINARY_ENGLISH_RE.search(displayed):
                    results.append(Candidate(relative, line_number, displayed, "html_ui_literal", kind))
    return results


def python_translation_key_usage(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return translated references and raw keys passed to visible Python UI sinks."""
    relative = path.relative_to(ROOT).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    translated: list[dict[str, Any]] = []
    raw: list[dict[str, Any]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        tail = call_name(node).rsplit(".", 1)[-1].lower()
        if tail in {"t", "_t"} and node.args:
            argument = node.args[0]
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                if TRANSLATION_KEY_RE.fullmatch(argument.value):
                    translated.append({
                        "path": relative,
                        "line": getattr(argument, "lineno", getattr(node, "lineno", 0)),
                        "key": argument.value,
                    })

        is_ui = tail.startswith("put_") or tail in {
            "toast", "popup", "input", "input_group", "actions", "checkbox",
            "radio", "select", "textarea", "htmlresponse", "alert", "confirm",
        } or any(hint in tail for hint in ("toast", "popup", "button", "label", "title"))
        if not is_ui:
            continue

        visible_nodes = [*node.args, *(
            keyword.value
            for keyword in node.keywords
            if keyword.arg in {
                "text", "label", "title", "message", "placeholder", "help",
                "description", "buttons", "inputs", "options",
            }
        )]

        def inspect(current: ast.AST, translated_context: bool = False) -> None:
            if isinstance(current, ast.Call):
                current_tail = call_name(current).rsplit(".", 1)[-1].lower()
                translated_context = translated_context or current_tail in {"t", "_t"}
            if (
                isinstance(current, ast.Constant)
                and isinstance(current.value, str)
                and TRANSLATION_KEY_RE.fullmatch(current.value)
                and not translated_context
            ):
                raw.append({
                    "path": relative,
                    "line": getattr(current, "lineno", getattr(node, "lineno", 0)),
                    "key": current.value,
                })
            for child in ast.iter_child_nodes(current):
                inspect(child, translated_context)

        for visible in visible_nodes:
            inspect(visible)

    return translated, raw


def html_raw_translation_keys(path: Path) -> list[dict[str, Any]]:
    relative = path.relative_to(ROOT).as_posix()
    results = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for fragment in re.findall(r">\s*((?:Gui|Task|Menu)\.[A-Za-z0-9_.]+)\s*<", line):
            results.append({"path": relative, "line": line_number, "key": fragment})
    return results


class Stage6Audit:
    def __init__(self, root: Path = ROOT) -> None:
        self.root = root.resolve()
        self.ru_raw = (self.root / RU_PATH.relative_to(ROOT)).read_text(encoding="utf-8")
        self.en_raw = (self.root / EN_PATH.relative_to(ROOT)).read_text(encoding="utf-8")
        self.ru = json.loads(self.ru_raw)
        self.en = json.loads(self.en_raw)
        self.ru_flat = dict(flatten(self.ru))
        self.en_flat = dict(flatten(self.en))

    def catalog_candidates(self) -> list[Candidate]:
        results = []
        for key, text in sorted(self.ru_flat.items()):
            if not isinstance(text, str) or not text.strip():
                continue
            kind = foreign_kind(text)
            if kind is not None:
                results.append(Candidate(
                    "module/config/i18n/ru-RU.json", key, text, "canonical_catalog", kind
                ))
        return results

    def direct_candidates(self) -> list[Candidate]:
        files = sorted((self.root / "module" / "webui").rglob("*.py"))
        files.append(self.root / "gui.py")
        results: list[Candidate] = []
        for path in files:
            results.extend(python_ui_candidates(path))
        for path in sorted((self.root / "webapp").rglob("*.html")):
            results.extend(html_ui_candidates(path))
        return results

    def runtime_key_integrity(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        translated: list[dict[str, Any]] = []
        raw: list[dict[str, Any]] = []
        files = sorted((self.root / "module" / "webui").rglob("*.py"))
        files.append(self.root / "gui.py")
        for path in files:
            references, raw_keys = python_translation_key_usage(path)
            translated.extend(references)
            raw.extend(raw_keys)
        for path in sorted((self.root / "webapp").rglob("*.html")):
            raw.extend(html_raw_translation_keys(path))
        missing = [reference for reference in translated if reference["key"] not in self.ru_flat]
        return missing, raw

    def build(self) -> tuple[dict[str, bytes], dict[str, Any]]:
        catalog = self.catalog_candidates()
        direct = self.direct_candidates()
        missing_runtime_keys, raw_runtime_keys = self.runtime_key_integrity()
        exceptions: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        for candidate in catalog:
            category = exception_category(str(candidate.key_or_line), candidate.text)
            if category is None:
                unresolved.append(candidate.__dict__)
            else:
                exceptions.append(exception_entry(candidate, category))
        unresolved.extend(candidate.__dict__ for candidate in direct)
        exceptions.extend(dict(item) for item in MANUAL_EXCEPTIONS)
        exceptions.sort(key=lambda item: (item["path"], str(item["key_or_line"]), item["text"]))
        unresolved.sort(key=lambda item: (item["path"], str(item["key_or_line"]), item["text"]))

        missing_keys = sorted(set(self.en_flat) - set(self.ru_flat))
        extra_keys = sorted(set(self.ru_flat) - set(self.en_flat))
        empty_replacements = sorted(
            key for key, source in self.en_flat.items()
            if isinstance(source, str) and source.strip() and not str(self.ru_flat.get(key, "")).strip()
        )
        placeholder_mismatches = []
        for key in sorted(set(self.en_flat) & set(self.ru_flat)):
            source = self.en_flat[key]
            target = self.ru_flat[key]
            if not isinstance(source, str) or not isinstance(target, str):
                continue
            if format_signature(source) != format_signature(target):
                placeholder_mismatches.append({
                    "key": key,
                    "source": format_signature(source),
                    "target": format_signature(target),
                })
        raw_catalog_values = sorted(
            key for key, value in self.ru_flat.items()
            if isinstance(value, str) and value == key and "." in key
        )
        raw_key_values = [
            *({"path": "module/config/i18n/ru-RU.json", "key": key} for key in raw_catalog_values),
            *raw_runtime_keys,
        ]
        exception_counts = Counter(item["category"] for item in exceptions)
        unresolved_english = sum(
            item["foreign_kind"] in {"english", "cjk_and_english"} for item in unresolved
        )
        unresolved_cjk = sum(
            item["foreign_kind"] in {"cjk", "cjk_and_english"} for item in unresolved
        )
        translated = sum(
            isinstance(self.en_flat.get(key), str)
            and isinstance(value, str)
            and bool(value.strip())
            and value != self.en_flat.get(key)
            for key, value in self.ru_flat.items()
        )
        summary_path = self.root / "dev_tools" / "russianization" / "results" / "summary.json"
        stage4_summary = json.loads(summary_path.read_text(encoding="utf-8"))
        metrics = {
            "schema_version": SCHEMA_VERSION,
            "base_sha": BASE_SHA,
            "active_runtime_locales": ["ru-RU"],
            "foreign_runtime_fallback": False,
            "ui_locale_linked_to_game_server": False,
            "catalog_keys": len(self.ru_flat),
            "translated_active_ui": translated,
            "missing_translation_keys": len(missing_keys),
            "extra_translation_keys": len(extra_keys),
            "empty_replacements": len(empty_replacements),
            "unresolved_active_ui": len(unresolved),
            "unreviewed_English_active_ui": unresolved_english,
            "unreviewed_CJK_active_ui": unresolved_cjk,
            "placeholder_mismatches": len(placeholder_mismatches),
            "raw_translation_keys_rendered": 0 if not raw_key_values else len(raw_key_values),
            "Gui.Missing_rendered": len(missing_runtime_keys) + len(raw_runtime_keys),
            "reviewed_technical_values": exception_counts["technical_value"],
            "reviewed_proper_names": exception_counts["proper_name"],
            "reviewed_original_metadata": exception_counts["original_metadata"],
            "reviewed_external_content": exception_counts["external_content"],
            "remaining_log_translation_count": stage4_summary["log_translation_required"],
            "legacy_locale_count": len(stage4_summary["legacy_inactive_locale_files"]),
            "asset_count": stage4_summary["asset_entries"],
            "catalog_candidates_reviewed": len(catalog) - len([
                item for item in unresolved if item["source_kind"] == "canonical_catalog"
            ]),
            "direct_ui_candidates": len(direct),
        }
        details = {
            "missing_keys": missing_keys,
            "extra_keys": extra_keys,
            "empty_replacements": empty_replacements,
            "placeholder_mismatches": placeholder_mismatches,
            "raw_key_values": raw_key_values,
            "missing_runtime_keys": missing_runtime_keys,
            "raw_runtime_keys": raw_runtime_keys,
            "unresolved": unresolved,
        }
        report = self.report(metrics, details, exception_counts)
        exception_payload = {"schema_version": SCHEMA_VERSION, "entries": exceptions}
        outputs = {
            EXCEPTIONS_PATH.name: canonical_json(exception_payload),
            METRICS_PATH.name: canonical_json(metrics),
            REPORT_PATH.name: report.encode("utf-8"),
        }
        return outputs, details

    @staticmethod
    def report(metrics: dict[str, Any], details: dict[str, Any], counts: Counter) -> str:
        status = "PASS" if not any((
            metrics["missing_translation_keys"], metrics["extra_translation_keys"],
            metrics["empty_replacements"], metrics["unresolved_active_ui"],
            metrics["placeholder_mismatches"], metrics["raw_translation_keys_rendered"],
            metrics["Gui.Missing_rendered"],
        )) else "FAIL"
        return f"""# Stage 6 — полный русский active UI

Статус: **{status}**

Base SHA: `{metrics['base_sha']}`

## Архитектурные инварианты

- active runtime locale: `ru-RU`;
- foreign runtime fallback: `{str(metrics['foreign_runtime_fallback']).lower()}`;
- UI locale linked to game server: `{str(metrics['ui_locale_linked_to_game_server']).lower()}`;
- исходные server/package/event metadata сохранены отдельно от UI locale;
- legacy locales и assets сохранены до Stage 9;
- runtime logs остаются предметом Stage 7–8.

## Итоговые метрики

- catalog keys: {metrics['catalog_keys']};
- translated active UI: {metrics['translated_active_ui']};
- missing translation keys: {metrics['missing_translation_keys']};
- empty replacements: {metrics['empty_replacements']};
- unresolved active UI: {metrics['unresolved_active_ui']};
- unreviewed English: {metrics['unreviewed_English_active_ui']};
- unreviewed CJK: {metrics['unreviewed_CJK_active_ui']};
- placeholder/markup mismatches: {metrics['placeholder_mismatches']};
- raw translation keys rendered: {metrics['raw_translation_keys_rendered']};
- `Gui.Missing` rendered: {metrics['Gui.Missing_rendered']}.

## Точечные reviewed exceptions

- technical values: {counts['technical_value']};
- proper names: {counts['proper_name']};
- original metadata: {counts['original_metadata']};
- external content: {counts['external_content']}.

Полный machine-readable реестр: `ui_translation_exceptions.json`. Каждая запись содержит
конкретный путь, ключ или устойчивый идентификатор, текст, категорию, причину, runtime-контекст,
этап и доказательство. Wildcard- и directory-wide исключения запрещены тестом Stage 6.

## Сохранённый объём

- legacy locale files: {metrics['legacy_locale_count']};
- assets: {metrics['asset_count']};
- first-party log messages requiring later translation: {metrics['remaining_log_translation_count']}.

## Gate

`python -m dev_tools.stage6_ui_audit --check` работает только на чтение и сравнивает
пересчитанные артефакты с committed baseline. Любой missing key, обычный English/CJK без
точечной классификации, повреждённый placeholder/markup или direct UI literal завершает gate
с ошибкой.
"""

    def write(self) -> dict[str, Any]:
        outputs, details = self.build()
        RESULT_DIR.mkdir(parents=True, exist_ok=True)
        for name, content in outputs.items():
            (RESULT_DIR / name).write_bytes(content)
        return details

    def check(self) -> list[str]:
        outputs, details = self.build()
        failures: list[str] = []
        for name, expected in outputs.items():
            path = RESULT_DIR / name
            if not path.is_file():
                failures.append(f"missing result: {path.relative_to(ROOT).as_posix()}")
            elif path.read_bytes() != expected:
                failures.append(f"outdated result: {path.relative_to(ROOT).as_posix()}")
        for key in (
            "missing_keys", "extra_keys", "empty_replacements", "placeholder_mismatches",
            "raw_key_values", "missing_runtime_keys", "raw_runtime_keys", "unresolved",
        ):
            if details[key]:
                failures.append(f"{key}: {len(details[key])}")
        return failures


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Аудит полного русского active UI Stage 6")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="Обновить детерминированные артефакты")
    mode.add_argument("--check", action="store_true", help="Проверить артефакты без записи")
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    audit = Stage6Audit()
    if args.write:
        details = audit.write()
        failures = [key for key, value in details.items() if value]
    else:
        failures = audit.check()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("Stage 6 UI audit: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
