from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from azurpilot.integrations import coderabbit
from azurpilot.integrations.config import IntegrationConfig
from azurpilot.integrations.contracts import IntegrationState
from azurpilot.tooling.contracts import ResultCode
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


def _result(
    root: Path,
    *,
    stdout: str = "",
    returncode: int = 0,
    timed_out: bool = False,
    termination_state: str | None = None,
) -> ProcessResult:
    identity = _identity(root)
    return ProcessResult(
        returncode=returncode,
        stdout=stdout,
        stderr="",
        stdout_truncated=False,
        stderr_truncated=False,
        timed_out=timed_out,
        pid=identity.pid,
        identity=identity,
        termination_state=termination_state,  # type: ignore[arg-type]
    )


def test_legacy_historical_finding_is_copied_before_active_fields_are_cleared():
    candidate = {
        "severity": "minor",
        "path": "azurpilot/integrations/coderabbit.py",
        "title": "legacy evidence",
        "line": 10,
        "impact": "legacy impact",
        "resolution": "legacy resolution",
        "disposition": "insufficient evidence",
        "fix_head": "b" * 40,
    }

    state = coderabbit.CodeRabbitAdapter._normalise_state(
        {
            "schema_version": 5,
            "findings": [candidate],
        },
        migrated=True,
    )

    historical = state["historical_non_authoritative_findings"]
    assert isinstance(historical, list)
    assert historical == [candidate]
    assert state["findings"][0]["disposition"] is None
    assert state["findings"][0]["fix_head"] is None


class _DiscoveryRunner:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[str, ...]] = []

    def with_repository_config(self) -> _DiscoveryRunner:
        (self.root / ".coderabbit.yaml").write_text(
            "language: ru-RU\n", encoding="utf-8"
        )
        return self

    def run(self, spec):  # type: ignore[no-untyped-def]
        self.calls.append(spec.argv)
        outputs = {
            ("--version",): "0.7.8\n",
            ("review", "--help"): "--agent --committed --base-commit\n",
            ("auth", "--help"): "status login\n",
            ("auth", "status"): "Signed in\n",
            ("doctor",): "Summary: 9 passed\n",
            ("config", "validate", ".coderabbit.yaml"): "Configuration is valid\n",
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


class _StartCapture:
    def __init__(self) -> None:
        self.argv: tuple[str, ...] | None = None

    def start(self, spec):  # type: ignore[no-untyped-def]
        self.argv = spec.argv
        return object()


def _ready_provider(root: Path, runner: _DiscoveryRunner) -> coderabbit.NativeCodeRabbit:
    return coderabbit.NativeCodeRabbit(
        executable=Path("C:/tools/coderabbit.exe"),
        version="0.7.8",
        review_help="--agent --committed --base-commit",
        root=root,
        runner=runner,  # type: ignore[arg-type]
    )


def _prepared_adapter(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    results: tuple[ProcessResult, ...],
    fingerprints: tuple[coderabbit.CandidateFingerprint, ...],
) -> coderabbit.CodeRabbitAdapter:
    runner = _DiscoveryRunner(root)
    adapter = coderabbit.CodeRabbitAdapter(runner=runner)  # type: ignore[arg-type]
    provider = _ready_provider(root, runner)
    result_iterator = iter(results)
    fingerprint_iterator = iter(fingerprints)
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
        "_candidate_preflight",
        lambda *_args, **_kwargs: (next(fingerprint_iterator), None, None),
    )
    monkeypatch.setattr(
        coderabbit.NativeCodeRabbit,
        "start_review",
        lambda _self, _base: _Running(next(result_iterator)),
    )
    return adapter


def test_native_discovery_requires_binary_syntax_auth_and_doctor(tmp_path: Path):
    executable = tmp_path / "coderabbit.exe"
    executable.write_bytes(b"native")
    runner = _DiscoveryRunner(tmp_path).with_repository_config()
    adapter = coderabbit.CodeRabbitAdapter(runner=runner, host_os="nt")  # type: ignore[arg-type]
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
        "repository_config=.coderabbit.yaml",
        "config_validation=passed",
        "effective_config_provenance=not_observable_through_native_surface",
        "transport=native_process",
    }.issubset(check.diagnostics)
    assert runner.calls == [
        ("--version",),
        ("review", "--help"),
        ("auth", "--help"),
        ("auth", "status"),
        ("doctor",),
        ("config", "validate", ".coderabbit.yaml"),
    ]


