from __future__ import annotations

import json
from pathlib import Path

import pytest

from azurpilot.integrations import coderabbit
from azurpilot.integrations.config import IntegrationConfig
from azurpilot.integrations.contracts import IntegrationState
from azurpilot.tooling.filesystem import StateLayout
from azurpilot.tooling.process import ProcessIdentity, ProcessResult


def _identity(root: Path) -> ProcessIdentity:
    return ProcessIdentity(
        pid=999999,
        start_time=1.0,
        executable=Path("C:/tools/coderabbit.exe"),
        argv=("C:/tools/coderabbit.exe", "review", "--agent"),
        cwd=root,
    )


def _result(root: Path, *, stdout: str = "", returncode: int = 0) -> ProcessResult:
    identity = _identity(root)
    return ProcessResult(
        returncode=returncode,
        stdout=stdout,
        stderr="",
        stdout_truncated=False,
        stderr_truncated=False,
        timed_out=False,
        pid=identity.pid,
        identity=identity,
    )


class _DiscoveryRunner:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[str, ...]] = []

    def run(self, spec):  # type: ignore[no-untyped-def]
        self.calls.append(spec.argv)
        outputs = {
            ("--version",): "0.7.8\n",
            ("review", "--help"): "--agent --committed --base-commit\n",
            ("auth", "--help"): "status login\n",
            ("auth", "status"): "Signed in\n",
            ("doctor",): "Summary: 9 passed\n",
        }
        return _result(self.root, stdout=outputs.get(spec.argv, ""))


class _Running:
    def __init__(self, result: ProcessResult) -> None:
        self.identity = result.identity
        self.result = result
        self.collected = False

    def collect(self, *, timeout_seconds: float):  # type: ignore[no-untyped-def]
        del timeout_seconds
        self.collected = True
        return self.result


def _ready_provider(root: Path, runner: _DiscoveryRunner) -> coderabbit.NativeCodeRabbit:
    return coderabbit.NativeCodeRabbit(
        executable=Path("C:/tools/coderabbit.exe"),
        version="0.7.8",
        review_help="--agent --committed --base-commit",
        root=root,
        runner=runner,  # type: ignore[arg-type]
    )


def test_native_discovery_requires_binary_syntax_auth_and_doctor(tmp_path: Path, monkeypatch):
    executable = tmp_path / "coderabbit.exe"
    executable.write_bytes(b"native")
    runner = _DiscoveryRunner(tmp_path)
    adapter = coderabbit.CodeRabbitAdapter(runner=runner)  # type: ignore[arg-type]
    monkeypatch.setattr(coderabbit.os, "name", "nt")
    settings = {"route": "direct_native_agent", "executable": str(executable)}

    check = adapter._discover_provider(tmp_path, settings)

    assert check.state is IntegrationState.READY
    assert check.reason_code == "CODERABBIT_NATIVE_READY"
    assert check.provider is not None
    assert check.provider.version == "0.7.8"
    assert {
        "platform=windows-native",
        "review_syntax=agent-committed-base-commit",
        "auth=ready",
        "readiness=doctor_passed",
        "transport=native_process",
    }.issubset(check.diagnostics)
    assert runner.calls == [
        ("--version",),
        ("review", "--help"),
        ("auth", "--help"),
        ("auth", "status"),
        ("doctor",),
    ]


@pytest.mark.parametrize(
    ("name", "expected"),
    [("coderabbit.cmd", "CODERABBIT_EXECUTABLE_WRAPPER_REJECTED"),
     ("other.exe", "CODERABBIT_EXECUTABLE_NOT_NATIVE")],
)
def test_native_discovery_rejects_wrappers_and_other_binaries(
    tmp_path: Path, monkeypatch, name: str, expected: str
):
    executable = tmp_path / name
    executable.write_bytes(b"not-provider")
    adapter = coderabbit.CodeRabbitAdapter()
    monkeypatch.setattr(coderabbit.os, "name", "nt")

    check = adapter._discover_provider(
        tmp_path,
        {"route": "direct_native_agent", "executable": str(executable)},
    )

    assert check.reason_code == expected
    assert check.provider is None


