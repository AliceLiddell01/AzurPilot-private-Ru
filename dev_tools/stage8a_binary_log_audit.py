from __future__ import annotations

import ast
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
    "nbytes",
    "resolution",
    "shape",
    "size",
    "status",
    "width",
}
SAFE_METADATA_CALLS = {"len", "type"}


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


def _name_parts(value: str) -> set[str]:
    lowered = value.lower()
    parts = {lowered}
    normalized = lowered.replace("-", "_")
    parts.update(part for part in normalized.split("_") if part)
    return parts


def _is_binary_name(value: str) -> bool:
    parts = _name_parts(value)
    if parts & BINARY_NAME_PARTS:
        return True
    return any(
        marker in value.lower()
        for marker in ("screenshot", "rawframe", "raw_image", "h264", "base64")
    )


def _binary_references(node: ast.AST, *, protected: bool = False) -> list[str]:
    findings: list[str] = []

    if isinstance(node, ast.Call):
        call_name = _call_name(node.func)
        leaf = call_name.rsplit(".", 1)[-1]
        if leaf in SAFE_METADATA_CALLS:
            for argument in node.args:
                findings.extend(_binary_references(argument, protected=True))
            for keyword in node.keywords:
                findings.extend(_binary_references(keyword.value, protected=True))
            return findings

    if isinstance(node, ast.Attribute) and node.attr in SAFE_METADATA_ATTRIBUTES:
        findings.extend(_binary_references(node.value, protected=True))
        return findings

    if isinstance(node, ast.Name) and _is_binary_name(node.id) and not protected:
        findings.append(node.id)
    elif isinstance(node, ast.Attribute):
        dotted = _call_name(node)
        if _is_binary_name(node.attr) and not protected:
            findings.append(dotted or node.attr)

    for child in ast.iter_child_nodes(node):
        findings.extend(_binary_references(child, protected=protected))
    return findings


def find_binary_payload_log_findings(root: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for file in _scope_files(root):
        relative = file.relative_to(root).as_posix()
        source = file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call_kind = _call_name(node.func)
            if not call_kind.startswith("logger.") or not node.args:
                continue
            references = sorted(set(_binary_references(node.args[0])))
            if not references:
                continue
            findings.append(
                {
                    "kind": "binary_payload_log",
                    "path": relative,
                    "line": node.lineno,
                    "call_kind": call_kind,
                    "references": references,
                    "evidence": (
                        "Logger message directly references a binary-payload-shaped value; "
                        "log only metadata such as byte count, format, dimensions or backend."
                    ),
                }
            )
    return findings
