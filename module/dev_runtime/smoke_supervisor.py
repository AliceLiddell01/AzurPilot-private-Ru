"""Фиксированная точка входа отдельного процесса supervisor для одного SmokeRun."""

from __future__ import annotations

import argparse
import logging
import sys

from module.dev_runtime.smoke import SmokeRunManager, _identifier


def _arguments(argv: list[str] | None = None) -> str:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--smoke-id", required=True)
    parsed, unknown = parser.parse_known_args(argv)
    if unknown:
        raise ValueError("supervisor принимает только фиксированный аргумент --smoke-id")
    return _identifier(parsed.smoke_id, field_name="smoke_id")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        smoke_id = _arguments(argv)
    except (SystemExit, ValueError):
        return 2
    SmokeRunManager().run_supervisor(smoke_id)
    return 0


if __name__ == "__main__":  # pragma: no cover — проверяется интеграционными тестами subprocess
    raise SystemExit(main())


__all__ = ["main"]
