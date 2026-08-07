from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BASE_SHA = "47dd60bfc549338b92a40f82a2d0d32d62541e43"
PRODUCTION_FILES = (
    "gui.py",
    "module/daemon/benchmark.py",
    "module/daemon/game_manager.py",
    "module/daemon/ocr_benchmark.py",
    "module/daemon/os_daemon.py",
    "module/daemon/screenshot_interval_benchmark.py",
    "module/daemon/uncensored.py",
    "module/logger.py",
    "module/server_checker.py",
)


class _StringValueScrubber(ast.NodeTransformer):
    """Ignore only string payloads while preserving AST node shape."""

    def visit_Constant(self, node: ast.Constant):
        if isinstance(node.value, str):
            node.value = "<STRING>"
        return node


def _normalized_ast(source: str) -> str:
    tree = ast.parse(source)
    tree = _StringValueScrubber().visit(tree)
    ast.fix_missing_locations(tree)
    return ast.dump(tree, annotate_fields=True, include_attributes=False)


def _ensure_base_commit() -> None:
    present = subprocess.run(
        ["git", "cat-file", "-e", f"{BASE_SHA}^{{commit}}"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if present.returncode == 0:
        return
    if os.environ.get("GITHUB_ACTIONS") != "true":
        pytest.skip("base commit is not available in this local shallow checkout")
    subprocess.run(
        ["git", "fetch", "--no-tags", "--depth=1", "origin", BASE_SHA],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def _base_source(path: str) -> str:
    completed = subprocess.run(
        ["git", "show", f"{BASE_SHA}:{path}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return completed.stdout.decode("utf-8")


def test_production_ast_diff_is_string_value_only():
    _ensure_base_commit()
    mismatches = []
    for path in PRODUCTION_FILES:
        before = _normalized_ast(_base_source(path))
        after = _normalized_ast((ROOT / path).read_text(encoding="utf-8"))
        if before != after:
            mismatches.append(path)
    assert mismatches == []