def test_native_discovery_has_no_fallback_when_executable_is_missing(tmp_path: Path, monkeypatch):
    adapter = coderabbit.CodeRabbitAdapter()
    monkeypatch.setattr(coderabbit.os, "name", "nt")
    monkeypatch.setattr(coderabbit.shutil, "which", lambda _name: None)

    check = adapter._discover_provider(tmp_path, {"route": "direct_native_agent"})

    assert check.reason_code == "CODERABBIT_NATIVE_EXECUTABLE_UNAVAILABLE"
    assert check.state is IntegrationState.UNAVAILABLE


def test_status_checks_fresh_canonical_checkout_before_reporting_ready(
    monkeypatch, tmp_path: Path
):
    adapter = coderabbit.CodeRabbitAdapter()
    provider = _ready_provider(tmp_path, _DiscoveryRunner(tmp_path))
    monkeypatch.setattr(
        adapter,
        "_discover_provider",
        lambda *_args: coderabbit.ProviderCheck(
            IntegrationState.READY,
            "CODERABBIT_NATIVE_READY",
            "ready",
            provider=provider,
            configured=True,
            authenticated=True,
        ),
    )
    monkeypatch.setattr(
        adapter,
        "_canonical_checkout_preflight",
        lambda *_args: (None, "CODERABBIT_CANDIDATE_DIRTY", "dirty"),
    )

    record = adapter.status(tmp_path, IntegrationConfig())

    assert record.reason_code == "CODERABBIT_CANDIDATE_DIRTY"
    assert record.state is IntegrationState.INCOMPATIBLE


def test_candidate_preflight_requires_clean_exact_canonical_checkout(monkeypatch, tmp_path: Path):
    class FakeGit:
        def __init__(self, root: Path) -> None:
            self.root = root

        def text(self, *args: str) -> str:
            assert args == ("rev-parse", "--show-toplevel")
            return str(tmp_path)

        def remote_identity(self, remote: str) -> str:
            assert remote == "origin"
            return "hosted:github.com/example/project"

        def head(self) -> str:
            return "b" * 40

        def object_exists(self, _revision: str) -> bool:
            return True

        def is_ancestor(self, _base: str, _head: str) -> bool:
            return True

        def status_z(self) -> str:
            return ""

    monkeypatch.setattr(coderabbit, "GitClient", FakeGit)
    fingerprint, code, detail = coderabbit.CodeRabbitAdapter._candidate_preflight(
        tmp_path,
        base_sha="a" * 40,
        head_sha="b" * 40,
        settings={},
    )

    assert code is None
    assert detail is None
    assert fingerprint is not None
    assert fingerprint.repository_identity == "hosted:github.com/example/project"
    assert fingerprint.status_digest


def test_legacy_active_state_is_migrated_without_becoming_native_active(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "checkout"
    root.mkdir()
    layout = StateLayout.for_repository(root)
    layout.ensure()
    layout.path("coderabbit-review.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "current_cycle_id": "coderabbit-cycle-0123456789abcdef",
                "substantive_iterations": 2,
                "active": True,
                "operation_id": "coderabbit-old",
                "managed_clone": {"path": "/foreign"},
            }
        ),
        encoding="utf-8",
    )

    state = coderabbit.CodeRabbitAdapter._load_review_state(root)

    assert state["schema_version"] == coderabbit.REVIEW_STATE_SCHEMA_VERSION
    assert state["substantive_iterations"] == 2
    assert state["active"] is False
    assert state["provider_identity"] is None
    assert state["candidate_fingerprint"] is None
    assert state["provider_state"] == "legacy_state_migrated"


