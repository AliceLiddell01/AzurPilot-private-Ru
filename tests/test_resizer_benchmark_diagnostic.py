"""Temporary CI diagnostic for the non-native resizer benchmark.

This intentionally fails so GitHub Actions preserves a compact decision payload
in pytest output. It is removed/replaced before the PR can be mergeable.
"""

import json

import pytest

from tools.benchmark_resizer_stage4 import run_benchmark


def _worst_detector_rows(payload):
    result = {}
    rows = payload["detector_measurements"]
    for candidate in payload["summary"]:
        candidate_rows = [row for row in rows if row["candidate"] == candidate]
        margin_rows = [row for row in candidate_rows if row["margin"] is not None]
        result[candidate] = {
            "min_correct": min(candidate_rows, key=lambda row: row["correct_similarity"]),
            "min_threshold_margin": min(
                candidate_rows,
                key=lambda row: row["threshold_margin"],
            ),
            "min_correct_vs_wrong_margin": min(
                margin_rows,
                key=lambda row: row["margin"],
            ),
        }
    return result


def test_emit_resizer_benchmark():
    payload = run_benchmark(timing_iterations=12)
    compact = {
        "summary": payload["summary"],
        "timings": payload["timings"],
        "worst_detector_rows": _worst_detector_rows(payload),
    }
    pytest.fail(
        "RESIZER_BENCHMARK_DECISION_JSON="
        + json.dumps(compact, ensure_ascii=False, separators=(",", ":"), default=str)
    )
