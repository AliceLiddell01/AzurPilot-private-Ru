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


def _config_server_values(decorators: list[ast.expr]) -> set[Any] | None:
    values: set[Any] = set()
    matched = False
    for decorator in decorators:
        if not (
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "Config"
            and decorator.func.attr == "when"
        ):
            continue
        for keyword in decorator.keywords:
            if keyword.arg != "SERVER":
                continue
            matched = True
            try:
                values.add(ast.literal_eval(keyword.value))
            except (TypeError, ValueError):
                return None
    return values if matched else None


def _server_condition_for_en(node: ast.AST) -> bool | None:
    """Evaluate simple ``server.server ==/!= '<name>'`` conditions for EN."""
    if not (isinstance(node, ast.Compare) and len(node.ops) == len(node.comparators) == 1):
        return None

    left = node.left
    right = node.comparators[0]
    if (
        isinstance(left, ast.Attribute)
        and isinstance(left.value, ast.Name)
        and left.value.id == "server"
        and left.attr == "server"
        and isinstance(right, ast.Constant)
        and isinstance(right.value, str)
    ):
        expected = right.value
    elif (
        isinstance(right, ast.Attribute)
        and isinstance(right.value, ast.Name)
        and right.value.id == "server"
        and right.attr == "server"
        and isinstance(left, ast.Constant)
        and isinstance(left.value, str)
    ):
        expected = left.value
    else:
        return None

    operator = node.ops[0]
    if isinstance(operator, ast.Eq):
        return "en" == expected
    if isinstance(operator, ast.NotEq):
        return "en" != expected
    return None


class _RemovedRuntimeModelVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str):
        self.relative_path = relative_path
        self.findings: list[dict[str, Any]] = []
        self._active_stack = [True]
        self._owner_stack = ["<module>"]

    @property
    def _active(self) -> bool:
        return self._active_stack[-1]

    @property
    def _owner(self) -> str:
        return ".".join(self._owner_stack[1:]) or "<module>"

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        server_values = _config_server_values(node.decorator_list)
        active = self._active and (server_values is None or "en" in server_values)
        self._active_stack.append(active)
        self._owner_stack.append(node.name)
        self.generic_visit(node)
        self._owner_stack.pop()
        self._active_stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._owner_stack.append(node.name)
        self.generic_visit(node)
        self._owner_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_branch(self, nodes: list[ast.stmt], active: bool) -> None:
        self._active_stack.append(self._active and active)
        for child in nodes:
            self.visit(child)
        self._active_stack.pop()

    def visit_If(self, node: ast.If) -> None:
        condition = _server_condition_for_en(node.test)
        if condition is None:
            self.generic_visit(node)
            return

        self.visit(node.test)
        self._visit_branch(node.body, condition)
        self._visit_branch(node.orelse, not condition)

    def _add(self, node: ast.AST, model: str, reference: str) -> None:
        self.findings.append(
            {
                "kind": "removed_runtime_model_reference",
                "path": self.relative_path,
                "line": getattr(node, "lineno", None),
                "owner": self._owner,
                "model": model,
                "reference": reference,
            }
        )

    def visit_Call(self, node: ast.Call) -> None:
        if self._active:
            for keyword in node.keywords:
                if keyword.arg != "lang":
                    continue
                if (
                    isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                    and keyword.value.value in REMOVED_MODEL_NAMES
                ):
                    self._add(node, keyword.value.value, "lang_keyword")

            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "OCR_MODEL"
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
                and node.args[1].value in REMOVED_MODEL_NAMES
            ):
                self._add(node, node.args[1].value, "getattr")

            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "OCR_MODEL"
                and node.func.attr == "__getattribute__"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and node.args[0].value in REMOVED_MODEL_NAMES
            ):
                self._add(node, node.args[0].value, "__getattribute__")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (
            self._active
            and isinstance(node.value, ast.Name)
            and node.value.id == "OCR_MODEL"
            and node.attr in REMOVED_MODEL_NAMES
        ):
            self._add(node, node.attr, "attribute")
        self.generic_visit(node)


def find_removed_runtime_model_references(root: Path = ROOT) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    module_root = root / "module"
    for path in sorted(module_root.rglob("*.py")):
        relative_path = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError) as exc:
            findings.append(
                {
                    "kind": "runtime_source_parse_error",
                    "path": relative_path,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        visitor = _RemovedRuntimeModelVisitor(relative_path)
        visitor.visit(tree)
        findings.extend(visitor.findings)
    return sorted(
        findings,
        key=lambda item: (
            item["path"],
            item.get("line") or 0,
            item.get("model", ""),
            item["kind"],
        ),
    )


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

    findings.extend(find_removed_runtime_model_references())

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
        "removed_runtime_model_names": sorted(REMOVED_MODEL_NAMES),
        "runtime_reference_scan_root": "module/**/*.py",
        "findings": findings,
    }
    metrics = {"stage8b_model_scope_findings": len(findings)}
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "model-scope.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload, metrics
