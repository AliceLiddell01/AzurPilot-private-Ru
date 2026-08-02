from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from dev_tools.stage7_log_audit import DEFAULT_OUTPUT_DIR, ROOT, Stage7LogAudit


TEST_MODULES = (
    "tests.test_stage7_log_audit",
    "tests.test_stage7_deploy_logs",
    "tests.test_stage7_process_lifecycle_logs",
    "tests.test_stage7_webui_traceback_rendering",
    "tests.test_stage7_powershell_logs",
)


def _existing_test_modules() -> list[str]:
    result = []
    for module in TEST_MODULES:
        path = ROOT / (module.replace(".", "/") + ".py")
        if path.is_file():
            result.append(module)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка Definition of Done Stage 7")
    parser.add_argument("--base-ref")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "gui.py", args.output_dir / "source-gui.py")

    audit = Stage7LogAudit(ROOT, base_ref=args.base_ref)
    metrics = audit.write(args.output_dir)
    failures = audit.check()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1

    modules = _existing_test_modules()
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "-v", *modules],
        cwd=ROOT,
        check=False,
    )
    if completed.returncode:
        return completed.returncode

    generated_metrics = json.loads(
        (args.output_dir / "metrics.json").read_text(encoding="utf-8")
    )
    print(
        "Stage 7 verifier: PASS "
        f"(translated={generated_metrics['stage7_translated']}, "
        f"reviewed={generated_metrics['stage7_reviewed_technical']}, "
        f"logs_remaining={generated_metrics['remaining_log_translation_count']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
