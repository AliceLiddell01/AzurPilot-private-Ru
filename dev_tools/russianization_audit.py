from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 1
RESULTS_RELATIVE = Path("dev_tools/russianization/results")
RESULT_FILENAMES = (
    "summary.json",
    "ui_strings.json",
    "first_party_logs.json",
    "asset_manifest.json",
    "locale_dependency_map.json",
    "terminology.json",
    "technical_allowlist.json",
    "asset_decisions.json",
    "en_global_required.json",
    "stage4_report.md",
    "deploy_language_migration.md",
    "stage5_9_test_matrix.md",
)

TEXT_EXTENSIONS = {
    ".py", ".pyi", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".md", ".txt", ".html", ".htm", ".css", ".js", ".mjs", ".ts",
    ".tsx", ".jsx", ".ps1", ".psm1", ".sh", ".bat", ".cmd", ".xml",
    ".csv", ".rst", ".properties",
}
BINARY_ASSET_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".ico", ".svg",
    ".onnx", ".bin", ".ncnn", ".param", ".model", ".pth", ".pt", ".pb",
    ".npz", ".npy", ".pkl", ".pickle", ".ttf", ".otf", ".woff", ".woff2",
    ".mp3", ".wav", ".ogg", ".mp4", ".avi", ".zip", ".7z", ".rar",
    ".apk", ".exe", ".dll", ".so", ".dylib",
}
ASSET_ROOTS = {
    "assets", "asset", "campaign", "doc", "docs", "screenshots", "images",
    "tests", "module/ocr", "module/webui", "module/ui", "module/os",
    "module/os_handler", "module/os_shop", "module/os_ash", "module/os_map",
    "module/campaign", "module/combat", "module/map", "module/exercise",
    "module/research", "module/raid", "module/event", "deploy", "config",
}
SOURCE_SCAN_ROOTS = {
    "module", "deploy", "scripts", "dev_tools", "campaign", "tests", ".github",
    "config", "gui.py", "alas.py", "submodule", "webapp",
}
EXCLUDED_PREFIXES = {
    ".git/", ".venv/", "venv/", "node_modules/", "dist/", "build/",
    "__pycache__/", ".pytest_cache/", ".ruff_cache/", ".mypy_cache/",
    "github/workflows/", "github/stage4_",
    RESULTS_RELATIVE.as_posix() + "/",
}
USER_UI_CLASSIFICATIONS = {
    "user_ui_text", "user_help_text", "user_error_text", "generated_output",
}
LOG_CALL_NAMES = {
    "info", "warning", "warn", "error", "exception", "critical", "debug",
    "hr", "attr", "success", "log", "print",
}
UI_CALL_HINTS = {
    "put_text", "put_markdown", "put_html", "put_button", "put_buttons",
    "toast", "popup", "alert", "confirm", "notify", "set_title", "title",
    "label", "tooltip", "placeholder", "description", "help", "message",
}
TECHNICAL_TOKEN_RE = re.compile(
    r"^(?:[A-Z0-9_]{2,}|[a-z0-9_.-]+(?:/[a-z0-9_.-]+)+|https?://\S+|"
    r"[a-zA-Z]:[\\/].*|[0-9a-f]{7,64}|--?[a-z0-9-]+|[a-z0-9_.-]+\.(?:exe|dll|py|json|yaml|yml))$"
)
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")
PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_@.+~/-]{3,}")
STRING_LITERAL_RE = re.compile(r"(?P<quote>['\"])(?P<value>[^'\"\r\n]{1,500})(?P=quote)")
POWERSHELL_MESSAGE_RE = re.compile(
    r"^\s*(?P<kind>Write-(?:Host|Output|Information|Warning|Error|Verbose|Debug)|throw)\b(?P<body>.*)$",
    re.IGNORECASE,
)
HTML_TEXT_RE = re.compile(r">\s*([^<>\r\n][^<>]{1,500}?)\s*<")
JS_UI_RE = re.compile(
    r"(?:alert|confirm|toast|notify|textContent|innerText|placeholder|title)\s*(?:=|\()\s*"
    r"(?P<quote>['\"])(?P<text>.*?)(?P=quote)",
    re.IGNORECASE,
)
LANGUAGE_SYMBOLS = (
    "Language", "LANGUAGES", "SERVER_TO_LANG", "LANG_TO_SERVER", "server_to_lang",
    "lang_to_server", "BrowserLanguage", "OcrModel", "OcrLanguage", "PackageName",
    "ServerName", "Event", "event_name",
)


@dataclass(frozen=True)
class SourceText:
    path: str
    text: str
    lines: tuple[str, ...]


class AuditError(RuntimeError):
    pass


def normalize_path(value: str | Path) -> str:
    return PurePosixPath(str(value).replace("\\", "/")).as_posix().lstrip("./")


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def compact_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def tabular_payload(entries: list[dict[str, Any]], columns: tuple[str, ...]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "columns": list(columns),
        "entries": [[entry.get(column) for column in columns] for entry in entries],
    }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def language_guess(text: str) -> str:
    has_cjk = bool(CJK_RE.search(text))
    has_cyrillic = bool(CYRILLIC_RE.search(text))
    has_latin = bool(LATIN_WORD_RE.search(text))
    if has_cjk and has_cyrillic:
        return "mixed_cjk_ru"
    if has_cjk and has_latin:
        return "mixed_cjk_latin"
    if has_cjk:
        return "cjk"
    if has_cyrillic and has_latin:
        return "mixed_ru_latin"
    if has_cyrillic:
        return "ru"
    if has_latin:
        return "latin"
    return "neutral"


def technical_only(text: str) -> bool:
    stripped = text.strip().strip("'\"")
    if not stripped:
        return True
    if TECHNICAL_TOKEN_RE.fullmatch(stripped):
        return True
    words = LATIN_WORD_RE.findall(stripped)
    if words and len(words) <= 2 and all(word.isupper() for word in words):
        return True
    return False


def translation_required(text: str, classification: str) -> bool:
    if classification not in USER_UI_CLASSIFICATIONS and classification not in {"first_party_log", "user_error_text"}:
        return False
    if technical_only(text):
        return False
    guess = language_guess(text)
    return guess in {"latin", "cjk", "mixed_cjk_latin", "mixed_cjk_ru", "mixed_ru_latin"}


