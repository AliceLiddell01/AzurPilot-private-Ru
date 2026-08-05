from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Iterable

SCOPE_PREFIX = Path("module/device")
SCOPE_FILES = (Path("module/webui/api.py"),)
LOGGER_METHODS = {"debug", "info", "warning", "error", "critical", "exception"}
EXCEPTION_NAMES = {"e", "err", "error", "exc", "exception"}
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
DEVICE_WEBUI_OWNERS = (
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


def _is_device_owned_webui(owner: str) -> bool:
    return any(
        owner == prefix or owner.startswith(prefix)
        for prefix in DEVICE_WEBUI_OWNERS
    )


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parts: list[str] = []
        current: ast.AST = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        return ".".join(reversed(parts))
    return ""


def _scope_files(root: Path) -> Iterable[Path]:
    device_root = root / SCOPE_PREFIX
    if device_root.is_dir():
        yield from sorted(device_root.rglob("*.py"))
    for relative in SCOPE_FILES:
        path = root / relative
        if path.is_file():
            yield path


def _static_text(node: ast.AST) -> str:
    parts: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            parts.append(child.value)
    return " ".join(parts)


def _references_name(node: ast.AST, name: str) -> bool:
    return any(isinstance(child, ast.Name) and child.id == name for child in ast.walk(node))


def _logger_call(node: ast.Call) -> str | None:
    call_name = _call_name(node.func)
    if not call_name.startswith("logger."):
        return None
    method = call_name.rsplit(".", 1)[-1]
    return call_name if method in LOGGER_METHODS else None


class _ExceptionContextVisitor(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.owner_stack: list[str] = []
        self.exception_stack: list[str | None] = []
        self.findings: list[dict[str, Any]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.owner_stack.append(node.name)
        self.generic_visit(node)
        self.owner_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.owner_stack.append(node.name)
        self.generic_visit(node)
        self.owner_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self.exception_stack.append(node.name if isinstance(node.name, str) else None)
        for statement in node.body:
            self.visit(statement)
        self.exception_stack.pop()

    def visit_Lambda(self, node: ast.Lambda) -> None:
        if not self.exception_stack:
            self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        call_kind = _logger_call(node)
        if call_kind is None:
            self.generic_visit(node)
            return

        values = [*node.args, *(keyword.value for keyword in node.keywords)]
        referenced_names = {
            child.id
            for value in values
            for child in ast.walk(value)
            if isinstance(child, ast.Name)
        }
        active_exception = self.exception_stack[-1] if self.exception_stack else None
        exception_names = referenced_names & EXCEPTION_NAMES
        if active_exception and active_exception in referenced_names:
            exception_names.add(active_exception)
        references_exception = bool(exception_names)
        if not references_exception and not (
            self.exception_stack and call_kind.endswith(".exception")
        ):
            self.generic_visit(node)
            return
        exception_name = sorted(exception_names)[0] if exception_names else "<active exception>"
        static_text = " ".join(_static_text(value) for value in values)
        has_russian_context = bool(CYRILLIC_RE.search(static_text))
        owner = ".".join(self.owner_stack)

        if (
            static_text
            and not has_russian_context
            and self.path == "module/webui/api.py"
            and not _is_device_owned_webui(owner)
        ):
            self.generic_visit(node)
            return

        method = call_kind.rsplit(".", 1)[-1]
        if references_exception and not has_russian_context:
            self.findings.append(
                {
                    "kind": "bare_external_exception",
                    "path": self.path,
                    "line": node.lineno,
                    "function_owner": owner,
                    "call_kind": call_kind,
                    "exception_name": exception_name,
                    "evidence": (
                        "Logger call preserves an exception payload but provides no Russian "
                        "first-party context in the same message."
                    ),
                }
            )
        elif method == "exception" and not has_russian_context:
            self.findings.append(
                {
                    "kind": "dynamic_message_without_first_party_context",
                    "path": self.path,
                    "line": node.lineno,
                    "function_owner": owner,
                    "call_kind": call_kind,
                    "exception_name": exception_name,
                    "evidence": (
                        "logger.exception must add Russian first-party context while preserving "
                        "the traceback contract."
                    ),
                }
            )
        self.generic_visit(node)


def find_bare_exception_context_findings(root: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for file in _scope_files(root):
        relative = file.relative_to(root).as_posix()
        source = file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
        visitor = _ExceptionContextVisitor(relative)
        visitor.visit(tree)
        findings.extend(visitor.findings)
    return findings
