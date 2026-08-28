"""Тонкая точка входа для проверки Dev Runtime Foundation."""

from __future__ import annotations

import argparse
import json

from module.dev_runtime import DevSessionManager


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
    result = getattr(manager, args.command)()
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
