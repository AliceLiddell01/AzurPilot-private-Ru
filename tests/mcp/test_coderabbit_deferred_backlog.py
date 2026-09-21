from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from azurpilot.cli import build_parser
from azurpilot.integrations import coderabbit
from azurpilot.integrations.config import IntegrationConfig
from azurpilot.integrations.contracts import IntegrationState
from azurpilot.tooling.contracts import (
    CodeRabbitConflictKind,
    CodeRabbitDeferralReason,
    CodeRabbitFinding,
    CodeRabbitFindingTriage,
    CodeRabbitTriageEntry,
    CodeRabbitTriageManifest,
    FindingDisposition,
    FindingSeverity,
)

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


def _finding(index: int, *, impact: str | None = None) -> CodeRabbitFinding:
    return CodeRabbitFinding(
        severity=FindingSeverity.MINOR,
        path="azurpilot/integrations/coderabbit.py",
        title=f"Finding {index}",
        line=index,
        impact=impact or f"Проверить claim {index}.",
        resolution=f"Изолированно проверить claim {index}.",
    )


def _triage(
    disposition: FindingDisposition,
    *,
    index: int,
    reviewed_head: str = HEAD_SHA,
) -> CodeRabbitFindingTriage:
    return CodeRabbitFindingTriage(
        disposition=disposition,
        reviewed_head=reviewed_head,
        affected_code=f"Код finding {index} проверен на exact reviewed head.",
        call_sites=f"Связанные call sites finding {index} проверены.",
        nearest_tests=f"Ближайшие tests finding {index} проверены.",
        relevant_contracts=f"Relevant contracts finding {index} проверены.",
        claimed_impact=f"Заявленный impact finding {index} сопоставлен с кодом.",
        decision_reason=(
            f"Finding {index} технически правдоподобен, но не относится к scope текущей task."
            if disposition is FindingDisposition.DEFERRED
            else f"Finding {index} проверен по коду, call sites и ближайшим tests."
        ),
        change_summary=(
            f"Finding {index} сохранён для отдельной remediation task без изменения текущего scope."
            if disposition is FindingDisposition.DEFERRED
            else f"Для finding {index} применимость зафиксирована текущей triage-записью."
        ),
        conflict_kind=(
            CodeRabbitConflictKind.REPOSITORY_CONTRACT_CONFLICT
            if disposition is FindingDisposition.FALSE_POSITIVE
            else None
        ),
        deferral_reason=(
            CodeRabbitDeferralReason.TASK_SCOPE
            if disposition is FindingDisposition.DEFERRED
            else None
        ),
        authoritative_source=(
            "attached-task-prompt: task scope"
            if disposition is FindingDisposition.DEFERRED
            else ".codex/context/GIT-WORKFLOW.md: contract"
            if disposition is FindingDisposition.FALSE_POSITIVE
            else None
        ),
    )


def _manifest(
    findings: tuple[CodeRabbitFinding, ...],
    dispositions: tuple[FindingDisposition, ...],
    *,
    reviewed_head: str = HEAD_SHA,
) -> CodeRabbitTriageManifest:
    return CodeRabbitTriageManifest(
        base_sha=BASE_SHA,
        reviewed_head=reviewed_head,
        findings=tuple(
            CodeRabbitTriageEntry(
                index=index,
                triage=_triage(
                    disposition,
                    index=index,
                    reviewed_head=reviewed_head,
                ),
            )
            for index, disposition in enumerate(dispositions, start=1)
        ),
    )


def _save_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    root: Path,
    findings: tuple[CodeRabbitFinding, ...],
    *,
    reviewed_head: str = HEAD_SHA,
) -> coderabbit.CodeRabbitAdapter:
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    adapter = coderabbit.CodeRabbitAdapter()
    state = coderabbit._default_review_state()
    state.update(
        {
            "current_cycle_id": "coderabbit-cycle-0123456789abcdef",
            "logical_task_id": "deferred-backlog-task",
            "repository_identity": "hosted:github.com/example/project",
            "reviewed_branch": "integration/example",
            "base_sha": BASE_SHA,
            "reviewed_head": reviewed_head,
            "substantive_iterations": 1,
            "iterations": 1,
            "cycle_status": "triage_required",
            "triage_complete": False,
            "findings": [item.model_dump(mode="json") for item in findings],
            "findings_count": len(findings),
        }
    )
    adapter._save_review_state(root, state)
    return adapter