def test_native_status_probe_checks_only_executable_and_version(tmp_path: Path):
    executable = tmp_path / "coderabbit.exe"
    executable.write_bytes(b"native")
    runner = _DiscoveryRunner(tmp_path)
    adapter = coderabbit.CodeRabbitAdapter(runner=runner, host_os="nt")  # type: ignore[arg-type]

    check = adapter._discover_provider(
        tmp_path,
        {"route": "direct_native_agent", "executable": str(executable)},
        False,
    )

    assert check.state is IntegrationState.READY
    assert check.provider is not None
    assert check.provider.version == "0.7.8"
    assert runner.calls == [("--version",)]


def test_native_review_does_not_use_config_flag_to_enable_repository_config(
    tmp_path: Path,
):
    runner = _StartCapture()
    provider = coderabbit.NativeCodeRabbit(
        executable=Path("C:/tools/coderabbit.exe"),
        version="0.7.8",
        review_help="--agent --committed --base-commit",
        root=tmp_path,
        runner=runner,  # type: ignore[arg-type]
    )

    provider.start_review("a" * 40)

    assert runner.argv == (
        "review",
        "--agent",
        "--committed",
        "--base-commit",
        "a" * 40,
    )
    assert "--config" not in runner.argv


@pytest.mark.parametrize(
    ("name", "expected"),
    [("coderabbit.cmd", "CODERABBIT_EXECUTABLE_WRAPPER_REJECTED"),
     ("other.exe", "CODERABBIT_EXECUTABLE_NOT_NATIVE")],
)
def test_native_discovery_rejects_wrappers_and_other_binaries(
    tmp_path: Path, name: str, expected: str
):
    executable = tmp_path / name
    executable.write_bytes(b"not-provider")
    adapter = coderabbit.CodeRabbitAdapter(host_os="nt")

    check = adapter._discover_provider(
        tmp_path,
        {"route": "direct_native_agent", "executable": str(executable)},
    )

    assert check.reason_code == expected
    assert check.provider is None


def test_native_discovery_has_no_fallback_when_executable_is_missing(tmp_path: Path, monkeypatch):
    adapter = coderabbit.CodeRabbitAdapter(host_os="nt")
    monkeypatch.setattr(coderabbit.shutil, "which", lambda _name: None)

    check = adapter._discover_provider(tmp_path, {"route": "direct_native_agent"})

    assert check.reason_code == "CODERABBIT_NATIVE_EXECUTABLE_UNAVAILABLE"
    assert check.state is IntegrationState.UNAVAILABLE


def test_native_discovery_uses_posix_provider_name_without_mutating_platform(
    tmp_path: Path,
):
    executable = tmp_path / "coderabbit"
    executable.write_bytes(b"native")
    executable.chmod(0o755)
    runner = _DiscoveryRunner(tmp_path).with_repository_config()
    adapter = coderabbit.CodeRabbitAdapter(runner=runner, host_os="posix")  # type: ignore[arg-type]

    check = adapter._discover_provider(
        tmp_path,
        {"route": "direct_native_agent", "executable": str(executable)},
    )

    assert check.state is IntegrationState.READY
    assert check.provider is not None
    assert "platform=posix-native" in check.diagnostics


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable discovery contract")
def test_native_discovery_allows_posix_path_symlink_to_host_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "coderabbit"
    target.write_bytes(b"native")
    target.chmod(0o755)
    entry = tmp_path / "bin" / "coderabbit"
    entry.parent.mkdir()
    entry.symlink_to(target)
    monkeypatch.setattr(coderabbit.shutil, "which", lambda _name: str(entry))
    runner = _DiscoveryRunner(tmp_path).with_repository_config()
    adapter = coderabbit.CodeRabbitAdapter(runner=runner, host_os="posix")  # type: ignore[arg-type]

    check = adapter._discover_provider(
        tmp_path,
        {"route": "direct_native_agent"},
    )

    assert check.state is IntegrationState.READY
    assert check.provider is not None
    assert check.provider.executable == target.resolve()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable discovery contract")
