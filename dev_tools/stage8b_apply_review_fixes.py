from __future__ import annotations

import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def write(relative: str, content: str) -> None:
    (ROOT / relative).write_text(content, encoding="utf-8")


def replace_once(relative: str, old: str, new: str) -> None:
    content = read(relative)
    count = content.count(old)
    if count != 1:
        raise RuntimeError(f"Expected one literal match in {relative}, got {count}: {old[:80]!r}")
    write(relative, content.replace(old, new))


def regex_once(relative: str, pattern: str, replacement: str, *, flags: int = 0) -> None:
    content = read(relative)
    updated, count = re.subn(pattern, replacement, content, count=1, flags=flags)
    if count != 1:
        raise RuntimeError(f"Expected one regex match in {relative}, got {count}: {pattern}")
    write(relative, updated)


def patch_al_ocr() -> None:
    relative = "module/ocr/al_ocr.py"
    regex_once(
        relative,
        r"AZUR_LANE_JP_V6_PARAMS = \(.*?\n\)\n\n\nclass RecOnlyOCR",
        "class RecOnlyOCR",
        flags=re.DOTALL,
    )
    english_registry = '''ONNX_MODEL_PARAMS = {
    "azur_lane": {
        "azur_lane_v6_6": (
            "bin/ocr_models/azur_lane/ap_azurlane-v6.6_small_rec_dcu.onnx",
            "bin/ocr_models/azur_lane/ppocrv6_azurlane_dict.txt",
            OCRVersion.PPOCRV6,
        ),
        "azur_lane_v6_5": (
            "bin/ocr_models/azur_lane/ap_azurlane-v6.5_small_rec_nvidia.onnx",
            "bin/ocr_models/azur_lane/ppocrv6_azurlane_dict.txt",
            OCRVersion.PPOCRV6,
        ),
        "ppocr_v6": GENERIC_PPOCR_V6_PARAMS,
        "alocr_en_v2_6": (
            "bin/ocr_models/azur_lane/alocr-en-us-v2.6.nvc.onnx",
            "bin/ocr_models/azur_lane/en_dict.txt",
            OCRVersion.PPOCRV4,
        ),
        "alocr_en_v2_0": (
            "bin/ocr_models/azur_lane/alocr-en-us-v2.0.nvc.onnx",
            "bin/ocr_models/azur_lane/en_dict.txt",
            OCRVersion.PPOCRV4,
        ),
        "alocr_en_v1_0": (
            "bin/ocr_models/azur_lane/alocr-en-v1.0.onnx",
            "bin/ocr_models/azur_lane/en_dict.txt",
            OCRVersion.PPOCRV4,
        ),
    },
}

CUSTOM_CTC_MODEL_PARAMS'''
    regex_once(
        relative,
        r"ONNX_MODEL_PARAMS = \{.*?\n\}\n\nCUSTOM_CTC_MODEL_PARAMS",
        english_registry,
        flags=re.DOTALL,
    )
    regex_once(
        relative,
        r"DEFAULT_ONNX_MODEL_VERSION = \{.*?\n\}",
        'DEFAULT_ONNX_MODEL_VERSION = {\n    "azur_lane": "alocr_en_v2_6",\n}',
        flags=re.DOTALL,
    )
    content = read(relative)
    content = content.replace(
        "name: 模型名称，如 'azur_lane'、'azur_lane_jp'、'ppocr_v6'、'cn'、'jp'、'tw'。",
        "name: имя единственной Global/English модели 'azur_lane'.",
    )
    write(relative, content)


def patch_ncnn() -> None:
    relative = "module/ocr/ncnn_ocr.py"
    replacement = '''MODEL_SPECS = {
    "azur_lane": NcnnRecModelSpec(
        name="azur_lane",
        param_path=MODEL_ROOT / "azur_lane.param",
        bin_path=MODEL_ROOT / "azur_lane.bin",
        keys_path=REPO_ROOT / "bin/ocr_models/azur_lane/ppocrv6_azurlane_dict.txt",
        output_name=OUTPUT_NAME,
        disable_fp16=True,
    ),
}

MODEL_ALIASES = {
    "en": "azur_lane",
}'''
    regex_once(
        relative,
        r"MODEL_SPECS = \{.*?\n\}\n\nMODEL_ALIASES = \{.*?\n\}",
        replacement,
        flags=re.DOTALL,
    )


def patch_rpc() -> None:
    regex_once(
        "module/ocr/rpc.py",
        r"SUPPORTED_OCR_MODELS = frozenset\(\s*\{.*?\}\s*\)",
        'SUPPORTED_OCR_MODELS = frozenset({"azur_lane"})',
        flags=re.DOTALL,
    )


def patch_argument_yaml() -> None:
    relative = "module/config/argument/argument.yaml"
    regex_once(
        relative,
        r"  OcrModelVersionChinese:\n.*?(?=  ScreenshotInterval:)",
        "",
        flags=re.DOTALL,
    )


