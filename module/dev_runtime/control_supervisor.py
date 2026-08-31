"""Фиксированная точка входа независимого Dev Runtime control supervisor."""

from __future__ import annotations

import argparse
import sys

from module.dev_runtime.control import RuntimeControlManager
from module.dev_runtime.contracts import DevEnvironment


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dev Runtime control supervisor")
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args(argv)
    try:
        environment = DevEnvironment.current()
        result = RuntimeControlManager(environment).execute(args.operation_id)
    except Exception:
        # Supervisor обязан завершить процесс без traceback/секретов в MCP stdio.
        return 1
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