def test_native_discovery_rejects_posix_path_symlink_to_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "coderabbit"
    target.write_text("#!/bin/sh\necho fake\n", encoding="utf-8")
    target.chmod(0o755)
    entry = tmp_path / "bin" / "coderabbit"
    entry.parent.mkdir()
    entry.symlink_to(target)
    monkeypatch.setattr(coderabbit.shutil, "which", lambda _name: str(entry))
    adapter = coderabbit.CodeRabbitAdapter(host_os="posix")

    check = adapter._discover_provider(tmp_path, {"route": "direct_native_agent"})

    assert check.reason_code == "CODERABBIT_EXECUTABLE_WRAPPER_REJECTED"
    assert check.provider is None


@pytest.mark.parametrize(
    ("host_os", "name"),
    [("nt", "coderabbit"), ("posix", "coderabbit.exe")],
)
def test_native_discovery_rejects_provider_name_for_other_host(
    tmp_path: Path, host_os: str, name: str
):
    executable = tmp_path / name
    executable.write_bytes(b"native")
    adapter = coderabbit.CodeRabbitAdapter(host_os=host_os)

    check = adapter._discover_provider(
        tmp_path,
        {"route": "direct_native_agent", "executable": str(executable)},
    )

    assert check.reason_code == "CODERABBIT_EXECUTABLE_NOT_NATIVE"
    assert check.provider is None


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
    monkeypatch.setattr(
        coderabbit,
        "load_deploy_settings",
        lambda _root: SimpleNamespace(
            repository_url="git@github.com:example/project.git"
        ),
    )
    fingerprint, code, detail = coderabbit.CodeRabbitAdapter._candidate_preflight(
        tmp_path,
        base_sha="a" * 40,
        head_sha="b" * 40,
        settings={"repository": "hosted:github.com/example/project"},
    )

    assert code is None
    assert detail is None
    assert fingerprint is not None
    assert fingerprint.repository_identity == "hosted:github.com/example/project"
    assert fingerprint.status_digest


def test_candidate_preflight_rejects_fork_against_project_owned_repository(
    monkeypatch, tmp_path: Path
):
    class ForkGit:
        def __init__(self, root: Path) -> None:
            self.root = root

        def text(self, *args: str) -> str:
            assert args == ("rev-parse", "--show-toplevel")
            return str(tmp_path)

        def remote_identity(self, remote: str) -> str:
            assert remote == "origin"
            return "hosted:github.com/fork/project"

        def head(self) -> str:
            return "b" * 40

        def object_exists(self, _revision: str) -> bool:
            return True

        def is_ancestor(self, _base: str, _head: str) -> bool:
            return True

        def status_z(self) -> str:
            return ""

    monkeypatch.setattr(coderabbit, "GitClient", ForkGit)
    monkeypatch.setattr(
        coderabbit,
        "load_deploy_settings",
        lambda _root: SimpleNamespace(
            repository_url="git@github.com:example/project.git"
        ),
    )

    fingerprint, code, detail = coderabbit.CodeRabbitAdapter._candidate_preflight(
        tmp_path,
        base_sha="a" * 40,
        head_sha="b" * 40,
        settings={"repository": "hosted:github.com/fork/project"},
    )

    assert fingerprint is None
    assert code == "CODERABBIT_REPOSITORY_IDENTITY_MISMATCH"
    assert detail


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


