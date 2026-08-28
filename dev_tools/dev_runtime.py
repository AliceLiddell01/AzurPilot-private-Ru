"""Тонкая точка входа для проверки Dev Runtime Foundation."""

from __future__ import annotations

import argparse
import json
import sys

from module.dev_runtime import DevResult, DevSessionManager, DevStatusKind


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Локальная диагностика AzurPilot Dev Runtime"
    )
    parser.add_argument(
        "command",
        choices=("preflight", "doctor", "status", "start", "stop", "recover", "smoke"),
        help="Операция над фиксированной dev-сессией профиля ap",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    manager = DevSessionManager()
    try:
        result = getattr(manager, args.command)()
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        result = DevResult(
            ok=False,
            code="DEV_CLI_FAILED",
            message=f"Операция Dev Runtime завершилась исключением {type(exc).__name__}",
            state=DevStatusKind.FAILED.value,
        )

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="strict")
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
