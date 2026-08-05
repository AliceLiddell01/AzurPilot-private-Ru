#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
GLOBAL_PACKAGE = "com.YoStarEN.AzurLane"


def run(*args: str) -> None:
    process = subprocess.run(
        args,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    print("$ " + " ".join(args))
    print(process.stdout)
    if process.returncode:
        raise RuntimeError(f"command failed {process.returncode}: {args}")


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise RuntimeError(f"finalization target drifted: {path}")
    target.write_text(text.replace(old, new), encoding="utf-8")


def flatten_strings(node, prefix=()):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from flatten_strings(value, prefix + (str(key),))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from flatten_strings(value, prefix + (str(index),))
    elif isinstance(node, str):
        yield ".".join(prefix), node


def prune_build_time_catalog() -> None:
    en_path = ROOT / "module/config/i18n/en-US.json"
    ru_path = ROOT / "module/config/i18n/ru-RU.json"
    en = json.loads(en_path.read_text(encoding="utf-8"))
    ru = json.loads(ru_path.read_text(encoding="utf-8"))

    emulator = en.get("Emulator")
    if not isinstance(emulator, dict):
        raise RuntimeError("en-US Emulator catalog missing")

    packages = emulator.get("PackageName")
    servers = emulator.get("ServerName")
    if not isinstance(packages, dict) or not isinstance(servers, dict):
        raise RuntimeError("en-US package/server catalogs missing")

    for key in list(packages):
        if key != GLOBAL_PACKAGE:
            del packages[key]
    for key in list(servers):
        if not key.startswith("en-"):
            del servers[key]

    en_keys = {key for key, _ in flatten_strings(en)}
    ru_keys = {key for key, _ in flatten_strings(ru)}
    if en_keys != ru_keys:
        extra = sorted(en_keys - ru_keys)
        missing = sorted(ru_keys - en_keys)
        raise RuntimeError(
            f"locale parity remains unresolved: extra={extra[:20]} "
            f"missing={missing[:20]}"
        )

    en_path.write_text(
        json.dumps(en, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_structured_files() -> None:
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix == ".json":
            json.loads(path.read_text(encoding="utf-8"))
        elif path.suffix in (".yaml", ".yml"):
            list(yaml.safe_load_all(path.read_text(encoding="utf-8")))


def hashes(paths):
    import hashlib

    result = {}
    for relative in paths:
        path = ROOT / relative
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def main() -> None:
    (ROOT / "tests/test_global_en_ocr.py").write_text(
        'from __future__ import annotations\n\nimport ast\nimport unittest\nfrom pathlib import Path\n\nROOT = Path(__file__).resolve().parents[1]\n\n\nclass GlobalEnOcrTests(unittest.TestCase):\n    def test_all_inventory_proved_ocr_files_are_kept(self) -> None:\n        files = sorted(\n            path for path in (ROOT / "bin/ocr_models").rglob("*")\n            if path.is_file()\n        )\n        self.assertEqual(len(files), 18)\n\n    def test_registry_is_global_only_and_paths_exist(self) -> None:\n        source_path = ROOT / "module/ocr/al_ocr.py"\n        source = source_path.read_text(encoding="utf-8")\n        tree = ast.parse(source, str(source_path))\n        assignments = {\n            target.id: node.value\n            for node in tree.body\n            if isinstance(node, ast.Assign)\n            for target in node.targets\n            if isinstance(target, ast.Name)\n        }\n        for name in (\n            "ONNX_MODEL_PARAMS",\n            "CUSTOM_CTC_MODEL_PARAMS",\n            "DEFAULT_ONNX_MODEL_VERSION",\n        ):\n            mapping = assignments[name]\n            self.assertIsInstance(mapping, ast.Dict)\n            keys = {ast.literal_eval(key) for key in mapping.keys}\n            self.assertEqual(keys, {"azur_lane"})\n        for relative in (\n            "bin/ocr_models/azur_lane/ap_azurlane-v6.6_small_rec_dcu.onnx",\n            "bin/ocr_models/azur_lane/ap_azurlane-v6.5_small_rec_nvidia.onnx",\n            "bin/ocr_models/azur_lane/ppocrv6_azurlane_dict.txt",\n            "bin/ocr_models/azur_lane/alocr-en-us-v2.6.nvc.onnx",\n            "bin/ocr_models/azur_lane/alocr-en-us-v2.0.nvc.onnx",\n            "bin/ocr_models/azur_lane/alocr-en-v1.0.onnx",\n            "bin/ocr_models/azur_lane/en_dict.txt",\n            "bin/ocr_models/azur_lane/alocr-en-us-900k-w768.dml.onnx",\n            "bin/ocr_models/det/PP-OCRv6_tiny_det.onnx",\n            "bin/ocr_models/ncnn/azur_lane.param",\n            "bin/ocr_models/ncnn/azur_lane.bin",\n        ):\n            self.assertTrue((ROOT / relative).is_file(), relative)\n\n    def test_stale_foreign_ocr_cache_names_are_absent(self) -> None:\n        path = ROOT / "module/base/resource.py"\n        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))\n        release = next(\n            node for node in tree.body\n            if isinstance(node, ast.FunctionDef)\n            and node.name == "release_resources"\n        )\n        runtime_strings = {\n            node.value for node in ast.walk(release)\n            if isinstance(node, ast.Constant) and isinstance(node.value, str)\n        }\n        for stale in ("cnocr", "jp", "tw"):\n            self.assertNotIn(stale, runtime_strings)\n        self.assertIn("azur_lane", runtime_strings)\n        self.assertIn("det", runtime_strings)\n\n\nif __name__ == "__main__":\n    unittest.main()\n',
        encoding="utf-8",
    )
    replace_once(
        "tests/test_server_locale_separation.py",
        '        self.assertEqual(EVENT_NAME_FALLBACK_ORDER, ("en", "cn", "jp", "tw"))\n',
        '        self.assertEqual(EVENT_NAME_FALLBACK_ORDER, ())\n',
    )
    prune_build_time_catalog()

    generated = (
        "module/config/argument/args.json",
        "module/config/argument/menu.json",
        "module/config/config_generated.py",
        "config/template.json",
        "module/config/i18n/ru-RU.json",
        "module/config/i18n/en-US.json",
    )
    run("uv", "run", "--locked", "-m", "module.config.config_updater")
    first = hashes(generated)
    run("uv", "run", "--locked", "-m", "module.config.config_updater")
    if hashes(generated) != first:
        raise RuntimeError("config updater is not idempotent")

    run("uv", "run", "--locked", "-m", "dev_tools.button_extract")
    asset_files = tuple(
        str(path.relative_to(ROOT))
        for path in sorted((ROOT / "module").glob("*/assets.py"))
        if "automatically generated by dev_tools/button_extract.py"
        in path.read_text(encoding="utf-8")
    )
    first_assets = hashes(asset_files)
    run("uv", "run", "--locked", "-m", "dev_tools.button_extract")
    if hashes(asset_files) != first_assets:
        raise RuntimeError("button generator is not idempotent")

    parse_structured_files()

    for relative in (
        ".github/workflows/global-en-finalize.yml",
        "tools/global_en_finalize.py",
    ):
        (ROOT / relative).unlink()

    run("git", "diff", "--check")
    print("Global/EN finalization complete")


if __name__ == "__main__":
    main()
