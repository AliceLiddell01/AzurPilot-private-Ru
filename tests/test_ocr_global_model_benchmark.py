from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from module.daemon.ocr_benchmark import BenchmarkResult, OcrBenchmark
from module.ocr.model_policy import (
    ENGLISH_ONNX_MODEL_VERSIONS,
    HIDDEN_PERSONAL_OCR_ARGUMENTS,
    REMOVED_MODEL_NAMES,
    should_hide_personal_ocr_argument,
)
from module.webui.app_personal_ocr import PersonalOcrSettingsMixin


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
)


class _SetGroupRecorder:
    def set_group(self, group, arg_dict, config, task):
        del group, config, task
        return arg_dict


class _PersonalOcrSubject(PersonalOcrSettingsMixin, _SetGroupRecorder):
    pass


class OcrGlobalModelBenchmarkTests(unittest.TestCase):
    def test_all_english_versions_are_benchmarked_once(self):
        candidates = OcrBenchmark._model_candidates()
        onnx_versions = tuple(
            candidate.version
            for candidate in candidates
            if candidate.backend == "onnxruntime"
        )
        self.assertEqual(onnx_versions, ENGLISH_ONNX_MODEL_VERSIONS)
        self.assertEqual(
            [
                candidate.version
                for candidate in candidates
                if candidate.backend == "ncnn"
            ],
            ["ncnn_azur_lane"],
        )
        self.assertEqual(len({candidate.version for candidate in candidates}), len(candidates))

    def test_non_english_runtime_models_are_not_registered(self):
        from module.ocr.models import OcrModel
        from module.ocr.ncnn_ocr import MODEL_ALIASES, MODEL_SPECS

        self.assertEqual(set(MODEL_SPECS), {"azur_lane"})
        self.assertEqual(MODEL_ALIASES, {"en": "azur_lane"})
        for name in REMOVED_MODEL_NAMES:
            self.assertNotIn(name, OcrModel.__dict__)
        self.assertIn("cnocr", OcrModel.__dict__)

    def test_legacy_cnocr_uses_english_model_without_chinese_weights(self):
        from module.ocr.models import OcrModel

        sentinel = object()
        with mock.patch("module.ocr.models.AlOcr", return_value=sentinel) as factory:
            models = OcrModel()
            self.assertIs(models.cnocr, sentinel)
        factory.assert_called_once_with(name="azur_lane")

    def test_non_english_assets_are_removed(self):
        remaining = [path for path in REMOVED_NON_ENGLISH_ASSETS if Path(path).exists()]
        self.assertEqual(remaining, [])

    def test_only_obsolete_language_settings_are_hidden(self):
        self.assertEqual(
            HIDDEN_PERSONAL_OCR_ARGUMENTS,
            {
                "OcrModelVersionChinese",
                "OcrModelVersionJapanese",
                "OcrModelVersionTraditionalChinese",
            },
        )
        for argument in HIDDEN_PERSONAL_OCR_ARGUMENTS:
            self.assertTrue(
                should_hide_personal_ocr_argument(
                    task="Alas",
                    group="Optimization",
                    argument=argument,
                )
            )
        self.assertFalse(
            should_hide_personal_ocr_argument(
                task="Alas",
                group="Optimization",
                argument="OcrModelVersionEnglish",
            )
        )
        self.assertFalse(
            should_hide_personal_ocr_argument(
                task="Other",
                group="Optimization",
                argument="OcrModelVersionChinese",
            )
        )

    def test_webui_mixin_filters_only_language_model_blocks(self):
        arguments = {
            "OcrDevice": {"value": "auto"},
            "OcrModelVersionEnglish": {"value": "auto"},
            "OcrModelVersionChinese": {"value": "auto"},
            "OcrModelVersionJapanese": {"value": "auto"},
            "OcrModelVersionTraditionalChinese": {"value": "auto"},
        }
        filtered = _PersonalOcrSubject().set_group(
            ("Optimization",),
            arguments,
            {},
            "Alas",
        )
        self.assertEqual(
            set(filtered),
            {"OcrDevice", "OcrModelVersionEnglish"},
        )
        self.assertEqual(set(arguments), {
            "OcrDevice",
            "OcrModelVersionEnglish",
            "OcrModelVersionChinese",
            "OcrModelVersionJapanese",
            "OcrModelVersionTraditionalChinese",
        })

    def test_ranking_prefers_accuracy_before_speed(self):
        fast_but_wrong = BenchmarkResult(
            version="fast",
            backend="onnxruntime",
            family="test",
            device_requested="gpu",
            runtime="test",
            model_files=["fast.onnx"],
            dictionary_file="dict.txt",
            status="ХОРОШО",
            accuracy=99.9,
            correct=999,
            total=1000,
            errors=1,
            avg_ms=1.0,
            p95_ms=1.2,
        )
        slower_but_exact = BenchmarkResult(
            version="exact",
            backend="onnxruntime",
            family="test",
            device_requested="gpu",
            runtime="test",
            model_files=["exact.onnx"],
            dictionary_file="dict.txt",
            status="ИДЕАЛЬНО",
            accuracy=100.0,
            correct=1000,
            total=1000,
            errors=0,
            avg_ms=8.0,
            p95_ms=9.0,
        )
        ranked = OcrBenchmark._rank_results([fast_but_wrong, slower_but_exact])
        self.assertEqual([item.version for item in ranked], ["exact", "fast"])
        self.assertTrue(slower_but_exact.recommended)
        self.assertFalse(fast_but_wrong.recommended)

    def test_json_report_describes_real_benchmark_scope(self):
        result = BenchmarkResult(
            version="alocr_en_v2_6",
            backend="onnxruntime",
            family="PP-OCRv4",
            device_requested="gpu",
            runtime="DmlExecutionProvider",
            model_files=["alocr-en-us-v2.6.nvc.onnx"],
            dictionary_file="en_dict.txt",
            status="ИДЕАЛЬНО",
            accuracy=100.0,
            correct=1000,
            total=1000,
            errors=0,
            avg_ms=5.0,
            p95_ms=6.0,
        )
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
        self.assertEqual(payload["results"][0]["version"], "alocr_en_v2_6")


if __name__ == "__main__":
    unittest.main()
