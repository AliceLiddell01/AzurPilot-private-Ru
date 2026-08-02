from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from dev_tools.stage7_gui_stable_policy import apply_gui_stable_policy
from dev_tools.stage7_log_audit import (
    BLOCKING_METRICS,
    DEFAULT_OUTPUT_DIR,
    ROOT,
    Stage7LogAudit,
)
from dev_tools.stage7_semantic_policy import apply_stage7_policy


TEST_MODULES = (
    "tests.test_stage7_log_audit",
    "tests.test_stage7_deploy_logs",
    "tests.test_stage7_process_lifecycle_logs",
    "tests.test_stage7_webui_traceback_rendering",
    "tests.test_stage7_powershell_logs",
    "tests.test_stage7_stable_contracts",
)


def _existing_test_modules() -> list[str]:
    result = []
    for module in TEST_MODULES:
        path = ROOT / (module.replace(".", "/") + ".py")
        if path.is_file():
            result.append(module)
    return result


def _write_outputs(output_dir: Path, outputs: dict[str, bytes]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, content in outputs.items():
        (output_dir / name).write_bytes(content)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка Definition of Done Stage 7")
    parser.add_argument("--base-ref")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)

    audit = Stage7LogAudit(ROOT, base_ref=args.base_ref)
    outputs, metrics = audit.build()
    outputs, metrics, gui_policy_errors = apply_gui_stable_policy(outputs, metrics)
    outputs, metrics, semantic_policy_errors = apply_stage7_policy(outputs, metrics)
    _write_outputs(args.output_dir, outputs)

    failures = [*gui_policy_errors, *semantic_policy_errors]
    failures.extend(
        f"{key}: {metrics[key]}"
        for key in BLOCKING_METRICS
        if metrics[key]
    )
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1

    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "-v", *_existing_test_modules()],
        cwd=ROOT,
        check=False,
        env={**__import__("os").environ, "STAGE7_BASE_REF": audit.base_sha},
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