def test_large_finding_state_remains_readable_for_project_owned_queries(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "checkout"
    root.mkdir()
    layout = StateLayout.for_repository(root)
    layout.ensure()
    long_text = "provider finding " * 40
    layout.path("coderabbit-review.json").write_text(
        json.dumps(
            {
                "schema_version": coderabbit.REVIEW_STATE_SCHEMA_VERSION,
                "findings": [
                    {
                        "severity": "minor",
                        "path": "azurpilot/integrations/coderabbit.py",
                        "impact": long_text,
                        "resolution": long_text,
                    }
                    for _ in range(32)
                ],
            }
        ),
        encoding="utf-8",
    )

    state = coderabbit.CodeRabbitAdapter._load_review_state(root)

    assert len(state["findings"]) == 32


def test_corrupted_finding_state_fails_closed(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "checkout"
    root.mkdir()
    layout = StateLayout.for_repository(root)
    layout.ensure()
    layout.path("coderabbit-review.json").write_text(
        json.dumps(
            {
                "schema_version": 4,
                "findings": [{"path": "azurpilot/integrations/coderabbit.py"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(coderabbit.ToolingError) as error:
        coderabbit.CodeRabbitAdapter._load_review_state(root)

    assert error.value.code is ResultCode.TOOLING_VERIFICATION_UNKNOWN


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
    assert recovered["phase"] == "idle"
    assert recovered["recovery"]["status"] == "completed"


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
    fingerprint = coderabbit.CandidateFingerprint(
        root_identity="a" * 24,
        repository_identity="hosted:github.com/example/project",
        base_sha="a" * 40,
        head_sha="b" * 40,
        status_digest="c" * 64,
    )
    review_result = _result(root, stdout=json.dumps({"type": "complete", "findings": []}) + "\n")
    adapter = _prepared_adapter(
        monkeypatch,
        root,
        results=(review_result,),
        fingerprints=(fingerprint, fingerprint),
    )

    outcome = adapter.review(root, IntegrationConfig(), base_sha="a" * 40, head_sha="b" * 40, task_id="task-1")
    state = adapter._load_review_state(root)

    assert outcome.record.reason_code == "CODERABBIT_REVIEW_COMPLETE"
    assert state["substantive_iterations"] == 1
    assert state["terminal"] is True
    assert state["active"] is False
    assert state["provider_identity"] is None


def test_incomplete_provider_finding_does_not_consume_substantive_budget(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "checkout"
    root.mkdir()
    fingerprint = coderabbit.CandidateFingerprint(
        root_identity="a" * 24,
        repository_identity="hosted:github.com/example/project",
        base_sha="a" * 40,
        head_sha="b" * 40,
        status_digest="c" * 64,
    )
    incomplete_result = _result(
        root,
        stdout=(
            json.dumps(
                {
                    "type": "finding",
                    "finding": {
                        "fileName": "azurpilot/tooling/contracts.py",
                        "severity": "minor",
                    },
                }
            )
            + "\n"
            + json.dumps({"type": "complete", "findings": 1})
            + "\n"
        ),
    )
    adapter = _prepared_adapter(
        monkeypatch,
        root,
        results=(incomplete_result,),
        fingerprints=(fingerprint, fingerprint),
    )

    outcome = adapter.review(
        root,
        IntegrationConfig(),
        base_sha="a" * 40,
        head_sha="b" * 40,
        task_id="task-incomplete",
    )
    state = adapter._load_review_state(root)

    assert outcome.record.reason_code == "CODERABBIT_FINDING_INCOMPLETE"
    assert state["substantive_iterations"] == 0
    assert state["cycle_status"] == "provider_error"
    assert state["terminal"] is False


def test_review_reserves_starting_state_before_provider_spawn(
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
    starts: list[str] = []

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
        "_candidate_preflight",
        lambda *_args, **_kwargs: (fingerprint, None, None),
    )

    def fail_start(_self, _base: str):  # type: ignore[no-untyped-def]
        starts.append("called")
        raise OSError("spawn uncertain")

    monkeypatch.setattr(coderabbit.NativeCodeRabbit, "start_review", fail_start)

    first = adapter.review(
        root,
        IntegrationConfig(),
        base_sha="a" * 40,
        head_sha="b" * 40,
        task_id="task-starting",
    )
    state = adapter._load_review_state(root)

    assert first.record.reason_code == "CODERABBIT_PROVIDER_START_FAILED_RETRYABLE"
    assert state["active"] is False
    assert state["phase"] == "failed"
    assert state["cycle_status"] == "provider_error"
    assert state["provider_state"] == "start_failed_not_spawned"
    assert state["reservation_state"] == "retryable"
    assert state["provider_identity"] is None

    second = adapter.review(
        root,
        IntegrationConfig(),
        base_sha="a" * 40,
        head_sha="b" * 40,
        task_id="task-starting",
    )

    assert second.record.reason_code == "CODERABBIT_PROVIDER_START_FAILED_RETRYABLE"
    assert starts == ["called", "called"]


@pytest.mark.parametrize(
    ("liveness", "expected_reason", "expected_state", "active", "reservation_state"),
    [
        (
            "absent",
            "CODERABBIT_PROVIDER_STATE_SAVE_FAILED_RECOVERED",
            IntegrationState.UNAVAILABLE,
            False,
            "retryable",
        ),
        (
            "alive",
            "CODERABBIT_PROVIDER_STATE_SAVE_RECOVERY_REQUIRED",
            IntegrationState.DEGRADED,
            True,
            "owned",
        ),
        (
            "unknown",
            "CODERABBIT_PROVIDER_STATE_SAVE_RECOVERY_REQUIRED",
            IntegrationState.UNKNOWN,
            True,
            "owned",
        ),
    ],
)
def test_provider_identity_save_failure_preserves_recoverable_ownership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    liveness: str,
    expected_reason: str,
    expected_state: IntegrationState,
    active: bool,
    reservation_state: str,
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
        "_candidate_preflight",
        lambda *_args, **_kwargs: (fingerprint, None, None),
    )
    result = _result(root, stdout=json.dumps({"type": "complete", "findings": []}) + "\n")
    monkeypatch.setattr(
        coderabbit.NativeCodeRabbit,
        "start_review",
        lambda _self, _base: _Running(result),
    )
    monkeypatch.setattr(
        coderabbit.ProcessController,
        "terminate",
        staticmethod(lambda _identity: False),
    )
    monkeypatch.setattr(
        coderabbit.ProcessController,
        "inspect_state",
        staticmethod(lambda _identity: liveness),
    )

    original_save = adapter._save_review_state
    save_calls = 0

    def fail_identity_save(save_root: Path, save_state: dict[str, object]) -> None:
        nonlocal save_calls
        save_calls += 1
        if save_calls == 2:
            raise OSError("identity save failed")
        original_save(save_root, save_state)

    monkeypatch.setattr(adapter, "_save_review_state", fail_identity_save)

    outcome = adapter.review(
        root,
        IntegrationConfig(),
        base_sha="a" * 40,
        head_sha="b" * 40,
        task_id="task-save-failure",
    )
    state = adapter._load_review_state(root)

    assert outcome.record.reason_code == expected_reason
    assert outcome.record.state is expected_state
    assert state["active"] is active
    assert state["reservation_state"] == reservation_state
    if active:
        assert state["provider_identity"] is not None
        assert state["phase"] == "recovery"
        assert state["recovery"]["status"] == "required"
        blocked = adapter.review(
            root,
            IntegrationConfig(),
            base_sha="a" * 40,
            head_sha="b" * 40,
            task_id="task-save-failure",
        )
        assert blocked.record.reason_code in {
            "CODERABBIT_REVIEW_STILL_ALIVE",
            "CODERABBIT_REVIEW_LIVENESS_UNKNOWN",
        }
    else:
        assert state["provider_identity"] is None
        assert state["phase"] == "failed"
        assert state["recovery"]["status"] == "completed"


def test_startup_uncertainty_has_typed_recovery_path(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "checkout"
    root.mkdir()
    adapter = coderabbit.CodeRabbitAdapter()
    state = coderabbit._default_review_state()
    state.update(
        {
            "current_cycle_id": "coderabbit-cycle-0123456789abcdef",
            "active": True,
            "reservation_state": "pre_spawn",
            "provider_state": "starting",
            "phase": "starting",
        }
    )
    adapter._save_review_state(root, state)

    outcome = adapter.recover_interrupted_review(root, IntegrationConfig())

    assert outcome.record.reason_code == "CODERABBIT_PROVIDER_START_UNKNOWN"
    assert outcome.record.state is IntegrationState.UNKNOWN
    assert adapter._load_review_state(root)["active"] is True


@pytest.mark.parametrize(
    ("termination_state", "expected_state", "expected_reason", "active"),
    [
        ("absent", IntegrationState.UNAVAILABLE, "CODERABBIT_REVIEW_TIMEOUT", False),
        (
            "alive",
            IntegrationState.DEGRADED,
            "CODERABBIT_REVIEW_TIMEOUT_RECOVERY_REQUIRED",
            True,
        ),
        (
            "unknown",
            IntegrationState.UNKNOWN,
            "CODERABBIT_REVIEW_TIMEOUT_RECOVERY_REQUIRED",
            True,
        ),
    ],
)
def test_timeout_clears_only_proven_absent_provider(
    monkeypatch,
    tmp_path: Path,
    termination_state: str,
    expected_state: IntegrationState,
    expected_reason: str,
    active: bool,
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
    result = _result(
        root,
        timed_out=True,
        termination_state=termination_state,
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
    monkeypatch.setattr(
        adapter,
        "_candidate_preflight",
        lambda *_args, **_kwargs: (fingerprint, None, None),
    )
    monkeypatch.setattr(
        coderabbit.NativeCodeRabbit,
        "start_review",
        lambda _self, _base: _Running(result),
    )

    outcome = adapter.review(
        root,
        IntegrationConfig(),
        base_sha="a" * 40,
        head_sha="b" * 40,
        task_id="task-timeout",
    )
    state = adapter._load_review_state(root)

    assert outcome.record.reason_code == expected_reason
    assert outcome.record.state is expected_state
    assert state["active"] is active
    if active:
        assert state["provider_identity"] is not None
        assert state["phase"] == "recovery"
        assert state["cycle_status"] == "recovery_required"
        assert state["recovery"]["status"] == "required"
    else:
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
    long_provider_claim = (
        "Длинный provider claim должен сохраниться в bounded IntegrationFinding без "
        "обрезания до старого лимита DTO. "
    ) * 8
    provider_findings = "\n".join(
        json.dumps(
            {
                "type": "finding",
                "finding": {
                    "path": f"azurpilot/module_{index}.py",
                    "severity": "major",
                    "comment": (
                        long_provider_claim
                        if index == 1
                        else f"Проверить finding {index}."
                    ),
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
    assert outcome.findings[0].message == long_provider_claim.strip()
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
                            "decision_reason": (
                                f"Решение основано на проверке реализации, call sites и тестов для finding {index}."
                            ),
                            "change_summary": (
                                f"Для finding {index} требуется применить remediation или зафиксировать conflict."
                            ),
                            **(
                                {
                                    "conflict_kind": "repository_contract_conflict",
                                    "authoritative_source": ".codex/context/GIT-WORKFLOW.md",
                                }
                                if index != 1
                                else {}
                            ),
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


def test_triage_respects_shared_lifecycle_lock(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "checkout"
    root.mkdir()
    class BusyLock:
        def acquire(self, _timeout: float) -> bool:
            return False

        def release(self) -> None:
            raise AssertionError("busy lock must not be released")

    class Coordinator:
        def lock(self, operation: str) -> BusyLock:
            assert operation == "coderabbit-review"
            return BusyLock()

    monkeypatch.setattr(
        coderabbit.RepositoryCoordinator,
        "for_root",
        lambda _root: Coordinator(),
    )
    manifest = tmp_path / "triage.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_sha": "a" * 40,
                "reviewed_head": "b" * 40,
                "findings": [],
            }
        ),
        encoding="utf-8",
    )

    outcome = coderabbit.CodeRabbitAdapter().triage(
        root,
        IntegrationConfig(),
        manifest_path=manifest,
    )

    assert outcome.record.reason_code == "CODERABBIT_REVIEW_IN_PROGRESS"


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


def test_rate_limit_metadata_and_budget_remain_bounded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    error = coderabbit.CodeRabbitStreamError("CODERABBIT_RATE_LIMITED", rate_limited=True, retry_source="provider")
    assert error.retry_source == "provider"
    assert coderabbit.review_iteration_allowed(0)
    assert coderabbit.review_iteration_allowed(2)
    assert not coderabbit.review_iteration_allowed(3)
    assert not coderabbit.review_iteration_allowed(0, terminal=True)

    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "checkout"
    root.mkdir()
    fingerprint = coderabbit.CandidateFingerprint(
        "a" * 24,
        "hosted:github.com/example/project",
        "a" * 40,
        "b" * 40,
        "c" * 64,
    )
    rate_limited = _result(
        root,
        stdout=json.dumps({"type": "error", "code": "429"}) + "\n",
    )
    complete = _result(
        root,
        stdout=json.dumps({"type": "complete", "findings": []}) + "\n",
    )
    adapter = _prepared_adapter(
        monkeypatch,
        root,
        results=(rate_limited, complete),
        fingerprints=(fingerprint, fingerprint, fingerprint, fingerprint),
    )

    first = adapter.review(
        root,
        IntegrationConfig(),
        base_sha="a" * 40,
        head_sha="b" * 40,
        task_id="rate-limit-cycle",
    )
    first_state = adapter._load_review_state(root)
    assert first.record.reason_code == "CODERABBIT_RATE_LIMITED"
    assert first_state["cycle_status"] == "rate_limited_retry_allowed"
    assert first_state["substantive_iterations"] == 0

    second = adapter.review(
        root,
        IntegrationConfig(),
        base_sha="a" * 40,
        head_sha="b" * 40,
        task_id="rate-limit-cycle",
    )
    second_state = adapter._load_review_state(root)
    assert second.record.reason_code == "CODERABBIT_REVIEW_COMPLETE"
    assert second_state["substantive_iterations"] == 1


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
    assert findings[0].resolution == findings[0].impact


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
