from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from dev_tools.stage8b_semantic_policy import ENGLISH_ONLY_MODEL_NAMES, ROOT

REMOVED_ASSET_PATHS = (
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

REMOVED_CONFIG_KEYS = (
    "OcrModelVersionChinese",
    "OcrModelVersionJapanese",
    "OcrModelVersionTraditionalChinese",
)


class ModelScopeError(RuntimeError):
    pass


def _assignment_value(path: Path, name: str) -> ast.AST:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            if node.value is None:
                break
            return node.value
    raise ModelScopeError(f"Assignment {name} not found in {path}")


def _constant_string(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    raise ModelScopeError(f"Expected a constant string, got {ast.dump(node)}")


def _dict_keys(path: Path, name: str) -> set[str]:
    value = _assignment_value(path, name)
    if not isinstance(value, ast.Dict):
        raise ModelScopeError(f"{name} must be a dict literal in {path}")
    return {_constant_string(key) for key in value.keys if key is not None}


def _string_collection(path: Path, name: str) -> set[str]:
    value = _assignment_value(path, name)
    if isinstance(value, (ast.Set, ast.List, ast.Tuple)):
        return {_constant_string(item) for item in value.elts}
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in {"set", "frozenset", "tuple", "list"}
        and len(value.args) == 1
        and isinstance(value.args[0], (ast.Set, ast.List, ast.Tuple))
    ):
        return {_constant_string(item) for item in value.args[0].elts}
    raise ModelScopeError(f"{name} must be a literal string collection in {path}")


def _class_properties(path: Path, class_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name != "__init__"
            }
    raise ModelScopeError(f"Class {class_name} not found in {path}")


def _benchmark_rows(path: Path) -> list[tuple[Any, ...]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "OcrBenchmark":
            for child in node.body:
                if not isinstance(child, ast.Assign):
                    continue
                if any(
                    isinstance(target, ast.Name) and target.id == "BENCHMARKS"
                    for target in child.targets
                ):
                    value = ast.literal_eval(child.value)
                    return [tuple(row) for row in value]
    raise ModelScopeError("OcrBenchmark.BENCHMARKS not found")


def build_model_scope(output_dir: Path) -> tuple[dict[str, Any], dict[str, int]]:
    findings: list[dict[str, Any]] = []
    al_ocr_path = ROOT / "module/ocr/al_ocr.py"
    ncnn_path = ROOT / "module/ocr/ncnn_ocr.py"
    models_path = ROOT / "module/ocr/models.py"
    rpc_path = ROOT / "module/ocr/rpc.py"
    argument_path = ROOT / "module/config/argument/argument.yaml"
    benchmark_path = ROOT / "module/daemon/ocr_benchmark.py"

    onnx_models = _dict_keys(al_ocr_path, "ONNX_MODEL_PARAMS")
    defaults = _dict_keys(al_ocr_path, "DEFAULT_ONNX_MODEL_VERSION")
    ncnn_models = _dict_keys(ncnn_path, "MODEL_SPECS")
    rpc_models = _string_collection(rpc_path, "SUPPORTED_OCR_MODELS")
    model_properties = _class_properties(models_path, "OcrModel")
    benchmark_rows = _benchmark_rows(benchmark_path)

    expected = set(ENGLISH_ONLY_MODEL_NAMES)
    registries = {
        "ONNX_MODEL_PARAMS": onnx_models,
        "DEFAULT_ONNX_MODEL_VERSION": defaults,
        "MODEL_SPECS": ncnn_models,
        "SUPPORTED_OCR_MODELS": rpc_models,
        "OcrModel properties": model_properties,
    }
    for registry, actual in registries.items():
        if actual != expected:
            findings.append(
                {
                    "kind": "model_registry_mismatch",
                    "registry": registry,
                    "expected": sorted(expected),
                    "actual": sorted(actual),
                }
            )

    argument_source = argument_path.read_text(encoding="utf-8")
    for key in REMOVED_CONFIG_KEYS:
        if key in argument_source:
            findings.append({"kind": "removed_config_key_present", "key": key})

    for relative in REMOVED_ASSET_PATHS:
        if (ROOT / relative).exists():
            findings.append({"kind": "removed_asset_present", "path": relative})

    for row in benchmark_rows:
        if len(row) < 4:
            findings.append({"kind": "benchmark_row_missing_metadata", "row": row})
            continue
        model_name, model_version, dataset_prefix, subfolder = row[:4]
        if model_name != "azur_lane":
            findings.append({"kind": "non_english_benchmark_model", "row": row})
        if not isinstance(model_version, str) or not model_version:
            findings.append({"kind": "benchmark_version_missing", "row": row})
        if not dataset_prefix or not subfolder:
            findings.append({"kind": "benchmark_dataset_missing", "row": row})

    payload = {
        "status": "PASS" if not findings else "FAIL",
        "expected_models": sorted(expected),
        "registries": {key: sorted(value) for key, value in registries.items()},
        "benchmark_rows": benchmark_rows,
        "removed_asset_paths": list(REMOVED_ASSET_PATHS),
        "removed_config_keys": list(REMOVED_CONFIG_KEYS),
        "findings": findings,
    }
    metrics = {"stage8b_model_scope_findings": len(findings)}
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "model-scope.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload, metrics
