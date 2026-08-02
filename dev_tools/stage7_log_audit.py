from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from dev_tools.russianization_audit import (
    AuditEngine,
    SourceText,
    compact_json_bytes,
    json_bytes,
    language_guess,
    technical_only,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "artifacts" / "stage7"
SCHEMA_VERSION = 2
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff]")
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
LATIN_RE = re.compile(r"[A-Za-z]{2,}")
MOJIBAKE_RE = re.compile(r"(?:[ÐÑ]|\ufffd|вЂ|в„|пїЅ)")
PLACEHOLDER_RE = re.compile(
    r"\{(?:…|[^{}]*)\}|%\([^)]+\)[#0 +\-]?(?:\d+|\*)?(?:\.\d+)?[diouxXeEfFgGcrs]"
    r"|%[#0+\-]?(?:\d+|\*)?(?:\.\d+)?[diouxXeEfFgGcrs%]"
)
STRING_RE = re.compile(r"(?P<quote>['\"])(?P<value>[^'\"\r\n]{1,500})(?P=quote)")
BLOCKING_METRICS = (
    "stage7_unresolved",
    "stage7_placeholder_mismatches",
    "stage7_severity_mismatches",
    "stage7_sequence_mismatches",
    "stage7_raw_payload_violations",
    "stage7_unknown_classifications",
    "stage7_invalid_stage8_transfers",
    "stage7_mojibake_findings",
)
COLUMNS = (
    "path", "stable_identifier", "call_kind", "subsystem", "runtime_owner",
    "message_or_template", "classification", "stage_owner", "translation_required",
    "raw_external_payload", "user_actionable", "evidence",
)


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=check, capture_output=True,
        text=True, encoding="utf-8",
    )


def resolve_base_sha(root: Path, base_ref: str | None = None) -> str:
    candidates = [base_ref, os.environ.get("STAGE7_BASE_REF")]
    if os.environ.get("GITHUB_BASE_REF"):
        candidates.append(f"origin/{os.environ['GITHUB_BASE_REF']}")
    candidates.extend(("origin/personal/stable", "personal/stable", "HEAD^"))
    for candidate in filter(None, candidates):
        result = _git(root, "rev-parse", "--verify", str(candidate), check=False)
        if result.returncode == 0:
            return result.stdout.strip()
    raise RuntimeError("Не удалось определить base SHA Stage 7; передайте --base-ref.")


def _decode(payload: bytes) -> list[dict[str, Any]]:
    table = json.loads(payload)
    return [dict(zip(table["columns"], row, strict=True)) for row in table["entries"]]


def _owner(path: str, message: str) -> str:
    lower = path.lower()
    if lower == "alas.py":
        return "stage7" if message.strip() in {"Start", "Запуск"} else "stage8c"
    if lower in {"gui.py", "mcp_server_sse.py", "module/logger.py"}:
        return "stage7"
    if lower.startswith(("deploy/", "module/webui/", "module/config/")):
        return "stage7"
    if lower.startswith("scripts/"):
        names = {
            "start-azurpilot.ps1", "update-azurpilot.ps1", "repair-azurpilot.ps1",
            "build-azurpilot.ps1", "azurpilot.shortcut.psm1",
        }
        return "stage7" if Path(path).name.lower() in names else "developer"
    if lower.startswith("tests/"):
        return "test"
    if lower.startswith("dev_tools/"):
        return "developer"
    if lower.startswith("module/device/") or "screenshot" in lower or "control" in lower:
        return "stage8a"
    if lower.startswith("module/ocr/") or "ocr" in lower:
        return "stage8b"
    if lower.startswith(("campaign/", "module/campaign/", "module/combat/")) or "fleet" in lower:
        return "stage8d"
    if lower.startswith("module/os") or "operation_siren" in lower or "opsi" in lower:
        return "stage8e"
    if lower.startswith("module/"):
        return "stage8c"
    return "developer"


def _runtime_owner(path: str, owner: str) -> str:
    if path == "gui.py" or path.startswith("module/webui/"):
        return "WebUI/process lifecycle"
    if path == "mcp_server_sse.py":
        return "MCP/SSE lifecycle"
    if path.startswith("deploy/"):
        return "deploy/dependency bootstrap"
    if path.startswith("module/config/"):
        return "configuration infrastructure"
    if path.startswith("scripts/"):
        return "Windows operational command"
    names = {
        "stage8a": "Stage 8A device runtime", "stage8b": "Stage 8B OCR runtime",
        "stage8c": "Stage 8C scheduler/task runtime", "stage8d": "Stage 8D combat runtime",
        "stage8e": "Stage 8E Operation Siren runtime", "developer": "developer tooling",
        "test": "test fixture",
    }
    return names.get(owner, "common infrastructure")


