"""Temporary Stage 4 CI diagnostic.

This intentionally fails so GitHub Actions preserves the compact benchmark
payload in pytest output. It is removed/replaced before the Stage 4 PR can be
considered mergeable.
"""

import json

import pytest

from tools.benchmark_resizer_stage4 import run_benchmark


def test_stage4_emit_resizer_benchmark():
    payload = run_benchmark(timing_iterations=8)
    compact = {
        "metadata": payload["metadata"],
        "native_detector_reference": payload["native_detector_reference"],
        "ocr_native_reference": payload["ocr_native_reference"],
        "summary": payload["summary"],
        "four_k_detector_rows": payload["four_k_detector_rows"],
        "four_k_ocr_rows": payload["four_k_ocr_rows"],
    }
    pytest.fail(
        "STAGE4_BENCHMARK_JSON="
        + json.dumps(compact, ensure_ascii=False, separators=(",", ":"), default=str)
    )
