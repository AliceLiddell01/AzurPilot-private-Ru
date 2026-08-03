from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from dev_tools.stage8a_binary_log_audit import find_binary_payload_log_findings
from dev_tools.stage8a_control_flow_policy import apply_stage8a_control_flow_policy
from dev_tools.stage8a_device_log_audit import (
    BLOCKING_METRICS,
    DEFAULT_OUTPUT_DIR,
    ROOT,
    Stage8ADeviceLogAudit,
)
from dev_tools.stage8a_semantic_policy import IMMUTABLE_STAGE8A_BASE_SHA

TEST_MODULES = (
    "tests.test_stage8a_device_log_audit",
    "tests.test_stage8a_binary_log_audit",
    "tests.test_stage8a_control_flow_policy",
    "tests.test_stage8a_semantic_contract",
    "tests.test_stage8a_stage7_policy_bridge",
    "tests.test_stage8a_device_acceptance",
)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_outputs(output_dir: Path, outputs: dict[str, bytes]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, content in outputs.items():
        (output_dir / name).write_bytes(content)


def _existing_test_modules() -> list[str]:
    return [
        module
        for module in TEST_MODULES
        if (ROOT / (module.replace(".", "/") + ".py")).is_file()
    ]


def _effective_base_ref(requested: str | None) -> str:
    if requested and requested != IMMUTABLE_STAGE8A_BASE_SHA:
        raise RuntimeError(
            "Stage 8A baseline immutable. Изменение SHA требует явного policy review "
            "в dev_tools/stage8a_semantic_policy.py."
        )
    return IMMUTABLE_STAGE8A_BASE_SHA


def _apply_binary_payload_audit(
    outputs: dict[str, bytes],
    metrics: dict[str, Any],
    findings: list[dict[str, Any]],
) -> tuple[dict[str, bytes], dict[str, Any]]:
    outputs = dict(outputs)
    metrics = dict(metrics)
    metric_name = "stage8a_binary_payload_log_findings"
    metrics[metric_name] = len(findings)

    semantic_findings = json.loads(outputs["semantic-findings.json"])
    semantic_findings.extend(findings)
    outputs["semantic-findings.json"] = _json_bytes(semantic_findings)
    outputs["metrics.json"] = _json_bytes(metrics)

    contract = json.loads(outputs["contract.json"])
    contract["binary_payload_log_contract_preserved"] = not findings
    outputs["contract.json"] = _json_bytes(contract)

    status = "FAIL" if any(metrics.get(key) for key in BLOCKING_METRICS) else "PASS"
    report_lines = outputs["report.md"].decode("utf-8").splitlines()
    updated_metric = False
    for index, line in enumerate(report_lines):
        if line.startswith("Статус: **"):
            report_lines[index] = f"Статус: **{status}**"
        if line.startswith(f"- {metric_name}:"):
            report_lines[index] = f"- {metric_name}: {len(findings)}"
            updated_metric = True
    if not updated_metric:
        report_lines.extend(("", f"- {metric_name}: {len(findings)}"))
    outputs["report.md"] = ("\n".join(report_lines) + "\n").encode("utf-8")
    return outputs, metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка Definition of Done Stage 8A")
    parser.add_argument("--base-ref")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)

    try:
        base_ref = _effective_base_ref(args.base_ref)
        audit = Stage8ADeviceLogAudit(ROOT, base_ref=base_ref)
        outputs, metrics = audit.build()
        outputs, metrics, _ = apply_stage8a_control_flow_policy(
            outputs,
            metrics,
            root=ROOT,
            base_sha=audit.base_sha,
        )
        binary_findings = find_binary_payload_log_findings(ROOT)
        outputs, metrics = _apply_binary_payload_audit(
            outputs,
            metrics,
            binary_findings,
        )
    except Exception as error:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        failure = {
            "status": "FAIL",
            "error": str(error),
            "immutable_base_sha": IMMUTABLE_STAGE8A_BASE_SHA,
        }
        (args.output_dir / "contract.json").write_text(
            json.dumps(failure, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    failures = [
        f"{key}: {metrics[key]}"
        for key in BLOCKING_METRICS
        if metrics.get(key)
    ]
    if metrics["remaining_log_translation_count"] <= 0:
        failures.append(
            "remaining_log_translation_count должен оставаться ненулевым до завершения Stage 8B–8E"
        )

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
        env={**os.environ, "STAGE8A_BASE_REF": audit.base_sha},
    )
    unittest_output = completed.stdout + completed.stderr
    (args.output_dir / "unittest.log").write_text(unittest_output, encoding="utf-8")
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    if completed.returncode:
        return completed.returncode

    print(
        "Stage 8A verifier: PASS "
        f"(translated={metrics['stage8a_translated']}, "
        f"reviewed={metrics['stage8a_reviewed_technical']}, "
        f"remaining={metrics['remaining_log_translation_count']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
