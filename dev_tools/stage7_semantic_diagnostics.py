from __future__ import annotations

import difflib
from collections import defaultdict
from typing import Any

from dev_tools.russianization_audit import AuditEngine
from dev_tools.stage7_log_audit import (
    ALLOWED_INSERTIONS,
    PLACEHOLDER_RE,
    Stage7LogAudit,
    _git,
    _identify,
    _owner,
    _rows,
    _scan,
)


def collect_semantic_findings(audit: Stage7LogAudit) -> list[dict[str, Any]]:
    engine = AuditEngine(audit.root)
    current = _rows(engine, audit.root)
    stage7_paths = {
        row["path"]
        for row in current
        if _owner(row["path"], row["message_or_template"]) == "stage7"
    }

    base = []
    for path in sorted(stage7_paths):
        shown = _git(audit.root, "show", f"{audit.base_sha}:{path}", check=False)
        if shown.returncode == 0:
            base.extend(_scan(engine, path, shown.stdout))
    base = _identify(base)

    before: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    after: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in base:
        before[row["path"]].append(row)
    for row in current:
        if row["path"] in stage7_paths:
            after[row["path"]].append(row)

    findings: list[dict[str, Any]] = []
    for path in sorted(stage7_paths):
        matcher = difflib.SequenceMatcher(
            a=[row["call_kind"] for row in before[path]],
            b=[row["call_kind"] for row in after[path]],
            autojunk=False,
        )
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for old, new in zip(
                    before[path][i1:i2], after[path][j1:j2], strict=True
                ):
                    old_signature = PLACEHOLDER_RE.findall(old["message_or_template"])
                    new_signature = PLACEHOLDER_RE.findall(new["message_or_template"])
                    if old_signature != new_signature:
                        findings.append(
                            {
                                "kind": "placeholder_mismatch",
                                "path": path,
                                "old": old,
                                "new": new,
                                "old_signature": old_signature,
                                "new_signature": new_signature,
                            }
                        )
            elif tag == "replace":
                findings.append(
                    {
                        "kind": "severity_or_call_kind_replace",
                        "path": path,
                        "old": before[path][i1:i2],
                        "new": after[path][j1:j2],
                    }
                )
            elif tag == "delete":
                findings.append(
                    {
                        "kind": "sequence_delete",
                        "path": path,
                        "old": before[path][i1:i2],
                        "new": [],
                    }
                )
            elif tag == "insert":
                unexpected = [
                    row
                    for row in after[path][j1:j2]
                    if row["message_or_template"] not in ALLOWED_INSERTIONS
                ]
                if unexpected:
                    findings.append(
                        {
                            "kind": "sequence_insert",
                            "path": path,
                            "old": [],
                            "new": unexpected,
                        }
                    )
    return findings
