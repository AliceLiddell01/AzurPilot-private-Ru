"""Быстрый CI-gate целостности first-party MCP bundle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from azurpilot.tooling.contracts import exit_code_for
from azurpilot.tooling.errors import ToolingError
from azurpilot.tooling.mcp import McpSourceReconciler


def _status_mark(ok: bool) -> str:
    """Вернуть предпочитаемый символ статуса с fallback для старой консоли."""
    mark = "✓" if ok else "✗"
    encoding = getattr(sys.stdout, "encoding", None)
    if encoding:
        try:
            mark.encode(encoding)
        except UnicodeEncodeError:
            return "[OK]" if ok else "[ERROR]"
    return mark


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Проверить целостность first-party MCP bundle без runtime mutation."
    )
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        build = McpSourceReconciler().check(arguments.repository_root)
    except ToolingError as error:
        payload = {
            "ok": False,
            "code": error.code.value,
            "message": error.message,
        }
        if arguments.as_json:
            print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        else:
            print(f"{_status_mark(False)} {error.code.value}: {error.message}")
        return int(exit_code_for(error.code).value)
    payload = {
        "ok": True,
        "code": "MCP_COMPATIBILITY_READY",
        "bundle_revision": build.bundle.bundle_revision,
        "plugin_version": build.bundle.plugin_version,
        "servers": {
            name: build.bundle.servers[name].version
            for name in ("azurpilot-dev", "azurpilot-game")
        },
    }
    if arguments.as_json:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        print(f"{_status_mark(True)} First-party MCP bundle согласован с исходным кодом.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
