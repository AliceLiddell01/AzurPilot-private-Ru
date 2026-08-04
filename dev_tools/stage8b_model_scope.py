from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

from dev_tools.stage8b_semantic_policy import (
    ENGLISH_ONLY_MODEL_NAMES,
    REMOVED_MODEL_NAMES,
    ROOT,
)

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


def _load_assignment(path: Path, name: str) -> Any:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise ModelScopeError(f"Assignment {name} not found in {path}")


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
                if isinstance(child, ast.Assign):
                    for target in child.targets:
                        if isinstance(target, ast.Name) and target.id == "BENCHMARKS":
                            return [tuple(row) for row in ast.literal_eval(child.value)]
    raise ModelScopeError("OcrBenchmark.BENCHMARKS not found")


def build_model_scope(output_dir: Path) -> tuple[dict[str, Any], dict[str, int]]:
    findings: list[dict[str, Any]] = []
    al_ocr_path = ROOT / "module/ocr/al_ocr.py"
    ncnn_path = ROOT / "module/ocr/ncnn_ocr.py"
    models_path = ROOT / "module/ocr/models.py"
    rpc_path = ROOT / "module/ocr/rpc.py"
    argument_path = ROOT / "module/config/argument/argument.yaml"
    benchmark_path = ROOT / "module/daemon/ocr_benchmark.py"

    onnx_models = _load_assignment(al_ocr_path, "ONNX_MODEL_PARAMS")
    defaults = _load_assignment(al_ocr_path, "DEFAULT_ONNX_MODEL_VERSION")
    ncnn_models = _load_assignment(ncnn_path, "MODEL_SPECS")
    rpc_models = set(_load_assignment(rpc_path, "SUPPORTED_OCR_MODELS"))
    model_properties = _class_properties(models_path, "OcrModel")
    benchmark_rows = _benchmark_rows(benchmark_path)

    expected = set(ENGLISH_ONLY_MODEL_NAMES)
    registries = {
        "ONNX_MODEL_PARAMS": set(onnx_models),
        "DEFAULT_ONNX_MODEL_VERSION": set(defaults),
        "MODEL_SPECS": set(ncnn_models),
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
        if model_version not in onnx_models["azur_lane"] and model_version != "alocr_en_900k":
            findings.append({"kind": "unknown_english_benchmark_version", "row": row})
        if not dataset_prefix or not subfolder:
            findings.append({"kind": "benchmark_dataset_missing", "row": row})

    all_runtime_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (al_ocr_path, ncnn_path, models_path, rpc_path, benchmark_path)
    )
    for name in sorted(REMOVED_MODEL_NAMES):
        if name in {"ppocr_v6"}:
            # ppocr_v6 remains an English recognition version under azur_lane,
            # but must not remain a top-level model registry.
            continue
        if f'"{name}"' in all_runtime_sources or f"'{name}'" in all_runtime_sources:
            findings.append({"kind": "removed_model_literal_present", "model": name})

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