def _machine(text: str) -> bool:
    value = text.strip()
    return (
        value in {"proc", "device", "command", "output", "result.returncode", "self.console", "Exception"}
        or ".join(" in value or ".returncode" in value
        or bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*\([^)]*\)", value))
    )


def _classify(row: dict[str, Any], owner: str) -> tuple[str, bool, str]:
    text = str(row["message_or_template"])
    if owner == "test":
        return "test_fixture", False, "Исполняемый тестовый fixture."
    if owner == "developer":
        return "developer_only_output", False, "Изолированный developer output."
    transfers = {
        "stage8a": "stage8a_device", "stage8b": "stage8b_ocr",
        "stage8c": "stage8c_scheduler", "stage8d": "stage8d_combat",
        "stage8e": "stage8e_operation_siren",
    }
    if owner in transfers:
        return transfers[owner], bool(row["translation_required"]), _runtime_owner(row["path"], owner)
    if row["first_party_or_external"] == "external_raw" or _machine(text):
        return "raw_external_payload", False, "Машинное значение сохраняется без перевода."
    if technical_only(text) or language_guess(text) == "neutral":
        return "technical_identifier", False, "Точечный технический идентификатор."
    if CYRILLIC_RE.search(text):
        return "stage7_first_party_message", False, "First-party контекст на русском языке."
    if CJK_RE.search(text) or LATIN_RE.search(text):
        return "stage7_first_party_message", True, "First-party сообщение требует русификации."
    return "unknown", True, "Классификация не доказана."