def test_task_scope_is_deferred_and_task_prompt_conflict_is_not_false_positive():
    legacy_data = _triage(
        FindingDisposition.FALSE_POSITIVE,
        index=1,
    ).model_dump()
    legacy_data["conflict_kind"] = CodeRabbitConflictKind.TASK_PROMPT_CONFLICT
    legacy_data["authoritative_source"] = "attached-task-prompt: scope"
    with pytest.raises(ValidationError):
        CodeRabbitFindingTriage(**legacy_data)

    deferred = _triage(FindingDisposition.DEFERRED, index=1)
    assert deferred.deferral_reason is CodeRabbitDeferralReason.TASK_SCOPE


def test_typed_triage_clears_legacy_historical_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    root = tmp_path / "checkout"
    root.mkdir()
    findings = (_finding(1),)
    adapter = _save_state(monkeypatch, tmp_path, root, findings)
    state = adapter._load_review_state(root)
    state["historical_non_authoritative_findings"] = [{"identifier": "legacy"}]
    adapter._save_review_state(root, state)

    outcome = adapter._triage_unlocked(
        root,
        IntegrationConfig(),
        manifest=_manifest(findings, (FindingDisposition.DEFERRED,)),
    )

    assert outcome.coderabbit_cycle is not None
    assert outcome.coderabbit_cycle.historical_non_authoritative_count == 0
    assert outcome.coderabbit_cycle.triage_required is False