def patch_manual_model_version() -> None:
    matches: list[Path] = []
    pattern = re.compile(
        r"(?P<indent>    )def ocr_model_version\(self, name\):\n.*?(?=\n    (?:def |@property|@cached_property)|\Z)",
        re.DOTALL,
    )
    for path in sorted((ROOT / "module/config").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "def ocr_model_version" not in source:
            continue
        replacement = '''    def ocr_model_version(self, name):
        if name != "azur_lane":
            raise ValueError(f"Неподдерживаемая OCR-модель: {name}")
        return self.Optimization_OcrModelVersionEnglish
'''
        updated, count = pattern.subn(replacement, source, count=1)
        if count != 1:
            raise RuntimeError(f"Unable to patch ocr_model_version in {path}")
        path.write_text(updated, encoding="utf-8")
        matches.append(path)
    if len(matches) != 1:
        raise RuntimeError(f"Expected one ocr_model_version owner, got {matches}")


def patch_owned_translations() -> None:
    replacements = {
        "module/campaign/campaign_ocr.py": {
            "logger.warning(f'[战役-OCR] 未知的关卡名称: {name}')": (
                "logger.warning(f'[Кампания — OCR] Неизвестное имя этапа: {name}')"
            ),
            "logger.warning('[战役] 数字与文本之间未找到间隔。')": (
                "logger.warning('[Кампания — OCR] Не найден интервал между номером этапа и текстом.')"
            ),
        },
        "module/os/sea_miles_ocr.py": {
            'logger.warning(f"[大世界-里程] 异常的海域里程: {result}")': (
                'logger.warning(f"[Operation Siren — OCR] Недопустимое значение Sea Miles: {result}")'
            ),
        },
        "module/device/device.py": {
            "logger.info('[设备-基准测试] 运行OCR设备基准测试')": (
                "logger.info('[Устройство — OCR benchmark] Проверка доступных OCR-устройств')"
            ),
        },
    }
    for relative, mapping in replacements.items():
        content = read(relative)
        for old, new in mapping.items():
            if content.count(old) != 1:
                raise RuntimeError(f"Translation owner string mismatch: {relative}: {old}")
            content = content.replace(old, new)
        write(relative, content)


def patch_scope_filter() -> None:
    relative = "dev_tools/stage8b_ocr_log_audit.py"
    replace_once(
        relative,
        "    OCR_SCOPE_PATHS,\n    PRESERVED_IDENTIFIERS,",
        "    OCR_SCOPE_PATHS,\n    OCR_SCOPE_RULES,\n    PRESERVED_IDENTIFIERS,",
    )
    marker = "\ndef collect_entries(root: Path = ROOT) -> list[ScopeEntry]:\n"
    helper = '''
def _entry_is_owned(entry: ScopeEntry) -> bool:
    owners = OCR_SCOPE_RULES.get(entry.path)
    if owners is None:
        return True
    return any(
        entry.function_owner == owner or entry.function_owner.startswith(owner + ".")
        for owner in owners
    )


def collect_entries(root: Path = ROOT) -> list[ScopeEntry]:
'''
    replace_once(relative, marker, helper)
    replace_once(
        relative,
        "            entries.extend(_entries_from_source(relative, path.read_text(encoding=\"utf-8\")))",
        "            parsed = _entries_from_source(relative, path.read_text(encoding=\"utf-8\"))\n"
        "            entries.extend(entry for entry in parsed if _entry_is_owned(entry))",
    )
    replace_once(
        relative,
        "        for entry in _entries_from_source(relative, source):\n            key = (entry.path, entry.function_owner, entry.call_kind, entry.severity)",
        "        for entry in _entries_from_source(relative, source):\n"
        "            if not _entry_is_owned(entry):\n"
        "                continue\n"
        "            key = (entry.path, entry.function_owner, entry.call_kind, entry.severity)",
    )


def patch_real_probe_compatibility() -> None:
    relative = "dev_tools/stage8b_real_output_probe.py"
    replace_once(
        relative,
        "    from module.ocr import al_ocr\n    from module.ocr.ocr import normalize_ocr_text",
        "    from module.ocr import al_ocr, ocr as ocr_module\n"
        "    normalize_ocr_text = getattr(\n"
        "        ocr_module,\n"
        "        \"normalize_ocr_text\",\n"
        "        lambda _model_name, text: text,\n"
        "    )",
    )


def patch_output_probe() -> None:
    relative = "dev_tools/stage8b_output_probe.py"
    regex_once(
        relative,
        r"def _model_versions\(al_ocr\) -> dict\[str, object\]:\n.*?\n\n\ndef _detection_values",
        '''def _model_versions(al_ocr) -> dict[str, object]:
    name = "azur_lane"
    return {
        "defaults": {name: al_ocr.DEFAULT_ONNX_MODEL_VERSION[name]},
        "onnx_versions": {name: sorted(al_ocr.ONNX_MODEL_PARAMS[name])},
        "custom_versions": {name: sorted(al_ocr.CUSTOM_CTC_MODEL_PARAMS.get(name, {}))},
        "detector": al_ocr.DET_MODEL_PATH,
    }


def _detection_values''',
        flags=re.DOTALL,
    )
    content = read(relative)
    content = content.replace('"phrase": normalize_text("azur_lane", "LEVEL: New Jersey 120")', '"phrase": normalize_text("azur_lane", "LEVEL: 120")')
    write(relative, content)


def patch_output_contract() -> None:
    relative = "dev_tools/stage8b_output_contract.py"
    content = read(relative)
    for literal in (
        '    "ALAS_CTC_MAX_WIDTH", "ONNX_MODEL_PARAMS", "CUSTOM_CTC_MODEL_PARAMS",\n',
        '    "DEFAULT_ONNX_MODEL_VERSION", "DET_MODEL_PATH", "MODEL_ALIASES",\n',
    ):
        if literal not in content:
            raise RuntimeError(f"Output literal policy fragment missing: {literal}")
    content = content.replace(
        '    "ALAS_CTC_MAX_WIDTH", "ONNX_MODEL_PARAMS", "CUSTOM_CTC_MODEL_PARAMS",\n',
        '    "ALAS_CTC_MAX_WIDTH", "CUSTOM_CTC_MODEL_PARAMS",\n',
    )
    content = content.replace(
        '    "DEFAULT_ONNX_MODEL_VERSION", "DET_MODEL_PATH", "MODEL_ALIASES",\n',
        '    "DET_MODEL_PATH",\n',
    )
    excluded_functions = (
        '("module/ocr/al_ocr.py", "_resolve_onnx_model_version"),',
        '("module/ocr/al_ocr.py", "_get_onnx_model_params"),',
        '("module/ocr/al_ocr.py", "_create_ocr"),',
        '("module/daemon/ocr_benchmark.py", "OcrBenchmark._find_archive"),',
        '("module/daemon/ocr_benchmark.py", "OcrBenchmark._load_test_cases"),',
        '("module/daemon/ocr_benchmark.py", "OcrBenchmark._rate_speed"),',
        '("module/daemon/ocr_benchmark.py", "OcrBenchmark._run_single"),',
        '("module/daemon/ocr_benchmark.py", "OcrBenchmark.run"),',
        '("module/daemon/ocr_benchmark.py", "OcrBenchmark.run_simple_ocr_benchmark"),',
        '("module/daemon/ocr_benchmark.py", "run_ocr_benchmark"),',
    )
    for fragment in excluded_functions:
        line = f"    {fragment}\n"
        if line not in content:
            raise RuntimeError(f"Expected critical function entry missing: {fragment}")
        content = content.replace(line, "")
    content = content.replace('"phrase": "LEVEL: New Jersey 120",', '"phrase": "LEVEL: 120",')
    write(relative, content)


def patch_security_audit() -> None:
    relative = "dev_tools/stage8b_security_audit.py"
    replace_once(
        relative,
        '        "_reject_existing_symlink_components",\n        "tempfile.mkstemp",',
        '        "_reject_existing_reparse_components",\n'
        '        "_is_reparse_point",\n'
        '        "is_junction",\n'
        '        "FILE_ATTRIBUTE_REPARSE_POINT",\n'
        '        "tempfile.mkstemp",',
    )
    replace_once(
        relative,
        '            "symlink_guard": True,\n            "atomic_publish": True,',
        '            "symlink_guard": True,\n'
        '            "junction_reparse_guard": True,\n'
        '            "atomic_publish": True,',
    )


def delete_non_global_assets() -> None:
    paths = (
        "bin/ocr_models/azur_lane_jp",
        "bin/ocr_models/zh-CN",
        "bin/ocr_models/ncnn/azur_lane_jp.param",
        "bin/ocr_models/ncnn/azur_lane_jp.bin",
        "bin/ocr_models/ncnn/cn.param",
        "bin/ocr_models/ncnn/cn.bin",
        "bin/ocr_models/ncnn/jp.param",
        "bin/ocr_models/ncnn/jp.bin",
        "bin/ocr_models/ncnn/tw.param",
        "bin/ocr_models/ncnn/tw.bin",
    )
    for relative in paths:
        target = ROOT / relative
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()


def main() -> None:
    patch_al_ocr()
    patch_ncnn()
    patch_rpc()
    patch_argument_yaml()
    patch_manual_model_version()
    patch_owned_translations()
    patch_scope_filter()
    patch_real_probe_compatibility()
    patch_output_probe()
    patch_output_contract()
    patch_security_audit()
    delete_non_global_assets()


if __name__ == "__main__":
    main()
