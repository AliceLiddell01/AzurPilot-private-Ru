"""Общие AST-помощники для проверки абсолютных и относительных импортов."""

from __future__ import annotations

import ast
from pathlib import Path


def absolute_import_candidates(
    root: Path, path: Path, node: ast.AST
) -> tuple[str, ...]:
    """Разрешить импорт в абсолютные имена относительно проверяемого файла."""

    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    if not isinstance(node, ast.ImportFrom):
        return ()

    if node.level == 0:
        if not node.module:
            return ()
        base = node.module
    else:
        relative = path.resolve().relative_to(root.resolve()).with_suffix("")
        parts = list(relative.parts)
        package = parts[:-1]
        keep = len(package) - node.level + 1
        if keep < 0:
            return ()
        base_parts = package[:keep]
        if node.module:
            base_parts.extend(node.module.split("."))
        base = ".".join(base_parts)

    candidates = [base] if base else []
    candidates.extend(
        f"{base}.{alias.name}" if base else alias.name
        for alias in node.names
        if alias.name != "*"
    )
    return tuple(candidates)


def imports_for_path(root: Path, path: Path) -> set[str]:
    """Собрать абсолютные кандидаты всех импортов Python-файла."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        candidate
        for node in ast.walk(tree)
        for candidate in absolute_import_candidates(root, path, node)
    }


__all__ = ["absolute_import_candidates", "imports_for_path"]
