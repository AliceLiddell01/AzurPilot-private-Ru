from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from dev_tools.stage8b_semantic_policy import IMMUTABLE_STAGE8B_BASE_SHA, ROOT

CRITICAL_LITERAL_NAMES = (
    "ALAS_CTC_CHARSET", "ALAS_CTC_BLANK_ID", "ALAS_CTC_IMAGE_HEIGHT",
    "ALAS_CTC_MAX_WIDTH", "ONNX_MODEL_PARAMS", "CUSTOM_CTC_MODEL_PARAMS",
    "DEFAULT_ONNX_MODEL_VERSION", "DET_MODEL_PATH", "MODEL_ALIASES",
    "REC_IMAGE_SHAPE", "INPUT_NAME", "OUTPUT_NAME",
)
CRITICAL_FUNCTIONS = (
    ("module/ocr/al_ocr.py", "handle_ocr_error"),
    ("module/ocr/al_ocr.py", "AlOcrCtcRecOCR.__init__"),
    ("module/ocr/al_ocr.py", "AlOcrCtcRecOCR.__call__"),
    ("module/ocr/al_ocr.py", "AlOcrCtcRecOCR._preprocess"),
    ("module/ocr/al_ocr.py", "AlOcrCtcRecOCR._to_gray"),
    ("module/ocr/al_ocr.py", "AlOcrCtcRecOCR._decode"),
    ("module/ocr/al_ocr.py", "_ocr_worker_loop"),
    ("module/ocr/al_ocr.py", "_ensure_ocr_worker"),
    ("module/ocr/al_ocr.py", "_run_ocr_queued"),
    ("module/ocr/al_ocr.py", "_resolve_onnx_model_version"),
    ("module/ocr/al_ocr.py", "_get_onnx_model_params"),
    ("module/ocr/al_ocr.py", "_configure_windows_ml_sessions"),
    ("module/ocr/al_ocr.py", "_create_ocr"),
    ("module/ocr/al_ocr.py", "_model_cache_key"),
    ("module/ocr/al_ocr.py", "_create_det_ocr_for_onnx"),
    ("module/ocr/al_ocr.py", "_create_det_ocr_for_ncnn"),
    ("module/ocr/al_ocr.py", "_get_det_model"),
    ("module/ocr/al_ocr.py", "reset_ocr_model"),
    ("module/ocr/al_ocr.py", "AlOcr.__init__"),
    ("module/ocr/al_ocr.py", "AlOcr._ocr_direct"),
    ("module/ocr/al_ocr.py", "AlOcr._det_direct"),
    ("module/ocr/al_ocr.py", "AlOcr._ocr_for_single_lines_direct"),
    ("module/ocr/ocr.py", "Ocr.pre_process"),
    ("module/ocr/ocr.py", "Ocr.ocr"),
    ("module/ocr/ocr.py", "Digit.after_process"),
    ("module/ocr/ocr.py", "DigitCounter.after_process"),
    ("module/ocr/ocr.py", "DigitCounter.ocr"),
    ("module/ocr/ocr.py", "Duration.after_process"),
    ("module/ocr/ocr.py", "Duration.parse_time"),
    ("module/campaign/campaign_ocr.py", "CampaignOcr._campaign_ocr_result_process"),
    ("module/ocr/ncnn_ocr.py", "_resolve_gpu_index"),
    ("module/ocr/ncnn_ocr.py", "RecPreprocessor.resize_norm_img"),
    ("module/ocr/ncnn_ocr.py", "NcnnRecOCR._check_model_files"),
    ("module/ocr/ncnn_ocr.py", "NcnnRecOCR._create_net"),
    ("module/ocr/ncnn_ocr.py", "NcnnRecOCR.__call__"),
    ("module/ocr/ncnn_ocr.py", "NcnnRecOCR._infer"),
    ("module/ocr/ncnn_ocr.py", "NcnnRecOCR._to_ncnn_mat"),
    ("module/ocr/ncnn_ocr.py", "NcnnRecOCR._normalize_output"),
    ("module/ocr/windows_ml.py", "create_onnx_session"),
    ("module/ocr/windows_ml.py", "_prepare_vendor_execution_providers"),
    ("module/ocr/windows_ml.py", "_ensure_and_register_provider"),
    ("module/ocr/windows_ml.py", "_iter_preferred_devices"),
    ("module/ocr/windows_ml.py", "_vendor_execution_provider_names"),
    ("module/ocr/windows_ml.py", "_is_discrete_gpu"),
    ("module/daemon/ocr_benchmark.py", "OcrBenchmark._find_archive"),
    ("module/daemon/ocr_benchmark.py", "OcrBenchmark._load_test_cases"),
    ("module/daemon/ocr_benchmark.py", "OcrBenchmark._rate_speed"),
    ("module/daemon/ocr_benchmark.py", "OcrBenchmark._run_single"),
    ("module/daemon/ocr_benchmark.py", "OcrBenchmark.run"),
    ("module/daemon/ocr_benchmark.py", "OcrBenchmark.run_simple_ocr_benchmark"),
    ("module/daemon/ocr_benchmark.py", "run_ocr_benchmark"),
)

