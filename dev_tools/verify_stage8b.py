from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from dev_tools.stage8b_acceptance_contract import build_acceptance_contract
from dev_tools.stage8b_evidence_policy import BACKEND_COVERAGE, scenario_evidence
from dev_tools.stage8b_model_scope import build_model_scope
from dev_tools.stage8b_ocr_log_audit import Stage8BOcrLogAudit
from dev_tools.stage8b_output_contract import build_output_contract
from dev_tools.stage8b_real_output_contract import build_real_output_contract
from dev_tools.stage8b_security_audit import build_security_review
from dev_tools.stage8b_semantic_policy import (
    APPROVED_BEHAVIOR_RUNTIME_PATHS,
    BLOCKING_METRICS,
    DEFAULT_OUTPUT_DIR,
    IMMUTABLE_STAGE8B_BASE_SHA,
    PROMPT_SCENARIO_COUNT,
    ROOT,
    SECURITY_RUNTIME_PATHS,
    TRANSLATION_ONLY_RUNTIME_PATHS,
)

TEST_MODULES = (
    "tests.test_stage8b_semantic_contract",
    "tests.test_stage8b_security_review",
    "tests.test_stage8b_output_contract",
    "tests.test_stage8b_ocr_acceptance",
    "tests.test_stage8b_rpc_runtime",
    "tests.test_stage8b_runtime_scenario_matrix",
    "tests.test_stage8b_prompt_scenario_matrix",
    "tests.test_stage8b_reparse_privacy",
)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_outputs(output_dir: Path, outputs: dict[str, bytes]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, content in outputs.items():
        (output_dir / name).write_bytes(content)


def _effective_base_ref(requested: str | None) -> str:
    if requested and requested != IMMUTABLE_STAGE8B_BASE_SHA:
        raise RuntimeError("Immutable Stage 8B baseline нельзя менять без policy review.")
    return IMMUTABLE_STAGE8B_BASE_SHA


def _git_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _runtime_contract() -> dict[str, Any]:
    import importlib.metadata
    from rapidocr.ch_ppocr_det import TextDetOutput

    try:
        from rapidocr.ch_ppocr_cls import TextClsOutput
    except ImportError:
        TextClsOutput = None
    from rapidocr.ch_ppocr_rec.typings import TextRecOutput
    from rapidocr.utils.output import RapidOCROutput

    def fields(value):
        if value is None:
            return None
        if dataclasses.is_dataclass(value):
            return [
                {"name": field.name, "type": str(field.type), "default": repr(field.default)}
                for field in dataclasses.fields(value)
            ]
        return {
            "annotations": {
                key: str(item)
                for key, item in getattr(value, "__annotations__", {}).items()
            },
            "dataclass": False,
        }

    return {
        "rapidocr_version": importlib.metadata.version("rapidocr"),
        "imports": {
            "TextDetOutput": f"{TextDetOutput.__module__}.{TextDetOutput.__name__}",
            "TextClsOutput": None if TextClsOutput is None else (
                f"{TextClsOutput.__module__}.{TextClsOutput.__name__}"
            ),
            "TextRecOutput": f"{TextRecOutput.__module__}.{TextRecOutput.__name__}",
            "RapidOCROutput": f"{RapidOCROutput.__module__}.{RapidOCROutput.__name__}",
        },
        "fields": {
            "TextDetOutput": fields(TextDetOutput),
            "TextClsOutput": fields(TextClsOutput),
            "TextRecOutput": fields(TextRecOutput),
            "RapidOCROutput": fields(RapidOCROutput),
        },
        "reviewed_members": [
            "boxes",
            "txts",
            "scores",
            "word_results",
            "imgs",
            "img_list",
            "elapse",
        ],
    }


def _scenario_outputs() -> tuple[dict[str, bytes], int]:
    rows = scenario_evidence()
    payload = {
        "status": "PENDING_TEST_EXECUTION",
        "requirements": len(rows),
        "prompt_requirement": PROMPT_SCENARIO_COUNT,
        "evidence": rows,
    }
    return {
        "scenario-evidence.json": _json_bytes(payload),
        "backend-coverage.json": _json_bytes(
            {
                "status": "CI_AND_REAL_FIXTURE_COVERAGE",
                "coverage": BACKEND_COVERAGE,
                "real_acceptance": {
                    "required": True,
                    "exact_head_required": True,
                    "visual_confirmation_required": True,
                    "path": "artifacts/stage8b/ocr-acceptance.json",
                    "included_in_ci_artifact": False,
                    "status": "PENDING_USER_PASS",
                },
            }
        ),
    }, len(rows)


def _verify_scenarios(output_dir: Path, unittest_output: str) -> dict[str, Any]:
    rows = scenario_evidence()
    fixtures = [row["fixture_test"] for row in rows]
    duplicates = sorted({fixture for fixture in fixtures if fixtures.count(fixture) > 1})
    missing = [fixture for fixture in fixtures if f"({fixture})" not in unittest_output]
    result = {
        "status": "PASS" if not (missing or duplicates) else "FAIL",
        "requirements": len(rows),
        "executed": len(rows) - len(missing),
        "missing": missing,
        "duplicates": duplicates,
        "unexpected": [],
        "fixtures": fixtures,
    }
    (output_dir / "scenario-execution.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return result


def _required_gate_contract(output_dir: Path) -> tuple[dict[str, Any], dict[str, int]]:
    workflow = ROOT / ".github/workflows/stage8b-validation.yml"
    source = workflow.read_text(encoding="utf-8")
    required_tokens = (
        "stage8b-required-gate:",
        "needs: stage8b-verifier",
        "if: always()",
        "needs.stage8b-verifier.result",
        "Stage 8B required gate",
    )
    findings = [
        {"kind": "workflow_gate_token_missing", "token": token}
        for token in required_tokens
        if token not in source
    ]
    payload = {
        "status": "PASS" if not findings else "FAIL",
        "workflow": workflow.relative_to(ROOT).as_posix(),
        "job": "stage8b-required-gate",
        "note": (
            "This validates a fail-closed final job in repository code. "
            "GitHub branch-ruleset attachment remains a repository setting."
        ),
        "findings": findings,
    }
    metrics = {"stage8b_required_gate_findings": len(findings)}
    (output_dir / "required-gate.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload, metrics


def _update_report(
    output_dir: Path,
    metrics: dict[str, Any],
    status: str,
    head_sha: str,
) -> None:
    lines = [
        "# Stage 8B — OCR и распознавание",
        "",
        f"Статус: **{status}**",
        f"Immutable base: `{IMMUTABLE_STAGE8B_BASE_SHA}`",
        f"Exact head: `{head_sha}`",
        "",
        "## Метрики",
        *[f"- {key}: {value}" for key, value in sorted(metrics.items())],
        "",
        "## Контракты",
        "- Scope: OCR core и только OCR-owned функции campaign/OS/device.",
        "- Models: только Global/English registries, settings, assets и benchmark rows.",
        "- Scenario matrix: 171 уникальный executable scenario ID из Stage 8B prompt.",
        "- Output: synthetic contract плюс реальный CPU inference на bundled fixtures в двух isolated worktrees.",
        "- OCR RPC: loopback-only и фиксированный ndarray wire format без pickle.",
        "- Debug images: explicit opt-in, вне Git root, symlink/junction/reparse-safe.",
        "- Acceptance: provider/cache/process/temp evidence измеряется, а не декларируется.",
        "- Real Windows/MuMu acceptance: отдельный exact-head visual user gate.",
    ]
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _review_snapshot(output_dir: Path) -> None:
    target = output_dir / "review-source"
    if target.exists():
        shutil.rmtree(target)
    for relative_root, patterns in (
        (ROOT / "dev_tools", ("stage8b_*.py", "verify_stage8b.py")),
        (ROOT / "tests", ("test_stage8b_*.py",)),
        (ROOT / "module" / "ocr", ("*.py",)),
    ):
        for pattern in patterns:
            for source in sorted(relative_root.glob(pattern)):
                destination = target / source.relative_to(ROOT)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка Definition of Done Stage 8B")
    parser.add_argument("--base-ref")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    head_sha = _git_head()

    try:
        base_ref = _effective_base_ref(args.base_ref)
        outputs, metrics = Stage8BOcrLogAudit(ROOT, base_ref=base_ref).build()
        scenario_outputs, scenario_count = _scenario_outputs()
        outputs.update(scenario_outputs)
        metrics["stage8b_scenario_requirements"] = scenario_count
        metrics["stage8b_scenario_count_mismatches"] = int(
            scenario_count != PROMPT_SCENARIO_COUNT
        )
        outputs["rapidocr-contract.json"] = _json_bytes(_runtime_contract())
        _write_outputs(output_dir, outputs)

        security_review, security_metrics = build_security_review(output_dir)
        metrics.update(security_metrics)
        output_contract, output_metrics = build_output_contract(output_dir)
        metrics.update(output_metrics)
        real_output_contract, real_output_metrics = build_real_output_contract(output_dir)
        metrics.update(real_output_metrics)
        model_scope, model_scope_metrics = build_model_scope(output_dir)
        metrics.update(model_scope_metrics)
        acceptance_contract, acceptance_metrics = build_acceptance_contract(output_dir)
        metrics.update(acceptance_metrics)
        required_gate, required_gate_metrics = _required_gate_contract(output_dir)
        metrics.update(required_gate_metrics)

        approved_delta_status = "PASS" if all(
            contract["status"] == "PASS"
            for contract in (
                security_review,
                output_contract,
                real_output_contract,
                model_scope,
                acceptance_contract,
                required_gate,
            )
        ) else "FAIL"
        approved_delta = {
            "status": approved_delta_status,
            "whole_change_is_translation_only": False,
            "translation_only_runtime_paths": list(TRANSLATION_ONLY_RUNTIME_PATHS),
            "approved_behavior_runtime_paths": list(APPROVED_BEHAVIOR_RUNTIME_PATHS),
            "security_runtime_paths": list(SECURITY_RUNTIME_PATHS),
            "security_deltas": [
                "OCR debug output is explicit opt-in, atomic and reparse-safe outside Git root.",
                "OCR RPC is loopback-only and uses a bounded ndarray wire format without pickle.",
                "Acceptance disables and observes vendor EP download/update state.",
            ],
            "approved_behavioral_deltas": [
                "remove non-Global Chinese/Japanese OCR model registries, settings and assets",
                "benchmark only Global/English model versions with explicit metadata",
                "azur_lane removes false whitespace for MAX and numeric separators only",
            ],
            "runtime_behavior_equivalent_except_approved_deltas": (
                output_contract["status"] == "PASS"
                and real_output_contract["status"] == "PASS"
            ),
            "behavioral_contract": {
                "compared": [
                    "real recognized text",
                    "real scores",
                    "real boxes",
                    "result order",
                    "English model hashes",
                    "provider order",
                    "thresholds",
                    "alphabets",
                    "postprocess",
                    "cache key",
                    "queue result",
                    "compact numeric spacing",
                ],
                "approved_behavior_exceptions": [
                    "English-only model scope",
                    "English benchmark matrix",
                    "compact numeric spacing",
                ],
                "security_exceptions": ["debug output", "RPC transport"],
            },
        }
        (output_dir / "approved-delta.json").write_text(
            json.dumps(approved_delta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        failure = {
            "status": "FAIL",
            "error": str(exc),
            "immutable_base_sha": IMMUTABLE_STAGE8B_BASE_SHA,
            "head_sha": head_sha,
        }
        (output_dir / "metrics.json").write_text(
            json.dumps(failure, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _review_snapshot(output_dir)
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "-v", *TEST_MODULES],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "STAGE8B_BASE_REF": IMMUTABLE_STAGE8B_BASE_SHA},
    )
    unittest_output = completed.stdout + completed.stderr
    (output_dir / "unittest.log").write_text(unittest_output, encoding="utf-8")
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)

    scenario_execution = _verify_scenarios(output_dir, unittest_output)
    metrics["stage8b_scenario_executed"] = scenario_execution["executed"]
    metrics["stage8b_scenario_missing"] = len(scenario_execution["missing"])
    scenario_evidence_path = output_dir / "scenario-evidence.json"
    scenario_payload = json.loads(scenario_evidence_path.read_text(encoding="utf-8"))
    scenario_payload["status"] = scenario_execution["status"]
    scenario_evidence_path.write_text(
        json.dumps(scenario_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    failures = [
        f"{key}: {metrics.get(key)}"
        for key in BLOCKING_METRICS
        if metrics.get(key)
    ]
    if metrics.get("remaining_log_translation_count", 0) <= 0:
        failures.append("remaining_log_translation_count должен быть ненулевым до Stage 8C–8E")
    if completed.returncode:
        failures.append(f"unittest return code: {completed.returncode}")
    if scenario_execution["status"] != "PASS":
        failures.append("scenario execution incomplete")

    (output_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    status = "PASS" if not failures else "FAIL"
    _update_report(output_dir, metrics, status, head_sha)
    _review_snapshot(output_dir)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print(
        "Stage 8B verifier: PASS "
        f"(translated={metrics['stage8b_translated']}, "
        f"scenarios={metrics['stage8b_scenario_executed']}/"
        f"{metrics['stage8b_scenario_requirements']}, real_inference=PASS)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
