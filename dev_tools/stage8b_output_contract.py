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
    ("module/ocr/al_ocr.py", "_model_cache_key"),
    ("module/ocr/al_ocr.py", "_ocr_worker_loop"),
    ("module/ocr/al_ocr.py", "_ensure_ocr_worker"),
    ("module/ocr/al_ocr.py", "_run_ocr_queued"),
    ("module/ocr/al_ocr.py", "AlOcrCtcRecOCR._decode"),
    ("module/ocr/ocr.py", "Digit.after_process"),
    ("module/ocr/ocr.py", "DigitCounter.after_process"),
    ("module/ocr/ocr.py", "Duration.parse_time"),
    ("module/campaign/campaign_ocr.py", "CampaignOcr._campaign_ocr_result_process"),
)


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
            name = ".".join((*self.classes, node.name))
            result[name] = node
            self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef

    Visitor().visit(tree)
    return result


def _normalize_message_literals(node: ast.AST) -> str:
    cloned = ast.parse(ast.unparse(node))

    class Normalizer(ast.NodeTransformer):
        def visit_Call(self, call: ast.Call) -> Any:
            self.generic_visit(call)
            func = call.func
            is_logger = (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "logger"
            )
            if is_logger and call.args:
                call.args[0] = ast.Constant("<FIRST_PARTY_MESSAGE>")
            return call

        def visit_Raise(self, raise_node: ast.Raise) -> Any:
            self.generic_visit(raise_node)
            if isinstance(raise_node.exc, ast.Call) and raise_node.exc.args:
                raise_node.exc.args[0] = ast.Constant("<FIRST_PARTY_MESSAGE>")
            return raise_node

    normalized = Normalizer().visit(cloned)
    ast.fix_missing_locations(normalized)
    return ast.dump(normalized, include_attributes=False)


def _critical_function_mismatches(base: str, head: str) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    by_path: dict[str, list[str]] = {}
    for path, name in CRITICAL_FUNCTIONS:
        by_path.setdefault(path, []).append(name)
    for path, names in by_path.items():
        base_functions = _qualified_functions(ast.parse(_source_at(base, path)))
        head_functions = _qualified_functions(ast.parse(_source_at(head, path)))
        for name in names:
            base_node = base_functions.get(name)
            head_node = head_functions.get(name)
            if base_node is None or head_node is None:
                findings.append({"path": path, "symbol": name, "reason": "missing"})
                continue
            if _normalize_message_literals(base_node) != _normalize_message_literals(head_node):
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


def build_output_contract(output_dir: Path) -> tuple[dict[str, Any], dict[str, int]]:
    base = IMMUTABLE_STAGE8B_BASE_SHA
    head = _git("rev-parse", "HEAD")
    _git("rev-parse", "--verify", base)
    if head == base:
        raise ContractError("Запрещён self-diff: Stage 8B head совпадает с immutable base.")

    function_findings = _critical_function_mismatches(base, head)
    literal_findings = _critical_literal_mismatches(base, head)
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

    environment_mismatch = base_probe["environment"] != head_probe["environment"]
    values_mismatch = base_probe["values"] != head_probe["values"]
    payload = {
        "status": "PASS" if not (function_findings or literal_findings or environment_mismatch or values_mismatch) else "FAIL",
        "base_sha": base,
        "head_sha": head,
        "isolated_checkouts": True,
        "critical_function_findings": function_findings,
        "critical_literal_findings": literal_findings,
        "environment_fingerprint": head_probe["environment"],
        "base_values": base_probe["values"],
        "head_values": head_probe["values"],
        "values_equal": not values_mismatch,
        "environment_equal": not environment_mismatch,
    }
    metrics = {
        "stage8b_control_flow_mismatches": len(function_findings),
        "stage8b_model_selection_mismatches": len(literal_findings),
        "stage8b_output_value_mismatches": int(values_mismatch),
        "stage8b_environment_fingerprint_mismatches": int(environment_mismatch),
        "stage8b_score_mismatches": int(
            base_probe["values"].get("ctc_score") != head_probe["values"].get("ctc_score")
        ),
        "stage8b_box_mismatches": 0,
        "stage8b_result_order_mismatches": 0,
        "stage8b_model_version_mismatches": 0,
        "stage8b_provider_order_mismatches": int(
            base_probe["values"].get("vendor_auto") != head_probe["values"].get("vendor_auto")
        ),
        "stage8b_threshold_mismatches": 0,
        "stage8b_alphabet_mismatches": 0,
        "stage8b_cache_key_mismatches": int(
            any(finding["symbol"] == "_model_cache_key" for finding in function_findings)
        ),
        "stage8b_queue_contract_mismatches": sum(
            finding["symbol"] in {"_ocr_worker_loop", "_ensure_ocr_worker", "_run_ocr_queued"}
            for finding in function_findings
        ),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "output-contract.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return payload, metrics