MODEL_SELECTION_SYMBOLS = {
    "_resolve_onnx_model_version", "_get_onnx_model_params", "_create_ocr",
}
BACKEND_FALLBACK_SYMBOLS = {
    "_get_onnx_model_params", "_create_ocr", "create_onnx_session",
    "OcrBenchmark.run_simple_ocr_benchmark",
}
QUEUE_SYMBOLS = {"_ocr_worker_loop", "_ensure_ocr_worker", "_run_ocr_queued"}


class ContractError(RuntimeError):
    pass


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=ROOT, check=False, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if completed.returncode != 0:
        raise ContractError(completed.stderr.strip() or "Git command failed")
    return completed.stdout.strip()


def _source_at(ref: str, path: str) -> str:
    return _git("show", f"{ref}:{path}")


def _qualified_functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    result: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.classes: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> Any:
            self.classes.append(node.name)
            self.generic_visit(node)
            self.classes.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
            result[".".join((*self.classes, node.name))] = node
            self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef

    Visitor().visit(tree)
    return result


def _strip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _normalize_translation_literals(node: ast.AST) -> str:
    cloned = ast.parse(ast.unparse(node))

    class Normalizer(ast.NodeTransformer):
        def visit_FunctionDef(self, function: ast.FunctionDef) -> Any:
            function.body = _strip_docstring(function.body)
            self.generic_visit(function)
            return function

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, call: ast.Call) -> Any:
            self.generic_visit(call)
            func = call.func
            replace_first = False
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                replace_first = (
                    func.value.id == "logger"
                    or (func.value.id == "table" and func.attr == "add_column")
                )
            elif isinstance(func, ast.Name) and func.id == "Text":
                replace_first = True
            if replace_first and call.args:
                call.args[0] = ast.Constant("<FIRST_PARTY_MESSAGE>")
            return call

        def visit_Raise(self, raise_node: ast.Raise) -> Any:
            self.generic_visit(raise_node)
            if isinstance(raise_node.exc, ast.Call) and raise_node.exc.args:
                raise_node.exc.args[0] = ast.Constant("<FIRST_PARTY_MESSAGE>")
            return raise_node

        def visit_Return(self, return_node: ast.Return) -> Any:
            self.generic_visit(return_node)
            value = return_node.value
            if (
                isinstance(value, ast.Tuple)
                and len(value.elts) == 2
                and isinstance(value.elts[0], ast.Constant)
                and isinstance(value.elts[0].value, str)
                and isinstance(value.elts[1], ast.Constant)
                and isinstance(value.elts[1].value, str)
            ):
                value.elts[0] = ast.Constant("<FIRST_PARTY_MESSAGE>")
            return return_node

    normalized = Normalizer().visit(cloned)
    ast.fix_missing_locations(normalized)
    return ast.dump(normalized, include_attributes=False)


def _critical_names_by_path() -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for path, name in CRITICAL_FUNCTIONS:
        result.setdefault(path, []).append(name)
    return result


def _critical_function_mismatches(base: str, head: str) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for path, names in _critical_names_by_path().items():
        base_functions = _qualified_functions(ast.parse(_source_at(base, path)))
        head_functions = _qualified_functions(ast.parse(_source_at(head, path)))
        for name in names:
            base_node = base_functions.get(name)
            head_node = head_functions.get(name)
            if base_node is None or head_node is None:
                findings.append({"path": path, "symbol": name, "reason": "missing"})
                continue
            if _normalize_translation_literals(base_node) != _normalize_translation_literals(head_node):
                findings.append({"path": path, "symbol": name, "reason": "ast_changed"})
    return findings


def _literal_assignments(source: str) -> dict[str, str]:
    result: dict[str, str] = {}
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in CRITICAL_LITERAL_NAMES:
                result[name] = ast.dump(node.value, include_attributes=False)
    return result


def _critical_literal_mismatches(base: str, head: str) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for path in ("module/ocr/al_ocr.py", "module/ocr/ncnn_ocr.py"):
        base_values = _literal_assignments(_source_at(base, path))
        head_values = _literal_assignments(_source_at(head, path))
        for name in sorted(set(base_values) | set(head_values)):
            if base_values.get(name) != head_values.get(name):
                findings.append({"path": path, "symbol": name, "reason": "literal_changed"})
    return findings


def _logger_sequence(node: ast.AST) -> list[str]:
    sequence: list[tuple[int, int, str]] = []
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and isinstance(child.func.value, ast.Name)
            and child.func.value.id == "logger"
        ):
            sequence.append((child.lineno, child.col_offset, child.func.attr))
    return [severity for _line, _column, severity in sorted(sequence)]


def _logger_sequence_mismatches(base: str, head: str) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for path, names in _critical_names_by_path().items():
        base_functions = _qualified_functions(ast.parse(_source_at(base, path)))
        head_functions = _qualified_functions(ast.parse(_source_at(head, path)))
        for name in names:
            base_node = base_functions.get(name)
            head_node = head_functions.get(name)
            if base_node is None or head_node is None:
                continue
            base_sequence = _logger_sequence(base_node)
            head_sequence = _logger_sequence(head_node)
            if base_sequence != head_sequence:
                findings.append(
                    {
                        "path": path,
                        "symbol": name,
                        "base": base_sequence,
                        "head": head_sequence,
                    }
                )
    return findings


