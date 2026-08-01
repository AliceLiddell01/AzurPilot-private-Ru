from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from dev_tools.stage6_ui_audit import METRICS_PATH, ROOT, Stage6Audit


TEST_MODULES = (
    "tests.test_stage5_locale_runtime",
    "tests.test_stage6_ui_russianization",
)


def main() -> int:
    failures = Stage6Audit(ROOT).check()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1

    command = [sys.executable, "-m", "unittest", "-v", *TEST_MODULES]
    completed = subprocess.run(command, cwd=ROOT, check=False)
    if completed.returncode:
        return completed.returncode

    metrics = json.loads(Path(METRICS_PATH).read_text(encoding="utf-8"))
    print(
        "Stage 6 verifier: PASS "
        f"(translated={metrics['translated_active_ui']}, "
        f"reviewed={metrics['catalog_candidates_reviewed']}, "
        f"logs_remaining={metrics['remaining_log_translation_count']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
