from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class GlobalEnOcrTests(unittest.TestCase):
    def test_all_inventory_proved_ocr_files_are_kept(self) -> None:
        files = sorted(
            path for path in (ROOT / "bin/ocr_models").rglob("*")
            if path.is_file()
        )
        self.assertEqual(len(files), 18)

    def test_registry_is_global_only_and_paths_exist(self) -> None:
        source_path = ROOT / "module/ocr/al_ocr.py"
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, str(source_path))
        assignments = {
            target.id: node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for name in (
            "ONNX_MODEL_PARAMS",
            "CUSTOM_CTC_MODEL_PARAMS",
            "DEFAULT_ONNX_MODEL_VERSION",
        ):
            mapping = assignments[name]
            self.assertIsInstance(mapping, ast.Dict)
            keys = {ast.literal_eval(key) for key in mapping.keys}
            self.assertEqual(keys, {"azur_lane"})
        for relative in (
            "bin/ocr_models/azur_lane/ap_azurlane-v6.6_small_rec_dcu.onnx",
            "bin/ocr_models/azur_lane/ap_azurlane-v6.5_small_rec_nvidia.onnx",
            "bin/ocr_models/azur_lane/ppocrv6_azurlane_dict.txt",
            "bin/ocr_models/azur_lane/alocr-en-us-v2.6.nvc.onnx",
            "bin/ocr_models/azur_lane/alocr-en-us-v2.0.nvc.onnx",
            "bin/ocr_models/azur_lane/alocr-en-v1.0.onnx",
            "bin/ocr_models/azur_lane/en_dict.txt",
            "bin/ocr_models/azur_lane/alocr-en-us-900k-w768.dml.onnx",
            "bin/ocr_models/det/PP-OCRv6_tiny_det.onnx",
            "bin/ocr_models/ncnn/azur_lane.param",
            "bin/ocr_models/ncnn/azur_lane.bin",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)

    def test_stale_foreign_ocr_cache_names_are_absent(self) -> None:
        path = ROOT / "module/base/resource.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        release = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "release_resources"
        )
        runtime_strings = {
            node.value for node in ast.walk(release)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        for stale in ("cnocr", "jp", "tw"):
            self.assertNotIn(stale, runtime_strings)
        self.assertIn("azur_lane", runtime_strings)
        self.assertIn("det", runtime_strings)


if __name__ == "__main__":
    unittest.main()
