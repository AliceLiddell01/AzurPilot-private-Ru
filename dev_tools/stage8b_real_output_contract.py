from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from dev_tools.stage8b_semantic_policy import IMMUTABLE_STAGE8B_BASE_SHA, ROOT

_MAX_LABEL_RE = re.compile(r"^\s*MAX\s*:\s*(?=\d)", re.IGNORECASE)
_NUMERIC_SEPARATOR_RE = re.compile(r"(?<=\d)\s*([:/-])\s*(?=\d)")


class RealOutputContractError(RuntimeError):
    pass


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RealOutputContractError(
            completed.stderr.strip() or completed.stdout.strip() or "Git command failed"
        )
    return completed.stdout.strip()


def _run_probe(source_root: Path, output: Path) -> dict[str, Any]:
    probe = ROOT / "dev_tools" / "stage8b_real_output_probe.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(probe),
            "--source-root",
            str(source_root),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RealOutputContractError(
            "Real OCR probe failed for "
            f"{source_root}: {completed.stderr.strip() or completed.stdout.strip()}"
        )
    return json.loads(output.read_text(encoding="utf-8"))


def _canonical_approved_spacing(value: str) -> str:
    if not value:
        return value
    value = _MAX_LABEL_RE.sub("MAX:", value)
    return _NUMERIC_SEPARATOR_RE.sub(r"\1", value)


def _float_lists_equal(left: list[float], right: list[float], tolerance: float = 1e-7) -> bool:
    if len(left) != len(right):
        return False
    return all(math.isclose(a, b, rel_tol=tolerance, abs_tol=tolerance) for a, b in zip(left, right))


def _boxes_equal(left: Any, right: Any, tolerance: float = 1e-6) -> bool:
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return False
        return all(_boxes_equal(a, b, tolerance) for a, b in zip(left, right))
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(float(left), float(right), rel_tol=tolerance, abs_tol=tolerance)
    return left == right


