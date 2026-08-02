from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from dev_tools.russianization_audit import json_bytes
from dev_tools.stage7_gui_contract import GUI_BLOCKING_METRICS, build_gui_contract
from dev_tools.stage7_log_audit import (
    BLOCKING_METRICS,
    DEFAULT_OUTPUT_DIR,
    ROOT,
    Stage7LogAudit,
)
from dev_tools.stage7_semantic_delta_policy import apply_semantic_delta_policy
from dev_tools.stage7_semantic_diagnostics import collect_semantic_findings
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
    findings = collect_semantic_findings(audit)
    outputs["semantic-findings.json"] = (
        json.dumps(findings, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    metrics, delta_policy_errors = apply_semantic_delta_policy(metrics, findings)
    outputs, metrics, semantic_policy_errors = apply_stage7_policy(outputs, metrics)
    gui_outputs, gui_metrics, gui_contract_errors = build_gui_contract(
        ROOT, audit.base_sha
    )
    outputs.update(gui_outputs)
    metrics.update(gui_metrics)

    failures = [
        *delta_policy_errors,
        *semantic_policy_errors,
        *gui_contract_errors,
    ]
    failures.extend(
        f"{key}: {metrics[key]}"
        for key in (*BLOCKING_METRICS, *GUI_BLOCKING_METRICS)
        if metrics[key]
    )
    outputs["metrics.json"] = json_bytes(metrics)
    final_status = "FAIL" if failures else "PASS"
    report = outputs["report.md"].decode("utf-8")
    report = report.replace(
        "Статус: **PASS**", f"Статус: **{final_status}**", 1
    ).replace(
        "Статус: **FAIL**", f"Статус: **{final_status}**", 1
    )
    report += (
        "\n## Контракт gui.py\n\n"
        + "\n".join(f"- {key}: {value}" for key, value in gui_metrics.items())
        + "\n"
    )
    outputs["report.md"] = report.encode("utf-8")
    _write_outputs(args.output_dir, outputs)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1

    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "-v", *_existing_test_modules()],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "STAGE7_BASE_REF": audit.base_sha},
    )
    unittest_output = completed.stdout + completed.stderr
    (args.output_dir / "unittest.log").write_text(
        unittest_output,
        encoding="utf-8",
    )
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
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