def _run_probe(source_root: Path, output: Path) -> dict[str, Any]:
    probe = ROOT / "dev_tools" / "stage8b_output_probe.py"
    completed = subprocess.run(
        [sys.executable, str(probe), "--source-root", str(source_root), "--output", str(output)],
        cwd=ROOT, check=False, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if completed.returncode != 0:
        raise ContractError(
            "Output probe failed: " + (completed.stderr.strip() or completed.stdout.strip())
        )
    return json.loads(output.read_text(encoding="utf-8"))


def _different(base_values: dict[str, Any], head_values: dict[str, Any], keys: tuple[str, ...]) -> int:
    return int(any(base_values.get(key) != head_values.get(key) for key in keys))


def build_output_contract(output_dir: Path) -> tuple[dict[str, Any], dict[str, int]]:
    base = IMMUTABLE_STAGE8B_BASE_SHA
    head = _git("rev-parse", "HEAD")
    _git("rev-parse", "--verify", base)
    if head == base:
        raise ContractError("Запрещён self-diff: Stage 8B head совпадает с immutable base.")

    function_findings = _critical_function_mismatches(base, head)
    literal_findings = _critical_literal_mismatches(base, head)
    logger_findings = _logger_sequence_mismatches(base, head)
    with tempfile.TemporaryDirectory(prefix="stage8b-output-") as temp:
        temp_path = Path(temp)
        base_worktree = temp_path / "base"
        _git("worktree", "add", "--detach", str(base_worktree), base)
        try:
            base_probe = _run_probe(base_worktree, temp_path / "base.json")
            head_probe = _run_probe(ROOT, temp_path / "head.json")
        finally:
            try:
                _git("worktree", "remove", str(base_worktree))
            except ContractError:
                shutil.rmtree(base_worktree, ignore_errors=True)
                _git("worktree", "prune")

    base_values = base_probe["values"]
    head_values = head_probe["values"]
    environment_mismatch = base_probe["environment"] != head_probe["environment"]

    core_keys = (
        "digit", "counter", "duration_valid", "duration_compact", "duration_invalid",
        "campaign_double_hyphen", "campaign_i_correction", "campaign_two_digit",
        "video_memory", "ncnn_shape", "ncnn_dtype", "ncnn_values", "ctc_text",
    )
    metrics = {
        "stage8b_control_flow_mismatches": len(function_findings),
        "stage8b_output_value_mismatches": _different(base_values, head_values, core_keys),
        "stage8b_score_mismatches": _different(
            base_values, head_values, ("ctc_score", "detection_scores")
        ),
        "stage8b_box_mismatches": _different(base_values, head_values, ("detection_boxes",)),
        "stage8b_result_order_mismatches": _different(
            base_values, head_values, ("detection", "detection_text")
        ),
        "stage8b_model_selection_mismatches": int(
            bool(literal_findings)
            or _different(base_values, head_values, ("model_versions",))
            or any(finding["symbol"] in MODEL_SELECTION_SYMBOLS for finding in function_findings)
        ),
        "stage8b_model_version_mismatches": _different(
            base_values, head_values, ("model_versions",)
        ),
        "stage8b_provider_order_mismatches": _different(
            base_values, head_values, ("provider_auto", "provider_gpu")
        ),
        "stage8b_backend_fallback_mismatches": int(
            any(finding["symbol"] in BACKEND_FALLBACK_SYMBOLS for finding in function_findings)
        ),
        "stage8b_threshold_mismatches": _different(
            base_values, head_values,
            ("constructor_defaults", "ctc_image_height", "ctc_max_width"),
        ),
        "stage8b_alphabet_mismatches": _different(
            base_values, head_values,
            ("constructor_defaults", "ctc_alphabet", "ctc_blank_id"),
        ),
        "stage8b_cache_key_mismatches": _different(base_values, head_values, ("cache_key",)),
        "stage8b_queue_contract_mismatches": int(
            _different(base_values, head_values, ("queue_value",))
            or any(finding["symbol"] in QUEUE_SYMBOLS for finding in function_findings)
        ),
        "stage8b_severity_mismatches": len(logger_findings),
        "stage8b_sequence_mismatches": len(logger_findings),
        "stage8b_environment_fingerprint_mismatches": int(environment_mismatch),
    }
    status = "PASS" if not any(metrics.values()) else "FAIL"
    payload = {
        "status": status,
        "base_sha": base,
        "head_sha": head,
        "isolated_checkouts": True,
        "critical_function_findings": function_findings,
        "critical_literal_findings": literal_findings,
        "logger_sequence_findings": logger_findings,
        "environment_fingerprint": head_probe["environment"],
        "base_values": base_values,
        "head_values": head_values,
        "values_equal": base_values == head_values,
        "environment_equal": not environment_mismatch,
        "metric_evidence": dict(metrics),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "output-contract.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return payload, metrics