def subsystem_for_path(path: str) -> str:
    p = path.lower()
    mapping = (
        (("scripts/", "deploy/"), "deploy_and_dependencies"),
        (("module/webui/", "gui.py"), "webui_and_process_lifecycle"),
        (("module/config/", "module/scheduler/"), "scheduler_and_config"),
        (("module/device/", "adb"), "device_adb_emulator"),
        (("screenshot", "control"), "screenshot_and_control"),
        (("module/ocr/", "ocr"), "ocr"),
        (("campaign/", "module/campaign/", "module/combat/", "fleet"), "campaign_combat_fleet"),
        (("module/os", "operation_siren", "opsi"), "operation_siren"),
        (("module/",), "game_tasks"),
        (("tests/",), "tests"),
        ((".github/",), "ci"),
    )
    for prefixes, subsystem in mapping:
        if any(token in p for token in prefixes):
            return subsystem
    return "other"


def flatten_json(value: Any, prefix: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Any]]:
    if isinstance(value, dict):
        for key in sorted(value):
            yield from flatten_json(value[key], prefix + (str(key),))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from flatten_json(item, prefix + (str(index),))
    else:
        yield prefix, value


def safe_source_segment(source: str, node: ast.AST) -> str:
    segment = ast.get_source_segment(source, node)
    if segment is None:
        return ""
    return segment.strip()


def literal_text(node: ast.AST, source: str) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for item in node.values:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                parts.append(item.value)
            else:
                parts.append("{…}")
        return "".join(parts)
    return safe_source_segment(source, node)


def call_name(node: ast.Call) -> str:
    target = node.func
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        parts = [target.attr]
        value = target.value
        while isinstance(value, ast.Attribute):
            parts.append(value.attr)
            value = value.value
        if isinstance(value, ast.Name):
            parts.append(value.id)
        return ".".join(reversed(parts))
    return ""


def likely_external_raw(text: str) -> bool:
    lower = text.lower()
    markers = ("stderr", "stdout", "traceback", "response.text", "exception", "raw", "returncode")
    return any(marker in lower for marker in markers) and ("{…}" in text or "%" in text or "{" in text)


def is_excluded(path: str) -> bool:
    normalized = normalize_path(path)
    return any(normalized == prefix.rstrip("/") or normalized.startswith(prefix) for prefix in EXCLUDED_PREFIXES)


def under_relevant_root(path: str) -> bool:
    normalized = normalize_path(path)
    first = normalized.split("/", 1)[0]
    return first in SOURCE_SCAN_ROOTS or normalized in SOURCE_SCAN_ROOTS


def is_asset_candidate(path: str) -> bool:
    normalized = normalize_path(path)
    suffix = Path(normalized).suffix.lower()
    if suffix in BINARY_ASSET_EXTENSIONS:
        return True
    return any(normalized == root or normalized.startswith(root + "/") for root in ASSET_ROOTS)


def asset_type(path: str) -> str:
    p = path.lower()
    suffix = Path(p).suffix.lower()
    if "test" in p or "fixture" in p:
        return "test fixture"
    if "doc/" in p or "docs/" in p or p.endswith("readme.md"):
        return "documentation media"
    if "webui" in p and suffix in {".js", ".css", ".html", ".svg", ".png", ".webp", ".ico"}:
        return "WebUI static asset"
    if suffix in {".onnx", ".ncnn", ".param", ".model", ".pth", ".pt", ".pb"}:
        return "OCR model" if "ocr" in p else "binary dependency"
    if "ocr" in p and suffix in {".png", ".jpg", ".jpeg", ".webp", ".json", ".txt", ".yaml", ".yml"}:
        return "OCR template"
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg", ".ico"}:
        if any(token in p for token in ("template", "button", "ui", "asset")):
            return "recognition screenshot/template"
        return "UI image"
    if "campaign" in p:
        return "campaign data"
    if "event" in p or "raid" in p:
        return "event data"
    if "generated" in p or "config_generated" in p:
        return "generated asset"
    if suffix in BINARY_ASSET_EXTENSIONS:
        return "binary dependency"
    return "unknown"


def scope_markers(path: str) -> tuple[str, list[str]]:
    lower = "/" + path.lower().replace("-", "_") + "/"
    markers: list[str] = []
    patterns = {
        "cn": ("/cn/", "_cn", "zh_cn", "zh-cn", "chinese", "china"),
        "jp": ("/jp/", "_jp", "ja_jp", "ja-jp", "japanese", "japan"),
        "tw": ("/tw/", "_tw", "zh_tw", "zh-tw", "taiwan", "traditional"),
        "en": ("/en/", "_en", "en_us", "en-us", "english", "global"),
    }
    scopes: list[str] = []
    for scope, tokens in patterns.items():
        matched = [token for token in tokens if token in lower]
        if matched:
            scopes.append(scope)
            markers.extend(matched)
    if not scopes:
        return "shared" if any(token in lower for token in ("shared", "common", "general")) else "unknown", []
    if len(scopes) > 1:
        return "multi_server", sorted(set(markers))
    return scopes[0], sorted(set(markers))


def status_from_evidence(scope: str, static_refs: list[dict[str, Any]], dynamic_refs: list[dict[str, Any]],
                         generated_refs: list[dict[str, Any]], test_refs: list[dict[str, Any]]) -> tuple[str, bool, float, str]:
    runtime_refs = [ref for ref in static_refs if not ref["path"].startswith(("tests/", "doc/", "docs/"))]
    if runtime_refs or dynamic_refs or generated_refs:
        status = "confirmed_keep" if runtime_refs or generated_refs else "probable_keep"
        return status, False, 0.95 if status == "confirmed_keep" else 0.75, "Найдены runtime/generated ссылки; удаление без дополнительной трассировки запрещено."
    if test_refs:
        return "probable_keep", True, 0.65, "Ресурс используется тестами; перед удалением требуется решение о сохранении покрытия."
    if scope in {"cn", "jp", "tw"}:
        return "probable_delete_candidate", True, 0.55, "Серверный маркер присутствует, подтверждённых ссылок нет; имя не является достаточным доказательством."
    return "needs_manual_review", True, 0.35, "Недостаточно доказательств назначения или безопасного удаления."