def _identify(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counters: Counter[str] = Counter()
    result = []
    for row in sorted(rows, key=lambda item: (item["path"], item["line"], item["call_kind"])):
        item = dict(row)
        counters[item["path"]] += 1
        item["stable_identifier"] = f"log-call:{counters[item['path']]:04d}"
        result.append(item)
    return result


def _scan(engine: AuditEngine, path: str, text: str) -> list[dict[str, Any]]:
    source = SourceText(path=path, text=text, lines=tuple(text.splitlines()))
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        return engine._python_log_records(path, source)
    if suffix in {".ps1", ".psm1"}:
        return engine._powershell_log_records(path, source)
    if suffix not in {".sh", ".bat", ".cmd"}:
        return []
    records = []
    for line_number, line in enumerate(source.lines, 1):
        if not re.search(r"(?:^|[;&|()])\s*(?:printf|echo)\b", line, re.IGNORECASE):
            continue
        match = STRING_RE.search(line)
        if match:
            kind = "shell.printf" if "printf" in line.lower() else "shell.echo"
            records.append(engine._log_record(path, line_number, kind, match.group("value"), "shell"))
    return records


def _rows(engine: AuditEngine, root: Path) -> list[dict[str, Any]]:
    rows = _decode(engine.build_outputs()["first_party_logs.json"])
    known = {(r["path"], r["line"], r["call_kind"]) for r in rows}
    paths = _git(root, "ls-files").stdout.splitlines()
    for path in paths:
        if path != "mcp_server_sse.py" and Path(path).suffix.lower() not in {".sh", ".bat", ".cmd"}:
            continue
        file = root / path
        if not file.is_file():
            continue
        for row in _scan(engine, path, file.read_text(encoding="utf-8", errors="replace")):
            key = (row["path"], row["line"], row["call_kind"])
            if key not in known:
                rows.append(row)
                known.add(key)
    return _identify(rows)


class Stage7LogAudit:
    def __init__(self, root: Path = ROOT, base_ref: str | None = None) -> None:
        self.root = root.resolve()
        self.base_sha = resolve_base_sha(self.root, base_ref)

    def build(self) -> tuple[dict[str, bytes], dict[str, Any]]:
        engine = AuditEngine(self.root)
        current = _rows(engine, self.root)
        scope, stage7_paths = [], set()
        for row in current:
            owner = _owner(row["path"], row["message_or_template"])
            classification, required, evidence = _classify(row, owner)
            if owner == "stage7":
                stage7_paths.add(row["path"])
            scope.append({
                "path": row["path"], "stable_identifier": row["stable_identifier"],
                "call_kind": row["call_kind"], "subsystem": row["subsystem"],
                "runtime_owner": _runtime_owner(row["path"], owner),
                "message_or_template": row["message_or_template"],
                "classification": classification, "stage_owner": owner,
                "translation_required": required,
                "raw_external_payload": bool(row["raw_external_payload_preserved"]),
                "user_actionable": row["user_actionable"], "evidence": evidence,
            })

        base = []
        for path in sorted(stage7_paths):
            shown = _git(self.root, "show", f"{self.base_sha}:{path}", check=False)
            if shown.returncode == 0:
                base.extend(_scan(engine, path, shown.stdout))
        base = _identify(base)
        before, after = defaultdict(list), defaultdict(list)
        for row in base:
            before[row["path"]].append(row)
        for row in current:
            if row["path"] in stage7_paths:
                after[row["path"]].append(row)
        placeholders = severity = sequence = raw = 0
        for path in sorted(stage7_paths):
            matcher = difflib.SequenceMatcher(
                a=[r["call_kind"] for r in before[path]],
                b=[r["call_kind"] for r in after[path]], autojunk=False,
            )
            for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                if tag == "equal":
                    for old, new in zip(before[path][i1:i2], after[path][j1:j2], strict=True):
                        if PLACEHOLDER_RE.findall(old["message_or_template"]) != PLACEHOLDER_RE.findall(new["message_or_template"]):
                            placeholders += 1
                        if old["first_party_or_external"] == "external_raw" and old["message_or_template"] != new["message_or_template"]:
                            raw += 1
                elif tag == "replace":
                    severity += max(i2 - i1, j2 - j1)
                else:
                    sequence += (i2 - i1) if tag == "delete" else (j2 - j1)

        stage7 = [entry for entry in scope if entry["stage_owner"] == "stage7"]
        transfers = [entry for entry in scope if str(entry["stage_owner"]).startswith("stage8")]
        metrics = {
            "schema_version": SCHEMA_VERSION, "base_sha": self.base_sha,
            "stage7_candidates_total": len(stage7),
            "stage7_translated": sum(e["classification"] == "stage7_first_party_message" and not e["translation_required"] for e in stage7),
            "stage7_reviewed_technical": sum(e["classification"] in {"technical_identifier", "raw_external_payload"} for e in stage7),
            "stage7_unresolved": sum(e["translation_required"] for e in stage7),
            "stage7_placeholder_mismatches": placeholders,
            "stage7_severity_mismatches": severity,
            "stage7_sequence_mismatches": sequence,
            "stage7_raw_payload_violations": raw,
            "stage7_unknown_classifications": sum(e["classification"] == "unknown" for e in stage7),
            "stage7_invalid_stage8_transfers": sum(not e["runtime_owner"].startswith("Stage 8") or not e["evidence"] for e in transfers),
            "stage7_mojibake_findings": sum(bool(MOJIBAKE_RE.search(e["message_or_template"])) for e in stage7),
            "remaining_log_translation_count": json.loads(engine.build_outputs()["summary.json"])["log_translation_required"],
        }
        table = {"schema_version": SCHEMA_VERSION, "columns": list(COLUMNS), "entries": [[e[c] for c in COLUMNS] for e in scope]}
        status = "PASS" if not any(metrics[key] for key in BLOCKING_METRICS) else "FAIL"
        report = "# Stage 7 — семантический аудит журналов\n\n" + f"Статус: **{status}**\n\n" + "\n".join(f"- {key}: {value}" for key, value in metrics.items()) + "\n"
        return {"scope.json": compact_json_bytes(table), "metrics.json": json_bytes(metrics), "report.md": report.encode()}, metrics

    def write(self, output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
        outputs, metrics = self.build()
        output_dir.mkdir(parents=True, exist_ok=True)
        for name, content in outputs.items():
            (output_dir / name).write_bytes(content)
        return metrics

    def check(self) -> list[str]:
        _, metrics = self.build()
        return [f"{key}: {metrics[key]}" for key in BLOCKING_METRICS if metrics[key]]


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Семантический аудит журналов Stage 7")
    parser.add_argument("--base-ref")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.write == args.check:
        parser.error("укажите ровно один режим: --write или --check")
    audit = Stage7LogAudit(base_ref=args.base_ref)
    metrics = audit.write(args.output_dir)
    failures = [] if args.write else [f"{key}: {metrics[key]}" for key in BLOCKING_METRICS if metrics[key]]
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    if failures:
        return 1
    print(f"Stage 7 log audit: PASS (candidates={metrics['stage7_candidates_total']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
