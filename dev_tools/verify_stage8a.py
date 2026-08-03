from __future__ import annotations

import argparse
import json
import os
import shutil
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
from dev_tools.stage8a_exception_context_audit import (
    find_bare_exception_context_findings,
)
from dev_tools.stage8a_evidence_policy import (
    BACKEND_CI_COVERAGE,
    EXTERNAL_CONTRACTS,
    SECURITY_REQUIREMENTS,
    scenario_evidence,
)
from dev_tools.stage8a_semantic_policy import IMMUTABLE_STAGE8A_BASE_SHA

EXCEPTION_CONTEXT_METRIC = "stage8a_bare_exception_context_findings"
FINAL_BLOCKING_METRICS = (*BLOCKING_METRICS, EXCEPTION_CONTEXT_METRIC)

TEST_MODULES = (
    "tests.test_stage8a_device_log_audit",
    "tests.test_stage8a_binary_log_audit",
    "tests.test_stage8a_binary_log_audit_arguments",
    "tests.test_stage8a_exception_context_audit",
    "tests.test_stage8a_control_flow_policy",
    "tests.test_stage8a_semantic_contract",
    "tests.test_stage8a_stage7_policy_bridge",
    "tests.test_stage8a_device_acceptance",
    "tests.test_stage8a_scenario_contracts",
    "tests.test_stage8a_runtime_scenario_matrix",
    "tests.test_stage8a_evidence_policy",
    "tests.test_stage8a_security_review",
    "tests.test_stage8a_external_contracts",
)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_outputs(output_dir: Path, outputs: dict[str, bytes]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, content in outputs.items():
        (output_dir / name).write_bytes(content)


def _copy_review_file(output_dir: Path, source: Path) -> None:
    relative = source.relative_to(ROOT)
    target = output_dir / "review-source" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _write_review_source_snapshot(output_dir: Path) -> None:
    device_root = ROOT / "module" / "device"
    for source in sorted(device_root.rglob("*.py")):
        _copy_review_file(output_dir, source)

    explicit = (
        ROOT / "module" / "webui" / "api.py",
        ROOT / "module" / "webui" / "app.py",
        ROOT / "module" / "webui" / "fastapi.py",
        ROOT / "gui.py",
        ROOT / "uv.lock",
        ROOT / "pyproject.toml",
    )
    for source in explicit:
        if source.is_file():
            _copy_review_file(output_dir, source)

    for pattern in ("stage8a_*.py", "verify_stage8a.py"):
        for source in sorted((ROOT / "dev_tools").glob(pattern)):
            _copy_review_file(output_dir, source)
    for source in sorted((ROOT / "tests").glob("test_stage8a_*.py")):
        _copy_review_file(output_dir, source)
    review_root = ROOT / ".codex" / "reviews"
    if review_root.is_dir():
        for source in sorted(review_root.glob("PR*_STAGE8A_*.md")):
            _copy_review_file(output_dir, source)


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


def _apply_findings_metric(
    outputs: dict[str, bytes],
    metrics: dict[str, Any],
    findings: list[dict[str, Any]],
    *,
    metric_name: str,
    contract_key: str,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    outputs = dict(outputs)
    metrics = dict(metrics)
    metrics[metric_name] = len(findings)

    semantic_findings = json.loads(outputs["semantic-findings.json"])
    semantic_findings.extend(findings)
    outputs["semantic-findings.json"] = _json_bytes(semantic_findings)
    outputs["metrics.json"] = _json_bytes(metrics)

    contract = json.loads(outputs["contract.json"])
    contract[contract_key] = not findings
    outputs["contract.json"] = _json_bytes(contract)

    status = "FAIL" if any(metrics.get(key) for key in FINAL_BLOCKING_METRICS) else "PASS"
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



def _apply_evidence_outputs(
    outputs: dict[str, bytes],
    metrics: dict[str, Any],
) -> dict[str, bytes]:
    outputs = dict(outputs)
    rows = scenario_evidence()
    outputs["scenario-evidence.json"] = _json_bytes(
        {
            "status": "PENDING_TEST_EXECUTION",
            "requirements": len(rows),
            "evidence": rows,
        }
    )
    outputs["backend-coverage.json"] = _json_bytes(
        {
            "status": "CI_EVIDENCE_ONLY",
            "ci_coverage": BACKEND_CI_COVERAGE,
            "external_acceptance": {
                "channel": "artifacts/stage8a/device-acceptance.json",
                "required_on_exact_head": True,
                "included_in_ci_artifact": False,
                "reason": "GitHub-hosted CI has no user emulator or local backend configuration.",
            },
        }
    )
    outputs["security-review.json"] = _json_bytes(
        {
            "status": "PENDING_TEST_EXECUTION",
            "checklist": SECURITY_REQUIREMENTS,
            "blocking_metrics": {
                "binary_payload_logs": metrics.get(
                    "stage8a_binary_payload_log_findings", 0
                ),
                "bare_exception_context": metrics.get(
                    EXCEPTION_CONTEXT_METRIC, 0
                ),
                "secret_findings": metrics.get("stage8a_secret_findings", 0),
            },
            "residual_limitations": [
                "Remote live preview/control is denied until a separate authenticated transport is designed.",
                "Legacy macOS emulator shell execution is unchanged by Stage 8A and remains a documented pre-existing boundary.",
            ],
        }
    )
    outputs["external-contracts.json"] = _json_bytes(
        {
            "status": "PENDING_TEST_EXECUTION",
            "contracts": EXTERNAL_CONTRACTS,
            "review": ".codex/reviews/PR20_STAGE8A_EXTERNAL_CONTRACTS.md",
        }
    )
    contract = json.loads(outputs["contract.json"])
    contract["backend_coverage"] = list(BACKEND_CI_COVERAGE)
    contract["external_acceptance_evidence"] = {
        "status": "SEPARATE_SANITIZED_REPORT_REQUIRED",
        "path": "artifacts/stage8a/device-acceptance.json",
        "exact_head_required": True,
    }
    contract["scenario_evidence_count"] = len(rows)
    contract["security_review_status"] = "PENDING_TEST_EXECUTION"
    outputs["contract.json"] = _json_bytes(contract)
    return outputs


def _verify_scenario_fixtures_executed(
    output_dir: Path,
    unittest_output: str,
) -> None:
    rows = scenario_evidence()
    missing = [
        row["fixture_test"]
        for row in rows
        if f"({row['fixture_test']})" not in unittest_output
    ]
    evidence = {
        "status": "PASS" if not missing else "FAIL",
        "requirements": len(rows),
        "executed": len(rows) - len(missing),
        "missing": missing,
        "fixtures": [row["fixture_test"] for row in rows],
    }
    (output_dir / "scenario-execution.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if missing:
        raise RuntimeError(
            "Stage 8A scenario fixtures were not executed: " + ", ".join(missing)
        )


def _mark_evidence_tests_passed(output_dir: Path) -> None:
    for name in (
        "scenario-evidence.json",
        "security-review.json",
        "external-contracts.json",
    ):
        path = output_dir / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["status"] = "PASS"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    contract_path = output_dir / "contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["security_review_status"] = "PASS"
    contract["scenario_evidence_status"] = "PASS"
    contract["external_contract_review_status"] = "PASS"
    contract_path.write_text(
        json.dumps(contract, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


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
        outputs, metrics = _apply_findings_metric(
            outputs,
            metrics,
            find_binary_payload_log_findings(ROOT),
            metric_name="stage8a_binary_payload_log_findings",
            contract_key="binary_payload_log_contract_preserved",
        )
        outputs, metrics = _apply_findings_metric(
            outputs,
            metrics,
            find_bare_exception_context_findings(ROOT),
            metric_name=EXCEPTION_CONTEXT_METRIC,
            contract_key="exception_first_party_context_preserved",
        )
        outputs = _apply_evidence_outputs(outputs, metrics)
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
        _write_review_source_snapshot(args.output_dir)
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    failures = [
        f"{key}: {metrics[key]}"
        for key in FINAL_BLOCKING_METRICS
        if metrics.get(key)
    ]
    if metrics["remaining_log_translation_count"] <= 0:
        failures.append(
            "remaining_log_translation_count должен оставаться ненулевым до завершения Stage 8B–8E"
        )

    _write_outputs(args.output_dir, outputs)
    _write_review_source_snapshot(args.output_dir)
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

    try:
        _verify_scenario_fixtures_executed(args.output_dir, unittest_output)
    except RuntimeError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1

    _mark_evidence_tests_passed(args.output_dir)

    print(
        "Stage 8A verifier: PASS "
        f"(translated={metrics['stage8a_translated']}, "
        f"reviewed={metrics['stage8a_reviewed_technical']}, "
        f"remaining={metrics['remaining_log_translation_count']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