def test_all_deferred_triage_is_terminal_and_persists_ignored_backlog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    root = tmp_path / "checkout"
    root.mkdir()
    findings = (_finding(1), _finding(2))
    adapter = _save_state(monkeypatch, tmp_path, root, findings)

    outcome = adapter._triage_unlocked(
        root,
        IntegrationConfig(),
        manifest=_manifest(
            findings,
            (FindingDisposition.DEFERRED, FindingDisposition.DEFERRED),
        ),
    )

    assert outcome.record.reason_code == "CODERABBIT_TRIAGE_COMPLETE_WITH_DEFERRED"
    assert outcome.record.state is IntegrationState.READY
    assert outcome.coderabbit_cycle is not None
    assert outcome.coderabbit_cycle.cycle_status == "complete_with_deferred_findings"
    assert outcome.coderabbit_cycle.triage_required is False
    assert outcome.coderabbit_cycle.terminal is True
    assert outcome.coderabbit_backlog is not None
    assert len(outcome.coderabbit_backlog.findings) == 2

    backlog_path = root / ".codex" / "local" / "coderabbit-deferred-findings.json"
    document = json.loads(backlog_path.read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    assert all(item["status"] == "open" for item in document["findings"])
    assert all("raw_provider" not in item for item in document["findings"])

    next_review = adapter.review(
        root,
        IntegrationConfig(),
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        task_id="deferred-backlog-task",
    )
    assert next_review.record.reason_code == "CODERABBIT_REVIEW_COMPLETE_WITH_DEFERRED"
    assert next_review.record.state is IntegrationState.READY


def test_deferred_backlog_upsert_deduplicates_and_separates_claims(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    root = tmp_path / "checkout"
    root.mkdir()
    adapter = _save_state(monkeypatch, tmp_path, root, ())
    deferred = _finding(
        1,
        impact="Первый claim с token=super-secret и api_key=another-secret.",
    ).model_copy(
        update={
            "disposition": FindingDisposition.DEFERRED,
            "triage": _triage(FindingDisposition.DEFERRED, index=1),
        }
    )
    state = adapter._load_review_state(root)
    first = adapter._upsert_deferred_backlog(root, state, (deferred,))
    second_state = dict(state)
    second_state["reviewed_head"] = "c" * 40
    second = adapter._upsert_deferred_backlog(root, second_state, (deferred,))
    other_claim = _finding(1, impact="Другой claim на том же path.").model_copy(
        update={
            "disposition": FindingDisposition.DEFERRED,
            "triage": _triage(
                FindingDisposition.DEFERRED,
                index=1,
                reviewed_head="c" * 40,
            ),
        }
    )
    third = adapter._upsert_deferred_backlog(root, second_state, (other_claim,))

    assert len(first.findings) == 1
    assert len(second.findings) == 1
    assert second.findings[0].occurrence_count == 2
    assert len(second.findings[0].occurrence_history) == 2
    assert len(third.findings) == 2
    assert third.findings[0].fingerprint != third.findings[1].fingerprint
    assert "super-secret" not in third.model_dump_json()
    assert "another-secret" not in third.model_dump_json()


def test_deferred_plus_confirmed_keeps_fix_gate_and_backlog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    root = tmp_path / "checkout"
    root.mkdir()
    findings = (_finding(1), _finding(2))
    adapter = _save_state(monkeypatch, tmp_path, root, findings)

    outcome = adapter._triage_unlocked(
        root,
        IntegrationConfig(),
        manifest=_manifest(
            findings,
            (FindingDisposition.DEFERRED, FindingDisposition.CONFIRMED),
        ),
    )

    assert outcome.record.reason_code == "CODERABBIT_TRIAGE_COMPLETE_FIXES_REQUIRED"
    assert outcome.record.state is IntegrationState.DEGRADED
    assert outcome.coderabbit_cycle is not None
    assert outcome.coderabbit_cycle.cycle_status == "fixes_required"
    assert outcome.coderabbit_cycle.triage_required is False
    assert outcome.coderabbit_cycle.terminal is False
    assert outcome.coderabbit_backlog is not None
    assert len(outcome.coderabbit_backlog.findings) == 1

    blocked = adapter.review(
        root,
        IntegrationConfig(),
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        task_id="deferred-backlog-task",
    )
    assert blocked.record.reason_code == "CODERABBIT_FIX_REQUIRED"


def test_all_false_positive_triage_is_terminal_without_backlog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    root = tmp_path / "checkout"
    root.mkdir()
    findings = (_finding(1), _finding(2))
    adapter = _save_state(monkeypatch, tmp_path, root, findings)

    outcome = adapter._triage_unlocked(
        root,
        IntegrationConfig(),
        manifest=_manifest(
            findings,
            (FindingDisposition.FALSE_POSITIVE, FindingDisposition.FALSE_POSITIVE),
        ),
    )

    assert outcome.record.reason_code == "CODERABBIT_TRIAGE_COMPLETE_FOR_TASK"
    assert outcome.record.state is IntegrationState.READY
    assert outcome.coderabbit_backlog is None
    assert outcome.coderabbit_cycle is not None
    assert outcome.coderabbit_cycle.cycle_status == "complete_for_current_task"
    assert outcome.coderabbit_cycle.terminal is True
    assert outcome.coderabbit_cycle.triage_required is False


def test_backlog_read_and_typed_resolve_require_exact_clean_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    root = tmp_path / "checkout"
    root.mkdir()
    finding = _finding(1).model_copy(
        update={
            "disposition": FindingDisposition.DEFERRED,
            "triage": _triage(FindingDisposition.DEFERRED, index=1),
        }
    )
    adapter = _save_state(monkeypatch, tmp_path, root, ())
    state = adapter._load_review_state(root)
    backlog = adapter._upsert_deferred_backlog(root, state, (finding,))
    backlog_id = backlog.findings[0].backlog_id

    read = adapter.backlog(root, IntegrationConfig())
    assert read.record.reason_code == "CODERABBIT_BACKLOG_READ"
    assert read.coderabbit_backlog is not None
    assert read.coderabbit_backlog.findings[0].backlog_id == backlog_id

    class _Git:
        def head(self) -> str:
            return HEAD_SHA

        def status_z(self) -> str:
            return ""

    monkeypatch.setattr(coderabbit, "GitClient", lambda _root: _Git())
    resolved = adapter.resolve_deferred_finding(
        root,
        IntegrationConfig(),
        backlog_id=backlog_id,
        fix_head=HEAD_SHA,
        resolution_summary="Реальный ремонт проверен на clean exact HEAD.",
    )
    assert resolved.record.reason_code == "CODERABBIT_BACKLOG_RESOLVED"
    assert resolved.coderabbit_backlog is not None
    assert resolved.coderabbit_backlog.findings[0].status == "resolved"
    assert resolved.coderabbit_backlog.findings[0].fix_head == HEAD_SHA


def test_backlog_path_is_ignored_and_cli_surface_is_typed():
    repository_root = Path(__file__).resolve().parents[2]
    ignored = subprocess.run(
        [
            "git",
            "check-ignore",
            "--no-index",
            "--quiet",
            "--",
            ".codex/local/coderabbit-deferred-findings.json",
        ],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert ignored.returncode == 0

    parsed = build_parser().parse_args(
        ["integrations", "coderabbit", "backlog", "--json"]
    )
    assert parsed.integration_action == "backlog"
    assert parsed.coderabbit_backlog_action is None

    resolved = build_parser().parse_args(
        [
            "integrations",
            "coderabbit",
            "backlog",
            "resolve",
            "--id",
            "coderabbit-deferred-0123456789abcdef",
            "--fix-head",
            HEAD_SHA,
            "--resolution",
            "Ремонт выполнен и проверен.",
            "--json",
        ]
    )
    assert resolved.coderabbit_backlog_action == "resolve"
    assert resolved.backlog_id == "coderabbit-deferred-0123456789abcdef"