def test_legacy_default_finding_disposition_is_not_verified_triage(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "checkout"
    root.mkdir()
    layout = StateLayout.for_repository(root)
    layout.ensure()
    layout.path("coderabbit-review.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "current_cycle_id": "coderabbit-cycle-0123456789abcdef",
                "base_sha": "a" * 40,
                "reviewed_head": "b" * 40,
                "substantive_iterations": 2,
                "terminal": True,
                "findings": [
                    {
                        "severity": "major",
                        "path": "azurpilot/tooling/git.py",
                        "impact": "Provider finding",
                        "disposition": "insufficient evidence",
                        "resolution": "Provider suggestion",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    state = coderabbit.CodeRabbitAdapter._load_review_state(root)

    assert state["findings"][0]["disposition"] is None
    assert state["triage_complete"] is False
    assert state["cycle_status"] == "triage_required"
    assert state["terminal"] is False


def test_exact_liveness_blocks_duplicate_review(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "checkout"
    root.mkdir()
    adapter = coderabbit.CodeRabbitAdapter()
    state = coderabbit._default_review_state()
    identity = _identity(root)
    state.update(
        {
            "current_cycle_id": "coderabbit-cycle-0123456789abcdef",
            "active": True,
            "provider_identity": coderabbit._serialize_identity(identity),
        }
    )
    adapter._save_review_state(root, state)
    monkeypatch.setattr(coderabbit.ProcessController, "inspect_state", staticmethod(lambda _identity: "alive"))

    outcome = adapter.review(
        root,
        IntegrationConfig(),
        base_sha="a" * 40,
        head_sha="b" * 40,
    )

    assert outcome.record.reason_code == "CODERABBIT_REVIEW_STILL_ALIVE"
    assert outcome.record.state is IntegrationState.DEGRADED


def test_recovery_only_closes_proven_absent_identity(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "checkout"
    root.mkdir()
    adapter = coderabbit.CodeRabbitAdapter()
    state = coderabbit._default_review_state()
    state.update(
        {
            "current_cycle_id": "coderabbit-cycle-0123456789abcdef",
            "active": True,
            "provider_identity": coderabbit._serialize_identity(_identity(root)),
            "substantive_iterations": 1,
        }
    )
    adapter._save_review_state(root, state)
    monkeypatch.setattr(coderabbit.ProcessController, "inspect_state", staticmethod(lambda _identity: "absent"))

    outcome = adapter.recover_interrupted_review(root, IntegrationConfig())
    recovered = adapter._load_review_state(root)

    assert outcome.record.reason_code == "CODERABBIT_REVIEW_RECOVERED"
    assert recovered["active"] is False
    assert recovered["substantive_iterations"] == 1
    assert recovered["provider_state"] == "interrupted_recovered"


def test_unknown_liveness_does_not_recover_or_start_duplicate(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "checkout"
    root.mkdir()
    adapter = coderabbit.CodeRabbitAdapter()
    state = coderabbit._default_review_state()
    state.update(
        {
            "current_cycle_id": "coderabbit-cycle-0123456789abcdef",
            "active": True,
            "provider_identity": coderabbit._serialize_identity(_identity(root)),
        }
    )
    adapter._save_review_state(root, state)
    monkeypatch.setattr(coderabbit.ProcessController, "inspect_state", staticmethod(lambda _identity: "unknown"))

    outcome = adapter.recover_interrupted_review(root, IntegrationConfig())

    assert outcome.record.reason_code == "CODERABBIT_REVIEW_LIVENESS_UNKNOWN"
    assert adapter._load_review_state(root)["active"] is True


def test_review_persists_native_identity_then_clears_it_after_exact_postcondition(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "checkout"
    root.mkdir()
    runner = _DiscoveryRunner(root)
    adapter = coderabbit.CodeRabbitAdapter(runner=runner)  # type: ignore[arg-type]
    provider = _ready_provider(root, runner)
    fingerprint = coderabbit.CandidateFingerprint(
        root_identity="a" * 24,
        repository_identity="hosted:github.com/example/project",
        base_sha="a" * 40,
        head_sha="b" * 40,
        status_digest="c" * 64,
    )
    review_result = _result(root, stdout=json.dumps({"type": "complete", "findings": []}) + "\n")
    monkeypatch.setattr(adapter, "_discover_provider", lambda *_args: coderabbit.ProviderCheck(IntegrationState.READY, "CODERABBIT_NATIVE_READY", "ready", provider=provider, configured=True, authenticated=True))
    candidate_calls = iter(((fingerprint, None, None), (fingerprint, None, None)))
    monkeypatch.setattr(adapter, "_candidate_preflight", lambda *_args, **_kwargs: next(candidate_calls))
    monkeypatch.setattr(coderabbit.NativeCodeRabbit, "start_review", lambda _self, _base: _Running(review_result))

    outcome = adapter.review(root, IntegrationConfig(), base_sha="a" * 40, head_sha="b" * 40, task_id="task-1")
    state = adapter._load_review_state(root)

    assert outcome.record.reason_code == "CODERABBIT_REVIEW_COMPLETE"
    assert state["substantive_iterations"] == 1
    assert state["terminal"] is True
    assert state["active"] is False
    assert state["provider_identity"] is None


def test_provider_findings_require_individual_triage_before_next_review(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "checkout"
    root.mkdir()
    runner = _DiscoveryRunner(root)
    adapter = coderabbit.CodeRabbitAdapter(runner=runner)  # type: ignore[arg-type]
    provider = _ready_provider(root, runner)
    fingerprint = coderabbit.CandidateFingerprint(
        root_identity="a" * 24,
        repository_identity="hosted:github.com/example/project",
        base_sha="a" * 40,
        head_sha="b" * 40,
        status_digest="c" * 64,
    )
    state = coderabbit._default_review_state()
    state.update(
        {
            "current_cycle_id": "coderabbit-cycle-0123456789abcdef",
            "base_sha": "a" * 40,
            "substantive_iterations": 1,
            "iterations": 1,
        }
    )
    adapter._save_review_state(root, state)
    provider_findings = "\n".join(
        json.dumps(
            {
                "type": "finding",
                "finding": {
                    "path": f"azurpilot/module_{index}.py",
                    "severity": "major",
                    "comment": f"Проверить finding {index}.",
                    "classification": "confirmed",
                },
            },
            ensure_ascii=False,
        )
        for index in range(1, 9)
    )
    review_result = _result(
        root,
        stdout=provider_findings + "\n" + json.dumps({"type": "complete"}) + "\n",
    )
    monkeypatch.setattr(
        adapter,
        "_discover_provider",
        lambda *_args: coderabbit.ProviderCheck(
            IntegrationState.READY,
            "CODERABBIT_NATIVE_READY",
            "ready",
            provider=provider,
            configured=True,
            authenticated=True,
        ),
    )
    candidate_calls = iter(((fingerprint, None, None), (fingerprint, None, None)))
    monkeypatch.setattr(
        adapter,
        "_candidate_preflight",
        lambda *_args, **_kwargs: next(candidate_calls),
    )
    monkeypatch.setattr(
        coderabbit.NativeCodeRabbit,
        "start_review",
        lambda _self, _base: _Running(review_result),
    )

    outcome = adapter.review(
        root,
        IntegrationConfig(),
        base_sha="a" * 40,
        head_sha="b" * 40,
        task_id="task-1",
    )
    saved = adapter._load_review_state(root)

    assert outcome.record.reason_code == "CODERABBIT_TRIAGE_REQUIRED"
    assert outcome.record.state is IntegrationState.DEGRADED
    assert len(outcome.findings) == 8
    assert all(finding.disposition is None for finding in outcome.findings)
    assert saved["substantive_iterations"] == 2
    assert saved["cycle_status"] == "triage_required"
    assert saved["terminal"] is False
    assert saved["triage_complete"] is False
    assert outcome.coderabbit_cycle is not None
    assert outcome.coderabbit_cycle.triaged_findings_count == 0
    assert outcome.coderabbit_cycle.triage_required is True

    blocked = adapter.review(
        root,
        IntegrationConfig(),
        base_sha="a" * 40,
        head_sha="b" * 40,
        task_id="task-1",
    )
    assert blocked.record.reason_code == "CODERABBIT_TRIAGE_REQUIRED"
    assert blocked.record.state is IntegrationState.DEGRADED


def test_triage_requires_evidence_and_new_exact_head_for_confirmed_findings(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "checkout"
    root.mkdir()
    runner = _DiscoveryRunner(root)
    adapter = coderabbit.CodeRabbitAdapter(runner=runner)  # type: ignore[arg-type]
    provider = _ready_provider(root, runner)
    first = coderabbit.CandidateFingerprint(
        "a" * 24, "hosted:github.com/example/project", "a" * 40, "b" * 40, "c" * 64
    )
    second = coderabbit.CandidateFingerprint(
        "a" * 24, "hosted:github.com/example/project", "a" * 40, "c" * 40, "d" * 64
    )
    state = coderabbit._default_review_state()
    state.update(
        {
            "current_cycle_id": "coderabbit-cycle-0123456789abcdef",
            "base_sha": "a" * 40,
            "substantive_iterations": 1,
            "iterations": 1,
        }
    )
    adapter._save_review_state(root, state)
    first_review = "\n".join(
        json.dumps(
            {
                "type": "finding",
                "finding": {
                    "path": f"azurpilot/module_{index}.py",
                    "severity": "major",
                    "comment": f"Проверить finding {index}.",
                },
            },
            ensure_ascii=False,
        )
        for index in range(1, 9)
    )
    provider_results = iter(
        (
            _result(root, stdout=first_review + "\n" + json.dumps({"type": "complete"}) + "\n"),
            _result(root, stdout=json.dumps({"type": "complete", "findings": []}) + "\n"),
        )
    )
    monkeypatch.setattr(
        adapter,
        "_discover_provider",
        lambda *_args: coderabbit.ProviderCheck(
            IntegrationState.READY,
            "CODERABBIT_NATIVE_READY",
            "ready",
            provider=provider,
            configured=True,
            authenticated=True,
        ),
    )
    candidate_calls = iter(
        ((first, None, None), (first, None, None), (second, None, None), (second, None, None))
    )
    monkeypatch.setattr(
        adapter,
        "_candidate_preflight",
        lambda *_args, **_kwargs: next(candidate_calls),
    )
    monkeypatch.setattr(
        coderabbit.NativeCodeRabbit,
        "start_review",
        lambda _self, _base: _Running(next(provider_results)),
    )

    first_outcome = adapter.review(
        root, IntegrationConfig(), base_sha="a" * 40, head_sha="b" * 40, task_id="task-1"
    )
    assert first_outcome.record.reason_code == "CODERABBIT_TRIAGE_REQUIRED"

    manifest = tmp_path / "triage.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_sha": "a" * 40,
                "reviewed_head": "b" * 40,
                "findings": [
                    {
                        "index": index,
                        "triage": {
                            "disposition": "confirmed" if index == 1 else "false positive",
                            "reviewed_head": "b" * 40,
                            "affected_code": f"affected code {index}",
                            "call_sites": f"call sites {index}",
                            "nearest_tests": f"nearest tests {index}",
                            "relevant_contracts": f"relevant contracts {index}",
                            "claimed_impact": f"claimed impact {index}",
                        },
                    }
                    for index in range(1, 9)
                ],
            }
        ),
        encoding="utf-8",
    )
    triaged = adapter.triage(root, IntegrationConfig(), manifest_path=manifest)

    assert triaged.record.reason_code == "CODERABBIT_TRIAGE_COMPLETE_FIXES_REQUIRED"
    assert triaged.record.state is IntegrationState.DEGRADED
    assert triaged.coderabbit_cycle is not None
    assert triaged.coderabbit_cycle.triaged_findings_count == 8
    assert triaged.coderabbit_cycle.triage_required is False
    assert triaged.coderabbit_cycle.terminal is False

    same_head = adapter.review(
        root, IntegrationConfig(), base_sha="a" * 40, head_sha="b" * 40, task_id="task-1"
    )
    assert same_head.record.reason_code == "CODERABBIT_FIX_REQUIRED"

    after_fixes = adapter.review(
        root, IntegrationConfig(), base_sha="a" * 40, head_sha="c" * 40, task_id="task-1"
    )
    final_state = adapter._load_review_state(root)
    assert after_fixes.record.reason_code == "CODERABBIT_REVIEW_COMPLETE"
    assert final_state["substantive_iterations"] == 3
    assert final_state["reviewed_head"] == "c" * 40
    assert final_state["terminal"] is True


def test_review_postcondition_mismatch_is_not_substantive_success(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "checkout"
    root.mkdir()
    runner = _DiscoveryRunner(root)
    adapter = coderabbit.CodeRabbitAdapter(runner=runner)  # type: ignore[arg-type]
    provider = _ready_provider(root, runner)
    first = coderabbit.CandidateFingerprint("a" * 24, "hosted:github.com/example/project", "a" * 40, "b" * 40, "c" * 64)
    changed = coderabbit.CandidateFingerprint("a" * 24, "hosted:github.com/example/project", "a" * 40, "b" * 40, "d" * 64)
    review_result = _result(root, stdout=json.dumps({"type": "complete", "findings": []}) + "\n")
    monkeypatch.setattr(adapter, "_discover_provider", lambda *_args: coderabbit.ProviderCheck(IntegrationState.READY, "CODERABBIT_NATIVE_READY", "ready", provider=provider, configured=True, authenticated=True))
    candidate_calls = iter(((first, None, None), (changed, None, None)))
    monkeypatch.setattr(adapter, "_candidate_preflight", lambda *_args, **_kwargs: next(candidate_calls))
    monkeypatch.setattr(coderabbit.NativeCodeRabbit, "start_review", lambda _self, _base: _Running(review_result))

    outcome = adapter.review(root, IntegrationConfig(), base_sha="a" * 40, head_sha="b" * 40)
    state = adapter._load_review_state(root)

    assert outcome.record.reason_code == "CODERABBIT_CANDIDATE_CHANGED"
    assert state["substantive_iterations"] == 0
    assert state["provider_state"] == "candidate_changed"


def test_rate_limit_metadata_and_budget_remain_bounded():
    error = coderabbit.CodeRabbitStreamError("CODERABBIT_RATE_LIMITED", rate_limited=True, retry_source="provider")
    assert error.retry_source == "provider"
    assert coderabbit.review_iteration_allowed(0)
    assert coderabbit.review_iteration_allowed(2)
    assert not coderabbit.review_iteration_allowed(3)
    assert not coderabbit.review_iteration_allowed(0, terminal=True)


def test_parse_provider_findings_output_without_suggested_fix() -> None:
    findings = coderabbit.parse_provider_findings_output(
        """major [Без блока исправления]
→ azurpilot/tooling/git.py:12
Описание проблемы без отдельного блока исправления.
"""
    )

    assert len(findings) == 1
    assert findings[0].path == "azurpilot/tooling/git.py"
    assert findings[0].impact == "Описание проблемы без отдельного блока исправления."
    assert findings[0].resolution == coderabbit._DEFAULT_FINDING_RESOLUTION


def test_obsolete_reconcile_route_is_not_in_provider_cli():
    from azurpilot.cli import CliInvocationError, build_parser

    parser = build_parser()
    with pytest.raises(CliInvocationError):
        parser.parse_args(["integrations", "coderabbit", "reconcile"])


def test_coderabbit_cli_exposes_typed_triage_manifest_action():
    from azurpilot.cli import build_parser

    parsed = build_parser().parse_args(
        [
            "integrations",
            "coderabbit",
            "triage",
            "--manifest",
            "C:/temp/coderabbit-triage.json",
            "--json",
        ]
    )

    assert parsed.integration_target == "coderabbit"
    assert parsed.integration_action == "triage"
    assert parsed.manifest == "C:/temp/coderabbit-triage.json"