def _recognition_findings(base: dict[str, Any], head: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    text_findings: list[dict[str, Any]] = []
    score_findings: list[dict[str, Any]] = []
    base_rows = base["recognition"]
    head_rows = head["recognition"]
    if len(base_rows) != len(head_rows):
        text_findings.append(
            {"kind": "recognition_count", "base": len(base_rows), "head": len(head_rows)}
        )
        return text_findings, score_findings
    for index, (base_row, head_row) in enumerate(zip(base_rows, head_rows)):
        if base_row["fixture_sha256"] != head_row["fixture_sha256"]:
            text_findings.append(
                {
                    "kind": "fixture_order",
                    "index": index,
                    "base": base_row["fixture_sha256"],
                    "head": head_row["fixture_sha256"],
                }
            )
            continue
        base_text = _canonical_approved_spacing(base_row["raw_text"])
        head_text = _canonical_approved_spacing(head_row["raw_text"])
        if base_text != head_text:
            text_findings.append(
                {
                    "kind": "recognized_text",
                    "index": index,
                    "fixture_sha256": base_row["fixture_sha256"],
                    "base": base_row["raw_text"],
                    "head": head_row["raw_text"],
                }
            )
        if not _float_lists_equal(base_row["scores"], head_row["scores"]):
            score_findings.append(
                {
                    "kind": "recognition_scores",
                    "index": index,
                    "fixture_sha256": base_row["fixture_sha256"],
                    "base": base_row["scores"],
                    "head": head_row["scores"],
                }
            )
        for field in ("word_results", "elapse"):
            if base_row[field] != head_row[field]:
                score_findings.append(
                    {
                        "kind": field,
                        "index": index,
                        "base": base_row[field],
                        "head": head_row[field],
                    }
                )
    return text_findings, score_findings


def _detection_findings(base: dict[str, Any], head: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    text_findings: list[dict[str, Any]] = []
    score_findings: list[dict[str, Any]] = []
    box_findings: list[dict[str, Any]] = []
    base_rows = base["detection"]
    head_rows = head["detection"]
    if len(base_rows) != len(head_rows):
        text_findings.append(
            {"kind": "detection_count", "base": len(base_rows), "head": len(head_rows)}
        )
        return text_findings, score_findings, box_findings
    for index, (base_row, head_row) in enumerate(zip(base_rows, head_rows)):
        if _canonical_approved_spacing(base_row["text"]) != _canonical_approved_spacing(head_row["text"]):
            text_findings.append(
                {
                    "kind": "detection_text",
                    "index": index,
                    "base": base_row["text"],
                    "head": head_row["text"],
                }
            )
        if not math.isclose(
            float(base_row["score"]),
            float(head_row["score"]),
            rel_tol=1e-7,
            abs_tol=1e-7,
        ):
            score_findings.append(
                {
                    "kind": "detection_score",
                    "index": index,
                    "base": base_row["score"],
                    "head": head_row["score"],
                }
            )
        if not _boxes_equal(base_row["box"], head_row["box"]):
            box_findings.append(
                {
                    "kind": "detection_box",
                    "index": index,
                    "base": base_row["box"],
                    "head": head_row["box"],
                }
            )
    return text_findings, score_findings, box_findings


def build_real_output_contract(output_dir: Path) -> tuple[dict[str, Any], dict[str, int]]:
    base_sha = IMMUTABLE_STAGE8B_BASE_SHA
    head_sha = _git("rev-parse", "HEAD")
    if head_sha == base_sha:
        raise RealOutputContractError("Real output contract forbids self-diff")
    _git("rev-parse", "--verify", base_sha)

    with tempfile.TemporaryDirectory(prefix="stage8b-real-output-") as directory:
        temp = Path(directory)
        base_worktree = temp / "base"
        _git("worktree", "add", "--detach", str(base_worktree), base_sha)
        try:
            base = _run_probe(base_worktree, temp / "base.json")
            head = _run_probe(ROOT, temp / "head.json")
        finally:
            try:
                _git("worktree", "remove", str(base_worktree), "--force")
            except RealOutputContractError:
                shutil.rmtree(base_worktree, ignore_errors=True)
                _git("worktree", "prune")

    fixture_hash_mismatch = base["fixtures"] != head["fixtures"]
    model_hash_mismatch = base["model"]["hashes"] != head["model"]["hashes"]
    environment_mismatch = base["environment"] != head["environment"]
    provider_mismatch = base["providers"] != head["providers"]
    recognition_text, recognition_scores = _recognition_findings(base, head)
    detection_text, detection_scores, detection_boxes = _detection_findings(base, head)
    text_findings = recognition_text + detection_text
    score_findings = recognition_scores + detection_scores
    order_findings: list[dict[str, Any]] = []
    base_detection_order = [row["text"] for row in base["detection"]]
    head_detection_order = [row["text"] for row in head["detection"]]
    if [
        _canonical_approved_spacing(value) for value in base_detection_order
    ] != [
        _canonical_approved_spacing(value) for value in head_detection_order
    ]:
        order_findings.append(
            {
                "kind": "detection_order",
                "base": base_detection_order,
                "head": head_detection_order,
            }
        )

    metrics = {
        "stage8b_real_output_text_mismatches": len(text_findings),
        "stage8b_real_output_score_mismatches": len(score_findings),
        "stage8b_real_output_box_mismatches": len(detection_boxes),
        "stage8b_real_output_order_mismatches": len(order_findings),
        "stage8b_real_fixture_hash_mismatches": int(fixture_hash_mismatch),
        "stage8b_real_model_hash_mismatches": int(model_hash_mismatch),
        "stage8b_real_environment_mismatches": int(environment_mismatch or provider_mismatch),
    }
    status = "PASS" if not any(metrics.values()) else "FAIL"
    payload = {
        "status": status,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "isolated_checkouts": True,
        "real_cpu_inference": True,
        "fixture_count": len(head["fixtures"]),
        "fixtures_equal": not fixture_hash_mismatch,
        "model_hashes_equal": not model_hash_mismatch,
        "environment_equal": not environment_mismatch,
        "provider_equal": not provider_mismatch,
        "values_equal_except_approved_spacing": not text_findings,
        "approved_behavioral_delta": {
            "name": "compact_numeric_spacing",
            "canonicalization": "MAX label and separators between digits only",
        },
        "base_model_hashes": base["model"]["hashes"],
        "head_model_hashes": head["model"]["hashes"],
        "base_provider": base["providers"],
        "head_provider": head["providers"],
        "recognition_findings": recognition_text + recognition_scores,
        "detection_findings": detection_text + detection_scores + detection_boxes,
        "order_findings": order_findings,
        "base": base,
        "head": head,
        "metrics": metrics,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "real-output-contract.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload, metrics
