from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable

from dev_tools.stage6_ui_audit import (
    EXCEPTIONS_PATH,
    METRICS_PATH,
    REPORT_PATH,
    ROOT,
    Stage6Audit,
    parse_args,
)

RETIRED_NON_EN_OCR_PREFIXES = (
    "Optimization.OcrModelVersionChinese.",
    "Optimization.OcrModelVersionJapanese.",
    "Optimization.OcrModelVersionTraditionalChinese.",
)


class ActiveStage6Audit(Stage6Audit):
    """Stage 6 audit over the currently reachable Russian UI catalog."""

    def __init__(self, root: Path = ROOT) -> None:
        super().__init__(root)
        self.en_flat = {
            key: value
            for key, value in self.en_flat.items()
            if not key.startswith(RETIRED_NON_EN_OCR_PREFIXES)
        }


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    audit = ActiveStage6Audit()
    if args.write:
        details = audit.write()
        failures = [key for key, value in details.items() if value]
    else:
        failures = audit.check()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("Stage 6 active UI audit: PASS")
    return 0


__all__ = (
    "ActiveStage6Audit",
    "EXCEPTIONS_PATH",
    "METRICS_PATH",
    "REPORT_PATH",
    "RETIRED_NON_EN_OCR_PREFIXES",
    "ROOT",
)


if __name__ == "__main__":
    raise SystemExit(main())