class AuditEngine:
    def __init__(self, root: Path, output_dir: Path | None = None) -> None:
        self.root = root.resolve()
        self.output_dir = (output_dir or self.root / RESULTS_RELATIVE).resolve()
        self.paths = self._tracked_paths()
        self.source_texts: dict[str, SourceText] = {}
        self.token_refs: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.loader_refs: list[dict[str, Any]] = []
        self._load_texts_and_reference_index()

    def _tracked_paths(self) -> list[str]:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.root), "ls-files", "-z"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            raw_paths = result.stdout.decode("utf-8", errors="strict").split("\0")
            paths = [normalize_path(path) for path in raw_paths if path]
        except (OSError, subprocess.CalledProcessError, UnicodeDecodeError):
            paths = [normalize_path(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_file()]
        return sorted(path for path in paths if not is_excluded(path))

    def _read_text(self, relative: str) -> str | None:
        path = self.root / relative
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if len(data) > 5_000_000 or b"\x00" in data[:8192]:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return data.decode("utf-8-sig")
            except UnicodeDecodeError:
                return None

    def _load_texts_and_reference_index(self) -> None:
        for relative in self.paths:
            suffix = Path(relative).suffix.lower()
            if suffix not in TEXT_EXTENSIONS and not under_relevant_root(relative):
                continue
            text = self._read_text(relative)
            if text is None:
                continue
            lines = tuple(text.splitlines())
            source = SourceText(relative, text, lines)
            self.source_texts[relative] = source
            for line_number, line in enumerate(lines, 1):
                for token in PATH_TOKEN_RE.findall(line):
                    normalized = token.strip(".,:;()[]{}<>\"'").replace("\\", "/").lower()
                    if len(normalized) < 3:
                        continue
                    refs = self.token_refs[normalized]
                    if len(refs) < 50:
                        refs.append({"path": relative, "line": line_number, "match": token[:200]})
                lower = line.lower()
                if any(marker in lower for marker in ("glob(", "rglob(", "listdir(", "iterdir(", "importlib", "getattr(", "pkgutil", "walk(")):
                    literals = [match.group("value") for match in STRING_LITERAL_RE.finditer(line)]
                    self.loader_refs.append({
                        "path": relative,
                        "line": line_number,
                        "code": line.strip()[:500],
                        "literals": [value.replace("\\", "/").lower() for value in literals],
                    })

    def source_fingerprint(self) -> str:
        digest = hashlib.sha256()
        for relative in self.paths:
            if relative.startswith(RESULTS_RELATIVE.as_posix() + "/"):
                continue
            path = self.root / relative
            try:
                data = path.read_bytes()
            except OSError:
                continue
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(data).digest())
        return digest.hexdigest()

    def locale_inventory(self) -> tuple[list[dict[str, Any]], dict[str, list[str]], dict[str, list[str]]]:
        locale_prefix = "module/config/i18n/"
        locale_paths = [
            path for path in self.paths
            if path.startswith(locale_prefix) and path.endswith(".json")
        ]
        locales: list[dict[str, Any]] = []
        key_sets: dict[str, set[str]] = {}
        for relative in locale_paths:
            try:
                data = json.loads((self.root / relative).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                locales.append({"path": relative, "locale": Path(relative).stem, "error": str(exc), "key_count": 0})
                key_sets[relative] = set()
                continue
            keys = {".".join(key) for key, value in flatten_json(data) if isinstance(value, str)}
            key_sets[relative] = keys
            locales.append({
                "path": relative,
                "locale": Path(relative).stem,
                "key_count": len(keys),
                "content_hash": sha256_bytes(json_bytes(data)),
            })
        all_keys = set().union(*key_sets.values()) if key_sets else set()
        missing = {path: sorted(all_keys - keys) for path, keys in key_sets.items() if all_keys - keys}
        extra: dict[str, list[str]] = {}
        if key_sets:
            intersection = set.intersection(*key_sets.values()) if key_sets else set()
            extra = {path: sorted(keys - intersection) for path, keys in key_sets.items() if keys - intersection}
        return sorted(locales, key=lambda item: item["path"]), missing, extra

    def inventory_ui_strings(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        locale_prefix = "module/config/i18n/"
        for relative, source in sorted(self.source_texts.items()):
            suffix = Path(relative).suffix.lower()
            if relative.startswith(locale_prefix) and suffix == ".json":
                try:
                    data = json.loads(source.text)
                except json.JSONDecodeError:
                    continue
                for key, value in flatten_json(data):
                    if not isinstance(value, str) or not value.strip():
                        continue
                    joined = ".".join(key)
                    classification = "user_help_text" if key and key[-1].lower() in {"help", "description", "tooltip"} else "user_ui_text"
                    records.append(self._ui_record(
                        relative, joined, "locale_json", value, classification,
                        generated=True, notes="Locale catalog value.",
                    ))
                continue
            if suffix in {".yaml", ".yml"} and relative.startswith("module/config/argument/"):
                for line_number, line in enumerate(source.lines, 1):
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#") or ":" not in stripped:
                        continue
                    key, value = stripped.split(":", 1)
                    value = value.strip().strip("'\"")
                    if not value or key.strip() not in {"name", "help", "description", "option", "value"}:
                        continue
                    classification = "user_help_text" if key.strip() in {"help", "description"} else "user_ui_text"
                    records.append(self._ui_record(
                        relative, line_number, "argument_yaml", value, classification,
                        generated=False, notes=f"YAML field: {key.strip()}.",
                    ))
                continue
            if suffix == ".py" and (relative.startswith("module/webui/") or relative in {"gui.py", "alas.py"}):
                records.extend(self._python_ui_records(relative, source))
            elif suffix in {".html", ".htm"}:
                for line_number, line in enumerate(source.lines, 1):
                    for match in HTML_TEXT_RE.finditer(line):
                        text = re.sub(r"\s+", " ", match.group(1)).strip()
                        if text and not text.startswith(("{", "$")) and not technical_only(text):
                            records.append(self._ui_record(relative, line_number, "html_text", text, "user_ui_text", False, "Visible HTML text candidate."))
            elif suffix in {".js", ".mjs", ".ts", ".tsx", ".jsx"} and "webui" in relative.lower():
                for line_number, line in enumerate(source.lines, 1):
                    for match in JS_UI_RE.finditer(line):
                        text = match.group("text").strip()
                        if text:
                            records.append(self._ui_record(relative, line_number, "javascript_ui", text, "user_ui_text", False, "JavaScript UI assignment/call."))
            elif suffix in {".ps1", ".psm1"}:
                for line_number, line in enumerate(source.lines, 1):
                    match = POWERSHELL_MESSAGE_RE.match(line)
                    if not match:
                        continue
                    body = match.group("body").strip()
                    text = self._extract_ps_message(body)
                    if not text:
                        continue
                    classification = "user_error_text" if match.group("kind").lower() in {"write-error", "throw"} else "user_ui_text"
                    records.append(self._ui_record(relative, line_number, "powershell_cli", text, classification, False, match.group("kind")))
        records.sort(key=lambda item: (item["path"], str(item["line_or_key"]), item["text"]))
        return records

    def _python_ui_records(self, relative: str, source: SourceText) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        try:
            tree = ast.parse(source.text, filename=relative)
        except SyntaxError:
            return records
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = call_name(node)
                tail = name.rsplit(".", 1)[-1].lower()
                if not (tail in UI_CALL_HINTS or any(hint in tail for hint in ("toast", "popup", "button", "label", "title"))):
                    continue
                candidates = list(node.args[:2]) + [keyword.value for keyword in node.keywords if keyword.arg in {"text", "label", "title", "message", "placeholder", "help", "description"}]
                for argument in candidates:
                    text = literal_text(argument, source.text).strip()
                    if not text or technical_only(text):
                        continue
                    records.append(self._ui_record(
                        relative, getattr(argument, "lineno", getattr(node, "lineno", 0)),
                        f"python_call:{name}", text, "user_ui_text", False,
                        "Hardcoded WebUI string candidate.",
                    ))
        return records

    def _ui_record(self, path: str, line_or_key: int | str, source_kind: str, text: str,
                   classification: str, generated: bool, notes: str) -> dict[str, Any]:
        return {
            "path": path,
            "line_or_key": line_or_key,
            "source_kind": source_kind,
            "text": text,
            "language_guess": language_guess(text),
            "subsystem": subsystem_for_path(path),
            "classification": classification,
            "runtime_visibility": "direct" if classification != "generated_output" else "generated",
            "generated": generated,
            "translation_required": translation_required(text, classification),
            "notes": notes,
        }

    def inventory_logs(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for relative, source in sorted(self.source_texts.items()):
            suffix = Path(relative).suffix.lower()
            if suffix == ".py" and under_relevant_root(relative):
                records.extend(self._python_log_records(relative, source))
            elif suffix in {".ps1", ".psm1"}:
                records.extend(self._powershell_log_records(relative, source))
        records.sort(key=lambda item: (item["path"], item["line"], item["call_kind"]))
        return records

    def _python_log_records(self, relative: str, source: SourceText) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        try:
            tree = ast.parse(source.text, filename=relative)
        except SyntaxError:
            return records
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = call_name(node)
                tail = name.rsplit(".", 1)[-1].lower()
                is_logger = tail in LOG_CALL_NAMES and ("logger" in name.lower() or tail == "print" or name.startswith("logging."))
                if not is_logger:
                    continue
                argument = node.args[0] if node.args else None
                text = literal_text(argument, source.text).strip() if argument is not None else ""
                if not text:
                    continue
                records.append(self._log_record(relative, getattr(node, "lineno", 0), name, text, "python_call"))
            elif isinstance(node, ast.Raise) and node.exc is not None:
                text = ""
                if isinstance(node.exc, ast.Call) and node.exc.args:
                    text = literal_text(node.exc.args[0], source.text).strip()
                elif isinstance(node.exc, ast.Constant) and isinstance(node.exc.value, str):
                    text = node.exc.value
                if text:
                    records.append(self._log_record(relative, getattr(node, "lineno", 0), "raise", text, "python_raise"))
        return records

    def _powershell_log_records(self, relative: str, source: SourceText) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for line_number, line in enumerate(source.lines, 1):
            match = POWERSHELL_MESSAGE_RE.match(line)
            if not match:
                continue
            text = self._extract_ps_message(match.group("body"))
            if text:
                records.append(self._log_record(relative, line_number, match.group("kind"), text, "powershell"))
        return records

    @staticmethod
    def _extract_ps_message(body: str) -> str:
        match = STRING_LITERAL_RE.search(body)
        if match:
            return match.group("value")
        body = body.strip()
        return body[:500] if body and not body.startswith(("@{", "$(")) else ""

    def _log_record(self, path: str, line: int, call_kind: str, text: str, source_kind: str) -> dict[str, Any]:
        raw = likely_external_raw(text)
        first_party = "external_raw" if raw and technical_only(text.replace("{…}", "")) else "first_party"
        return {
            "path": path,
            "line": line,
            "call_kind": call_kind,
            "source_kind": source_kind,
            "message_or_template": text,
            "subsystem": subsystem_for_path(path),
            "first_party_or_external": first_party,
            "language_guess": language_guess(text),
            "user_actionable": any(word in text.lower() for word in ("error", "failed", "cannot", "invalid", "please", "warning", "ошиб", "не удалось", "проверь")),
            "translation_required": first_party == "first_party" and translation_required(text, "first_party_log"),
            "raw_external_payload_preserved": raw,
            "notes": "Preserve raw external payload after localized context." if raw else "First-party message candidate.",
        }

    def _references_for_asset(self, relative: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        normalized = relative.lower()
        basename = Path(relative).name.lower()
        stem = Path(relative).stem.lower()
        candidates = {normalized, basename}
        if len(stem) >= 5:
            candidates.add(stem)
        refs: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for token in sorted(candidates):
            for ref in self.token_refs.get(token, []):
                key = (ref["path"], ref["line"])
                if ref["path"] == relative or key in seen:
                    continue
                seen.add(key)
                refs.append(ref)
                if len(refs) >= 20:
                    break
        asset_parent = PurePosixPath(relative).parent.as_posix().lower()
        specific_fragments = [
            part.lower()
            for part in PurePosixPath(relative).parts[:-1]
            if len(part) >= 3 and part.lower() not in {"assets", "asset", "module", "tests", "campaign", "doc", "docs"}
        ]
        dynamic: list[dict[str, Any]] = []
        for loader in self.loader_refs:
            if loader["path"] == relative:
                continue
            literals = loader["literals"]
            literal_match = False
            for literal in literals:
                base = re.sub(r"[?*\[].*$", "", literal).rstrip("/")
                if not base:
                    continue
                if asset_parent == base or asset_parent.startswith(base + "/"):
                    literal_match = True
                    break
            code_match = any(fragment in loader["code"].lower() for fragment in specific_fragments[-2:])
            if literal_match or code_match:
                dynamic.append({"path": loader["path"], "line": loader["line"], "code": loader["code"]})
            if len(dynamic) >= 10:
                break
        refs.sort(key=lambda item: (item["path"], item["line"], item["match"]))
        dynamic.sort(key=lambda item: (item["path"], item["line"], item["code"]))
        generated = [ref for ref in refs if any(token in ref["path"].lower() for token in ("generated", "button_extract", "config_updater"))]
        tests = [ref for ref in refs if ref["path"].startswith("tests/")]
        static = [ref for ref in refs if ref not in generated and ref not in tests]
        return static, dynamic, generated, tests

    def asset_manifest(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for relative in self.paths:
            if not is_asset_candidate(relative):
                continue
            path = self.root / relative
            try:
                data = path.read_bytes()
            except OSError:
                continue
            scope, markers = scope_markers(relative)
            static_refs, dynamic_refs, generated_refs, test_refs = self._references_for_asset(relative)
            status, manual, confidence, reason = status_from_evidence(scope, static_refs, dynamic_refs, generated_refs, test_refs)
            shared_candidate = scope in {"shared", "unknown", "multi_server"} or bool(static_refs or dynamic_refs)
            en_candidate = scope in {"en", "shared", "multi_server"} or any(
                any(token in ref["path"].lower() for token in ("en", "global", "server"))
                for ref in static_refs + dynamic_refs
            )
            records.append({
                "path": relative,
                "size_bytes": len(data),
                "extension": path.suffix.lower(),
                "content_hash_or_stable_fingerprint": sha256_bytes(data),
                "asset_type": asset_type(relative),
                "suspected_scope": scope,
                "language_or_server_markers": markers,
                "static_references": static_refs,
                "dynamic_loader_references": dynamic_refs,
                "generated_references": generated_refs,
                "test_references": test_refs,
                "shared_runtime_candidate": shared_candidate,
                "en_global_required_candidate": en_candidate,
                "deletable_candidate": status in {"probable_delete_candidate", "confirmed_delete_candidate"},
                "decision_status": status,
                "confidence": confidence,
                "reason": reason,
                "manual_review_required": manual,
            })
        records.sort(key=lambda item: item["path"])
        return records

    def dependency_map(self, locales: list[dict[str, Any]], missing: dict[str, list[str]], extra: dict[str, list[str]]) -> dict[str, Any]:
        evidence: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in LANGUAGE_SYMBOLS}
        for relative, source in sorted(self.source_texts.items()):
            for line_number, line in enumerate(source.lines, 1):
                for symbol in LANGUAGE_SYMBOLS:
                    if symbol in line and len(evidence[symbol]) < 100:
                        evidence[symbol].append({"path": relative, "line": line_number, "code": line.strip()[:500]})
        links = [
            self._dependency_link("UI locale", "translation loader", evidence, ("LANGUAGES", "Language", "BrowserLanguage"), "Stage 5: retain only ru-RU UI locale."),
            self._dependency_link("translation loader", "deploy Language", evidence, ("Language",), "Stage 5: migrate only Language key patch-wise."),
            self._dependency_link("deploy Language", "config generator", evidence, ("Language", "LANGUAGES"), "Stage 5: separate generated UI locale from game server."),
            self._dependency_link("config generator", "event-name source", evidence, ("event_name", "Event"), "Stage 5: preserve server-specific event names independently."),
            self._dependency_link("event-name source", "game server", evidence, ("SERVER_TO_LANG", "LANG_TO_SERVER", "ServerName"), "Stage 5: make game server explicit."),
            self._dependency_link("game server", "OCR profile/model", evidence, ("OcrModel", "OcrLanguage", "SERVER_TO_LANG"), "Stage 5/9: preserve EN/shared OCR fallback until runtime smoke."),
            self._dependency_link("OCR profile/model", "package/server options", evidence, ("PackageName", "ServerName"), "Stage 5/9: decouple options from UI locale."),
            self._dependency_link("package/server options", "assets", evidence, ("PackageName", "ServerName", "OcrModel"), "Stage 9: delete only after manifest evidence and EN smoke."),
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "current_locales": locales,
            "locale_key_mismatches": {"missing": missing, "extra": extra},
            "symbols": evidence,
            "links": links,
            "target_chain": [
                "UI locale", "translation loader", "deploy Language", "config generator",
                "event-name source", "game server", "OCR profile/model",
                "package/server options", "assets",
            ],
        }

    @staticmethod
    def _dependency_link(source: str, target: str, evidence: dict[str, list[dict[str, Any]]], symbols: tuple[str, ...], action: str) -> dict[str, Any]:
        refs: list[dict[str, Any]] = []
        for symbol in symbols:
            refs.extend(evidence.get(symbol, [])[:15])
        refs = sorted(refs, key=lambda item: (item["path"], item["line"]))[:30]
        return {
            "source": source,
            "target": target,
            "exists": bool(refs),
            "necessary_currently": bool(refs),
            "stage5_break_or_refactor": True,
            "evidence": refs,
            "required_tests": [action],
        }

    def terminology(self) -> dict[str, Any]:
        entries = [
            ("Task", "задача", [], [], "scheduler/UI", False, "Stable."),
            ("Scheduler", "планировщик", [], [], "scheduler", False, "Stable."),
            ("Campaign", "кампания", ["сюжетная кампания"], [], "game mode", False, "Context may require chapter/map wording."),
            ("Fleet", "флот", ["отряд"], ["флотилия"], "combat", False, "Use game context consistently."),
            ("Sortie", "боевой выход", ["выход"], [], "campaign action", False, "Manual product decision for compact buttons."),
            ("Operation Siren", "Операция «Сирена»", [], [], "official game mode", False, "Keep official capitalization."),
            ("Device", "устройство", [], [], "ADB", False, "Stable."),
            ("Emulator", "эмулятор", [], [], "ADB", False, "Stable."),
            ("Screenshot", "снимок экрана", ["скриншот"], [], "diagnostics", False, "Prefer formal wording in UI."),
            ("Control method", "метод управления", [], [], "device control", False, "Stable."),
            ("OCR", "OCR", ["распознавание текста"], [], "recognition", True, "Keep acronym; explain on first use."),
            ("Recognition", "распознавание", [], [], "OCR/templates", False, "Stable."),
            ("Restart", "перезапуск", ["перезапустить"], [], "process action", False, "Match noun/verb context."),
            ("Recovery", "восстановление", [], [], "error handling", False, "Stable."),
            ("Repair", "восстановление", ["ремонт"], [], "PowerShell command", True, "Keep command name Repair; translate explanation."),
            ("Build", "подготовка установки", ["сборка"], [], "PowerShell command", True, "Keep command name Build; avoid implying compilation."),
            ("Update", "обновление", [], [], "PowerShell command", True, "Keep command name Update in command references."),
            ("Instance", "профиль", ["экземпляр"], [], "WebUI/process", False, "Manual by context: profile for user-facing config, instance for process internals."),
            ("Config", "конфигурация", ["настройки"], [], "UI/internal", False, "Use settings for user controls; configuration for files/system."),
            ("Deploy", "конфигурация запуска", ["развёртывание"], [], "deploy.yaml", True, "Keep file/path identifiers."),
            ("Logger / log", "журнал", ["лог"], [], "diagnostics", False, "Prefer журнал in UI, log only in technical notes."),
            ("Event", "событие", ["ивент"], [], "game content", False, "Prefer событие unless official name requires otherwise."),
            ("Raid", "рейд", [], [], "game mode", False, "Manual verification against official RU terminology."),
            ("Commission", "комиссия", ["поручение"], [], "game task", False, "Manual product decision required."),
            ("Research", "исследование", [], [], "game task", False, "Stable."),
            ("Reward", "награда", [], [], "game task", False, "Stable."),
            ("Dorm", "общежитие", ["дорм"], [], "game feature", False, "Manual verification against community terminology."),
            ("Shop", "магазин", [], [], "game feature", False, "Stable."),
            ("Exercise", "учения", ["тренировка"], [], "PvP mode", False, "Manual verification against official terminology."),
            ("Tactical", "тактическое обучение", ["тактика"], [], "game feature", False, "Manual by exact screen context."),
            ("Meowfficer", "мяуфицер", ["офицер-кот"], [], "game proper name", False, "Manual product decision; community term likely preferable."),
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "entries": [
                {
                    "source_term": source,
                    "preferred_ru": preferred,
                    "allowed_alternatives": alternatives,
                    "forbidden_or_discouraged_variants": discouraged,
                    "context": context,
                    "keep_original": keep,
                    "notes": notes,
                }
                for source, preferred, alternatives, discouraged, context, keep, notes in entries
            ],
        }

    def allowlist(self) -> dict[str, Any]:
        categories = {
            "protocols_and_tools": ["ADB", "OCR", "HTTP", "HTTPS", "TCP", "UDP", "WebSocket", "Git", "Python", "PowerShell", "WebUI", "JSON", "YAML", "CSS", "HTML", "JavaScript"],
            "identifiers": ["class names", "function names", "module names", "exception types", "internal state keys", "package names"],
            "machine_values": ["paths", "URLs", "Git refs", "SHA hashes", "HTTP methods", "ADB serials", "exit codes", "command-line options"],
            "external_raw": ["stdout", "stderr", "traceback", "library exception text", "device/emulator raw output"],
            "proper_names": ["AzurPilot", "Azur Lane", "Operation Siren", "Meowfficer"],
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "categories": categories,
            "regex_hints": [
                r"https?://\\S+", r"[A-Za-z]:[\\\\/].+", r"/[A-Za-z0-9_.@+/-]+",
                r"[0-9a-f]{7,64}", r"--?[a-z0-9-]+", r"[A-Za-z0-9_.-]+\\.(?:exe|dll|py|json|yaml|yml)",
            ],
            "policy": "Allowlist applies only to technical/proper-name context; it does not exempt ordinary English sentences.",
        }

    def build_outputs(self) -> dict[str, bytes]:
        locales, missing, extra = self.locale_inventory()
        ui = self.inventory_ui_strings()
        logs = self.inventory_logs()
        assets = self.asset_manifest()
        dependency = self.dependency_map(locales, missing, extra)
        terminology = self.terminology()
        allowlist = self.allowlist()
        ui_columns = (
            "path", "line_or_key", "source_kind", "text", "language_guess", "subsystem",
            "classification", "runtime_visibility", "generated", "translation_required", "notes",
        )
        log_columns = (
            "path", "line", "call_kind", "source_kind", "message_or_template", "subsystem",
            "first_party_or_external", "language_guess", "user_actionable", "translation_required",
            "raw_external_payload_preserved", "notes",
        )
        decision_columns = (
            "path", "decision_status", "confidence", "reason", "manual_review_required",
            "suspected_scope", "asset_type",
        )
        en_columns = (
            "path", "suspected_scope", "asset_type", "decision_status", "reason",
            "static_reference_count", "dynamic_reference_count", "generated_reference_count",
        )
        manifest_columns = (
            "path", "size_bytes", "extension", "content_hash_or_stable_fingerprint", "asset_type",
            "suspected_scope", "language_or_server_markers", "static_references",
            "dynamic_loader_references", "generated_references", "test_references", "reference_counts",
            "shared_runtime_candidate", "en_global_required_candidate", "deletable_candidate",
            "decision_status", "confidence", "reason", "manual_review_required",
        )
        decisions = [
            {key: item[key] for key in decision_columns}
            for item in assets
        ]
        en_required = [
            {
                "path": item["path"],
                "suspected_scope": item["suspected_scope"],
                "asset_type": item["asset_type"],
                "decision_status": item["decision_status"],
                "reason": item["reason"],
                "static_reference_count": len(item["static_references"]),
                "dynamic_reference_count": len(item["dynamic_loader_references"]),
                "generated_reference_count": len(item["generated_references"]),
            }
            for item in assets
            if item["en_global_required_candidate"]
        ]
        review_assets = []
        for item in assets:
            if item["decision_status"] not in {"needs_manual_review", "probable_delete_candidate", "confirmed_delete_candidate"}:
                continue
            compact_item = dict(item)
            compact_item["static_references"] = item["static_references"][:3]
            compact_item["dynamic_loader_references"] = item["dynamic_loader_references"][:3]
            compact_item["generated_references"] = item["generated_references"][:3]
            compact_item["test_references"] = item["test_references"][:3]
            compact_item["reference_counts"] = {
                "static": len(item["static_references"]),
                "dynamic": len(item["dynamic_loader_references"]),
                "generated": len(item["generated_references"]),
                "tests": len(item["test_references"]),
            }
            review_assets.append(compact_item)
        full_manifest_payload = {"schema_version": SCHEMA_VERSION, "entries": assets}
        full_manifest_digest = sha256_bytes(compact_json_bytes(full_manifest_payload))
        summary = self._summary(locales, missing, ui, logs, assets)
        summary["full_asset_manifest_sha256"] = full_manifest_digest
        asset_manifest = tabular_payload(review_assets, manifest_columns)
        asset_manifest.update({
            "mode": "aggregate_with_review_candidates",
            "full_manifest_committed": False,
            "full_manifest_entry_count": len(assets),
            "full_manifest_sha256": full_manifest_digest,
            "reference_samples_per_kind": 3,
            "reproduction_command": "python -m dev_tools.russianization_audit --write --full-manifest .stage4/full_asset_manifest.json",
            "aggregate": {
                "decision_counts": summary["asset_decision_counts"],
                "scope_counts": summary["asset_scope_counts"],
                "bytes_total": summary["asset_bytes_total"],
            },
        })
        outputs: dict[str, bytes] = {
            "summary.json": json_bytes(summary),
            "ui_strings.json": compact_json_bytes(tabular_payload(ui, ui_columns)),
            "first_party_logs.json": compact_json_bytes(tabular_payload(logs, log_columns)),
            "asset_manifest.json": compact_json_bytes(asset_manifest),
            "locale_dependency_map.json": json_bytes(dependency),
            "terminology.json": json_bytes(terminology),
            "technical_allowlist.json": json_bytes(allowlist),
            "asset_decisions.json": compact_json_bytes(tabular_payload(decisions, decision_columns)),
            "en_global_required.json": compact_json_bytes(tabular_payload(en_required, en_columns)),
            "stage4_report.md": self._report(summary, dependency, assets, ui, logs).encode("utf-8"),
            "deploy_language_migration.md": self._migration_plan(dependency).encode("utf-8"),
            "stage5_9_test_matrix.md": self._test_matrix().encode("utf-8"),
        }
        return outputs

    def _summary(self, locales: list[dict[str, Any]], missing: dict[str, list[str]], ui: list[dict[str, Any]],
                 logs: list[dict[str, Any]], assets: list[dict[str, Any]]) -> dict[str, Any]:
        decision_counts = Counter(item["decision_status"] for item in assets)
        scope_counts = Counter(item["suspected_scope"] for item in assets)
        ui_subsystems = Counter(item["subsystem"] for item in ui)
        log_subsystems = Counter(item["subsystem"] for item in logs)
        return {
            "schema_version": SCHEMA_VERSION,
            "source_fingerprint": self.source_fingerprint(),
            "tracked_files_scanned": len(self.paths),
            "text_files_scanned": len(self.source_texts),
            "locale_files": len(locales),
            "locales": [item["locale"] for item in locales],
            "locale_files_with_missing_keys": len(missing),
            "ui_strings": len(ui),
            "ui_translation_required": sum(1 for item in ui if item["translation_required"]),
            "ui_by_subsystem": dict(sorted(ui_subsystems.items())),
            "first_party_log_messages": len(logs),
            "log_translation_required": sum(1 for item in logs if item["translation_required"]),
            "logs_by_subsystem": dict(sorted(log_subsystems.items())),
            "asset_entries": len(assets),
            "asset_decision_counts": dict(sorted(decision_counts.items())),
            "asset_scope_counts": dict(sorted(scope_counts.items())),
            "asset_bytes_total": sum(item["size_bytes"] for item in assets),
            "en_global_required_candidates": sum(1 for item in assets if item["en_global_required_candidate"]),
            "manual_review_assets": sum(1 for item in assets if item["manual_review_required"]),
            "probable_delete_candidates": decision_counts.get("probable_delete_candidate", 0),
            "confirmed_delete_candidates": decision_counts.get("confirmed_delete_candidate", 0),
        }

    def _report(self, summary: dict[str, Any], dependency: dict[str, Any], assets: list[dict[str, Any]],
                ui: list[dict[str, Any]], logs: list[dict[str, Any]]) -> str:
        decision = summary["asset_decision_counts"]
        scope = summary["asset_scope_counts"]
        locale_rows = "\n".join(
            f"| `{item['locale']}` | `{item['path']}` | {item['key_count']} |"
            for item in dependency["current_locales"]
        ) or "| — | — | 0 |"
        links = "\n".join(
            f"| {item['source']} → {item['target']} | {'да' if item['exists'] else 'не подтверждено'} | {len(item['evidence'])} |"
            for item in dependency["links"]
        )
        candidates = [item for item in assets if item["decision_status"] == "probable_delete_candidate"][:50]
        candidate_rows = "\n".join(
            f"| `{item['path']}` | {item['suspected_scope']} | {item['asset_type']} | {item['confidence']:.2f} |"
            for item in candidates
        ) or "| — | — | — | — |"
        ui_top = Counter(item["subsystem"] for item in ui).most_common()
        log_top = Counter(item["subsystem"] for item in logs).most_common()
        return f"""# Stage 4 — аудит русификации и карта зависимостей

## Границы

Этот отчёт создан read-only аудитором. Runtime locale, язык по умолчанию, WebUI, логи, OCR-модели, server logic и существующие assets не изменялись и не удалялись.

## Воспроизводимость

```text
uv run python -m dev_tools.russianization_audit --write
uv run python -m dev_tools.russianization_audit --check
```

`--check` генерирует результаты во временном каталоге и побайтово сравнивает их с committed baseline, не изменяя tracked tree.

## Итоговые counts

| Метрика | Значение |
|---|---:|
| Tracked files scanned | {summary['tracked_files_scanned']} |
| Text files scanned | {summary['text_files_scanned']} |
| Locale files | {summary['locale_files']} |
| UI string entries | {summary['ui_strings']} |
| UI translation required | {summary['ui_translation_required']} |
| First-party/direct log entries | {summary['first_party_log_messages']} |
| Log translation required | {summary['log_translation_required']} |
| Asset entries | {summary['asset_entries']} |
| Asset bytes represented | {summary['asset_bytes_total']} |
| EN/Global required candidates | {summary['en_global_required_candidates']} |
| Manual review assets | {summary['manual_review_assets']} |
| Probable delete candidates | {summary['probable_delete_candidates']} |
| Confirmed delete candidates | {summary['confirmed_delete_candidates']} |

Source fingerprint: `{summary['source_fingerprint']}`

## Locale inventory

| Locale | Path | String keys |
|---|---|---:|
{locale_rows}

Locale files with missing keys against union: **{summary['locale_files_with_missing_keys']}**.

## Locale / server / OCR dependency map

| Связь | Фактически найдена | Evidence entries |
|---|---|---:|
{links}

Архитектурный вывод: текущие связи должны разрываться только в Stage 5, сохраняя game server, event-name source, OCR profile и package options независимо от UI locale.

## Пользовательские строки

Разбиение по подсистемам: `{dict(ui_top)}`.

Inventory содержит путь, строку/ключ, источник, текст, language guess, classification, runtime visibility, generated flag и решение о необходимости перевода. Эвристика не считает любой ASCII-текст пользовательским английским: identifiers, paths, commands и technical values отделены.

## First-party логи

Разбиение по подсистемам: `{dict(log_top)}`.

Сырые stdout/stderr/traceback отмечаются отдельно и должны сохраняться без перевода. В будущих Stage русифицируется только first-party контекст вокруг них.

## Assets

Decision counts: `{decision}`.

Scope counts: `{scope}`.

`confirmed_delete_candidate` намеренно не присваивается на основании имени, CJK или суффикса. Наличие server marker без runtime evidence даёт максимум `probable_delete_candidate` и `manual_review_required: true`.

### Первые probable delete candidates

| Path | Scope | Type | Confidence |
|---|---|---|---:|
{candidate_rows}

Committed `asset_manifest.json` содержит агрегаты и review/delete findings с ограниченными evidence samples. Полный manifest воспроизводится командой из файла и сверяется по SHA-256. Решения: `asset_decisions.json`. Ресурсы EN/shared: `en_global_required.json`.

## Доказательные ограничения

- Статические ссылки извлекаются из tracked UTF-8 text files и отличаются от dynamic loader evidence.
- Glob/path-convention/importlib/getattr/listdir evidence помечается как dynamic и запрещает автоматический вывод об удалении.
- Тестовые ссылки отделены от runtime/generated references.
- Binary semantic contents не распознаются; спорные ресурсы остаются manual review.
- Реальная необходимость OCR fallback окончательно подтверждается только EN/Global runtime smoke на Stage 9.

## Следующие этапы

Stage 5 использует dependency map и migration plan; Stage 6 — UI inventory и terminology; Stage 7–8 — log inventory; Stage 9 — asset decisions и EN/Global keep list. Stage 4 ничего из этого не реализует.
"""

    def _migration_plan(self, dependency: dict[str, Any]) -> str:
        evidence_count = sum(len(link["evidence"]) for link in dependency["links"])
        return f"""# План миграции `Language` в пользовательском `config/deploy.yaml`

## Цель

На Stage 5 безопасно преобразовать старое значение `Language` в `ru-RU`, не перезаписывая неизвестные пользовательские ключи и не связывая UI locale с game server.

## Подтверждённая поверхность

Dependency map содержит {evidence_count} evidence entries по цепочке locale/server/OCR/package/assets. Конкретные файлы и строки находятся в `locale_dependency_map.json`.

## Контракт миграции

1. Прочитать существующий YAML без создания файла при обычном read path.
2. Если файл отсутствует, шаблон Stage 5 создаёт новый config с `Language: ru-RU`.
3. Если файл существует, изменить только скаляр `Language` patch-only механизмом, уже применяемым персональной веткой.
4. Сохранить порядок, неизвестные ключи, комментарии и все значения, не относящиеся к `Language`, насколько это обеспечивает существующий writer.
5. Не менять game server, package name, OCR model/profile и event-name source.
6. Значения `en-US`, `zh-CN`, `ja-JP`, `zh-TW`, `zh-MIAO`, пустое и неизвестное locale мигрируют в `ru-RU` с понятным first-party сообщением.
7. Повторный запуск при `Language: ru-RU` является no-op.
8. При ошибке парсинга не переписывать файл; вернуть русскую диагностическую ошибку и исходную exception detail.

## Обязательные regression fixtures

- каждый legacy locale;
- неизвестное locale;
- отсутствующий `Language`;
- дублирующийся/невалидный YAML;
- неизвестные nested keys;
- комментарии и нестандартный порядок;
- EN/Global server + foreign UI locale;
- повторный запуск.

## Запреты

Миграция не должна выполнять full dump поверх пользовательского файла, silent fallback на английский, смену server/OCR/package options или удаление неизвестных полей.
"""

    @staticmethod
    def _test_matrix() -> str:
        return """# Матрица тестов Stage 5–9

| Stage | Подсистема | Автоматические проверки | Ручная приёмка |
|---|---|---|---|
| 5 | locale loader | ru-RU only, no foreign fallback, browser fallback, key completeness | WebUI startup |
| 5 | deploy migration | patch-only Language, unknown keys preserved, idempotence | existing user config copy |
| 5 | server separation | EN server unchanged across locale migration | EN/Global profile open |
| 5 | generator | config/i18n regeneration leaves tree clean | none |
| 6 | WebUI | inventory reaches zero untranslated first-party UI items; render smoke fixtures | dark/light, long Russian labels |
| 6 | CLI/OOBE | hardcoded UI gate with technical allowlist | OOBE and error pages |
| 7 | deploy/process logs | first-party Russian context; raw stderr preserved | Start/Update/Repair/Build logs |
| 7 | config/lifecycle | message sequence preserved; only text changes | startup/shutdown |
| 8A | device/ADB/control | reconnect/timeouts/backend messages; raw ADB preserved | emulator/device smoke |
| 8B | OCR | model selection/fallback/template errors | EN OCR smoke |
| 8C | scheduler/tasks | queue/start/retry/stop sequence unchanged | safe task smoke |
| 8D | campaign/combat | event sequence and formatted values unchanged | safe campaign scenario |
| 8E | Operation Siren | AP/navigation/repair/shop sequence unchanged | isolated OS smoke |
| 9 | locale cleanup | no references to removed locale; generator-clean | WebUI startup |
| 9 | asset cleanup | missing-reference scan, glob/import/registry tests, button_extract | EN/Global/OCR smoke |
| 9 | package/server options | unsupported legacy profile yields explicit migration error | old profile fixture |

Permanent gates retained: Ruff syntax/static, Stage 3 regression suite, button/config regeneration, PowerShell Parser and PSScriptAnalyzer.
"""

    def write(self) -> dict[str, bytes]:
        outputs = self.build_outputs()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for filename, data in outputs.items():
            (self.output_dir / filename).write_bytes(data)
        return outputs

    def write_full_asset_manifest(self, path: Path) -> None:
        payload = {"schema_version": SCHEMA_VERSION, "entries": self.asset_manifest()}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(compact_json_bytes(payload))

    def check(self) -> list[str]:
        outputs = self.build_outputs()
        differences: list[str] = []
        for filename in RESULT_FILENAMES:
            expected = outputs[filename]
            path = self.output_dir / filename
            try:
                actual = path.read_bytes()
            except OSError:
                differences.append(f"missing: {path.relative_to(self.root) if path.is_relative_to(self.root) else path}")
                continue
            if actual != expected:
                differences.append(f"outdated: {path.relative_to(self.root) if path.is_relative_to(self.root) else path}")
        unexpected = sorted(
            path.name for path in self.output_dir.glob("*")
            if path.is_file() and path.name not in RESULT_FILENAMES
        ) if self.output_dir.exists() else []
        differences.extend(f"unexpected: {self.output_dir / name}" for name in unexpected)
        return differences


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Russianization inventory for AzurPilot Private RU Stage 4.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="Generate/update committed Stage 4 audit results.")
    mode.add_argument("--check", action="store_true", help="Compare generated results with committed baseline without writing.")
    parser.add_argument("--root", type=Path, default=None, help="Repository root; defaults to parent of dev_tools.")
    parser.add_argument("--output", type=Path, default=None, help="Output directory; defaults to dev_tools/russianization/results.")
    parser.add_argument("--full-manifest", type=Path, default=None, help="Optional path for the complete local asset manifest; never required by --check.")
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    root = (args.root or Path(__file__).resolve().parents[1]).resolve()
    output = args.output.resolve() if args.output is not None else root / RESULTS_RELATIVE
    engine = AuditEngine(root, output)
    if args.write:
        outputs = engine.write()
        summary = json.loads(outputs["summary.json"])
        if args.full_manifest is not None:
            full_manifest_path = args.full_manifest.resolve()
            engine.write_full_asset_manifest(full_manifest_path)
            print(f"Full local asset manifest written: {full_manifest_path}")
        print(
            "Stage 4 audit written: "
            f"UI={summary['ui_strings']}, logs={summary['first_party_log_messages']}, "
            f"assets={summary['asset_entries']}, locales={summary['locale_files']}"
        )
        return 0
    differences = engine.check()
    if differences:
        print("Stage 4 audit baseline is outdated:", file=sys.stderr)
        for difference in differences:
            print(f"- {difference}", file=sys.stderr)
        return 1
    print("Stage 4 audit baseline is current; check mode left the repository unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
