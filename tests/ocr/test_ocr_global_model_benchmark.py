from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from module.daemon.ocr_benchmark import OcrBenchmark


REMOVED_NON_ENGLISH_ASSETS = (
    "bin/ocr_models/azur_lane_jp/ap_azurlane_jp-v6_small_rec_nvidia.onnx",
    "bin/ocr_models/azur_lane_jp/ppocrv6_azurlane_jp_dict.txt",
    "bin/ocr_models/zh-CN/ap_zh-cn-v6.1_small_rec_dcu.onnx",
    "bin/ocr_models/zh-CN/ap_zh-cn-v6_small_rec_dcu.onnx",
    "bin/ocr_models/zh-CN/alocr-zh-cn-v3.dtk.onnx",
    "bin/ocr_models/zh-CN/alocr-zh-cn-v2.5.dtk.onnx",
    "bin/ocr_models/zh-CN/ppocrv6_cn_dict.txt",
    "bin/ocr_models/zh-CN/cn.txt",
    "bin/ocr_models/ncnn/azur_lane_jp.param",
    "bin/ocr_models/ncnn/azur_lane_jp.bin",
    "bin/ocr_models/ncnn/cn.param",
    "bin/ocr_models/ncnn/cn.bin",
    "bin/ocr_models/ncnn/jp.param",
    "bin/ocr_models/ncnn/jp.bin",
    "bin/ocr_models/ncnn/tw.param",
    "bin/ocr_models/ncnn/tw.bin",
    "module/daemon/sets_azur_lane_jp.tar",
    "module/daemon/sets_zhcn.tar",
    "test/ncnn_ocr_benchmark.py",
    "test/ncnn_ocr_benchmark.zh-CN.md",
)

RETIRED_CONFIG_KEYS = (
    "OcrModelVersionChinese",
    "OcrModelVersionJapanese",
    "OcrModelVersionTraditionalChinese",
)


def _result(
    version: str,
    *,
    accuracy: float,
    errors: int,
    avg_ms: float,
    p95_ms: float,
) -> dict:
    return {
        "model_version": version,
        "status": "ИДЕАЛЬНО" if accuracy == 100.0 else "ХОРОШО",
        "accuracy": accuracy,
        "errors": errors,
        "avg_ms": avg_ms,
        "p95_ms": p95_ms,
        "load_ms": 1.0,
        "recommended": False,
    }


class OcrGlobalModelBenchmarkTests(unittest.TestCase):
    def test_benchmark_covers_every_registered_english_version_once(self):
        from module.ocr.al_ocr import CUSTOM_CTC_MODEL_PARAMS, ONNX_MODEL_PARAMS

        expected = (
            *CUSTOM_CTC_MODEL_PARAMS["azur_lane"].keys(),
            *ONNX_MODEL_PARAMS["azur_lane"].keys(),
        )
        actual = tuple(row[1] for row in OcrBenchmark.BENCHMARKS)

        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), len(set(actual)))
        self.assertTrue(
            all(
                row[0] == "azur_lane"
                and row[2:] == ("sets_num", "sets_num")
                for row in OcrBenchmark.BENCHMARKS
            )
        )

    def test_runtime_registries_contain_only_global_model(self):
        from module.ocr.models import OcrModel
        from module.ocr.ncnn_ocr import MODEL_ALIASES, MODEL_SPECS

        self.assertIn("azur_lane", OcrModel.__dict__)
        for retired in ("azur_lane_jp", "cn", "cnocr", "jp", "tw", "ppocr_v6"):
            self.assertNotIn(retired, OcrModel.__dict__)
        self.assertEqual(set(MODEL_SPECS), {"azur_lane"})
        self.assertEqual(MODEL_ALIASES, {"en": "azur_lane"})

    def test_non_english_assets_are_removed(self):
        remaining = [path for path in REMOVED_NON_ENGLISH_ASSETS if Path(path).exists()]
        self.assertEqual(remaining, [])

    def test_retired_language_model_settings_stay_absent(self):
        sources = (
            Path("module/config/argument/argument.yaml").read_text(encoding="utf-8"),
            Path("module/config/argument/args.json").read_text(encoding="utf-8"),
            Path("config/template.json").read_text(encoding="utf-8"),
        )
        for key in RETIRED_CONFIG_KEYS:
            self.assertTrue(all(key not in source for source in sources), key)

    def test_ranking_prefers_accuracy_before_speed(self):
        fast_but_wrong = _result(
            "fast",
            accuracy=99.9,
            errors=1,
            avg_ms=1.0,
            p95_ms=1.2,
        )
        slower_but_exact = _result(
            "exact",
            accuracy=100.0,
            errors=0,
            avg_ms=8.0,
            p95_ms=9.0,
        )

        ranked = OcrBenchmark._rank_results([fast_but_wrong, slower_but_exact])

        self.assertEqual([item["model_version"] for item in ranked], ["exact", "fast"])
        self.assertTrue(slower_but_exact["recommended"])
        self.assertFalse(fast_but_wrong["recommended"])

    def test_json_report_describes_recognition_only_scope(self):
        result = {
            **_result(
                "alocr_en_v2_6",
                accuracy=100.0,
                errors=0,
                avg_ms=5.0,
                p95_ms=6.0,
            ),
            "model": "azur_lane",
            "backend": "onnxruntime",
            "runtime": "DmlExecutionProvider",
        }
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.json"
            with mock.patch(
                "module.daemon.ocr_benchmark.REPORT_PATH",
                report_path,
            ):
                OcrBenchmark._write_report([result])
            payload = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(payload["scope"], "EN/Global only")
        self.assertEqual(payload["dataset"], "sets_num")
        self.assertTrue(payload["recognition_only"])
        self.assertFalse(payload["detector_tested"])
        self.assertEqual(payload["results"][0]["model_version"], "alocr_en_v2_6")

    def test_accuracy_path_uses_production_normalizer(self):
        source = Path("module/daemon/ocr_benchmark.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "normalize_ocr_text"
        ]
        self.assertTrue(calls)
        self.assertTrue(
            any(
                len(call.args) == 2
                and isinstance(call.args[0], ast.Name)
                and call.args[0].id == "model_name"
                for call in calls
            )
        )


if __name__ == "__main__":
    unittest.main()
