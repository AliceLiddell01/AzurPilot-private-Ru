from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Iterable


SCOPE_PREFIX = Path("module/device")
SCOPE_FILES = (Path("module/webui/api.py"),)
BINARY_NAME_PARTS = {
    "binary",
    "bitmap",
    "buffer",
    "bytearray",
    "bytes",
    "frame",
    "h264",
    "image",
    "packet",
    "payload",
    "png",
    "raw_frame",
    "raw_image",
    "screenshot",
    "video",
}
SAFE_METADATA_ATTRIBUTES = {
    "backend",
    "channels",
    "dtype",
    "format",
    "fps",
    "height",
    "limit",
    "method",
    "nbytes",
    "resolution",
    "shape",
    "size",
    "status",
    "threshold",
    "timer",
    "width",
}
SAFE_METADATA_CALLS = {"len", "type"}
SAFE_TECHNICAL_NAMES = {"adb_binary"}
LENGTH_GUARDED_BINARY_NAMES = {"array", "blob", "body", "content", "data", "response", "stream"}
SAFE_METADATA_NAME_SUFFIXES = {
    "backend",
    "channels",
    "count",
    "dtype",
    "format",
    "fps",
    "height",
    "length",
    "limit",
    "method",
    "nbytes",
    "resolution",
    "shape",
    "size",
    "status",
    "threshold",
    "timer",
    "width",
}
CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


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


def _normalized_name(value: str) -> str:
    leaf = value.rsplit(".", 1)[-1]
    snake = CAMEL_BOUNDARY_RE.sub("_", leaf)
    return snake.lower().replace("-", "_")


def _name_parts(value: str) -> set[str]:
    normalized = _normalized_name(value)
    return {normalized, *(part for part in normalized.split("_") if part)}


def _is_binary_name(value: str) -> bool:
    normalized = _normalized_name(value)
    if normalized in SAFE_TECHNICAL_NAMES:
        return False
    if any(normalized.endswith(f"_{suffix}") for suffix in SAFE_METADATA_NAME_SUFFIXES):
        return False
    parts = _name_parts(value)
    if parts & BINARY_NAME_PARTS:
        return True
    return any(
        marker in normalized
        for marker in ("screenshot", "rawframe", "raw_image", "h264", "base64")
    )


def _length_guard_names(test: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(test):
        if not isinstance(node, ast.Call) or _call_name(node.func) != "len":
            continue
        if len(node.args) != 1:
            continue
        name = _call_name(node.args[0])
        if name:
            leaf = name.rsplit(".", 1)[-1]
            if _is_binary_name(name) or leaf.lower() in LENGTH_GUARDED_BINARY_NAMES:
                names.add(name)
                names.add(leaf)
    return names


def _is_forced_binary(node: ast.AST, forced_binary_names: set[str]) -> bool:
    name = _call_name(node)
    if not name:
        return False
    return name in forced_binary_names or name.rsplit(".", 1)[-1] in forced_binary_names


def _binary_references(
    node: ast.AST,
    *,
    forced_binary_names: set[str],
    protected: bool = False,
) -> list[str]:
    findings: list[str] = []

    if isinstance(node, ast.Call):
        call_name = _call_name(node.func)
        leaf = call_name.rsplit(".", 1)[-1]
        if leaf in SAFE_METADATA_CALLS:
            for argument in node.args:
                findings.extend(
                    _binary_references(
                        argument,
                        forced_binary_names=forced_binary_names,
                        protected=True,
                    )
                )
            for keyword in node.keywords:
                findings.extend(
                    _binary_references(
                        keyword.value,
                        forced_binary_names=forced_binary_names,
                        protected=True,
                    )
                )
            return findings

    if isinstance(node, ast.Attribute) and node.attr in SAFE_METADATA_ATTRIBUTES:
        findings.extend(
            _binary_references(
                node.value,
                forced_binary_names=forced_binary_names,
                protected=True,
            )
        )
        return findings

    if isinstance(node, (ast.Name, ast.Attribute)) and not protected:
        name = _call_name(node)
        if _is_forced_binary(node, forced_binary_names) or _is_binary_name(name):
            findings.append(name)
            return findings

    for child in ast.iter_child_nodes(node):
        findings.extend(
            _binary_references(
                child,
                forced_binary_names=forced_binary_names,
                protected=protected,
            )
        )
    return findings


def _logger_binary_references(
    node: ast.Call,
    *,
    forced_binary_names: set[str],
) -> list[str]:
    references: list[str] = []
    for argument in node.args:
        references.extend(
            _binary_references(
                argument,
                forced_binary_names=forced_binary_names,
            )
        )
    for keyword in node.keywords:
        keyword_references = _binary_references(
            keyword.value,
            forced_binary_names=forced_binary_names,
        )
        if keyword.arg and _is_binary_name(keyword.arg) and not keyword_references:
            value_name = _call_name(keyword.value)
            keyword_references.append(value_name or f"<keyword:{keyword.arg}>")
        references.extend(keyword_references)
    return references


class _BinaryLogVisitor(ast.NodeVisitor):
    def __init__(self, path: str):
        self.path = path
        self.forced_binary_names: set[str] = set()
        self.findings: list[dict[str, Any]] = []

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        previous = self.forced_binary_names
        self.forced_binary_names = previous | _length_guard_names(node.test)
        for statement in node.body:
            self.visit(statement)
        self.forced_binary_names = previous
        for statement in node.orelse:
            self.visit(statement)

    def visit_Call(self, node: ast.Call) -> None:
        call_kind = _call_name(node.func)
        if call_kind.startswith("logger."):
            references = sorted(
                set(
                    _logger_binary_references(
                        node,
                        forced_binary_names=self.forced_binary_names,
                    )
                )
            )
            if references:
                self.findings.append(
                    {
                        "kind": "binary_payload_log",
                        "path": self.path,
                        "line": node.lineno,
                        "call_kind": call_kind,
                        "references": references,
                        "evidence": (
                            "Logger arguments directly reference a binary-payload-shaped value; "
                            "log only metadata such as byte count, format, dimensions or backend."
                        ),
                    }
                )
        self.generic_visit(node)


def find_binary_payload_log_findings(root: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for file in _scope_files(root):
        relative = file.relative_to(root).as_posix()
        source = file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
        visitor = _BinaryLogVisitor(relative)
        visitor.visit(tree)
        findings.extend(visitor.findings)
    return findings
