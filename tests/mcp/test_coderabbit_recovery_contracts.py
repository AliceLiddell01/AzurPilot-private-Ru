from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from azurpilot.integrations import coderabbit
from azurpilot.integrations.config import IntegrationConfig
from azurpilot.integrations.contracts import IntegrationState
from azurpilot.tooling.contracts import ResultCode
from azurpilot.tooling.errors import ToolingError
from azurpilot.tooling.filesystem import StateLayout

REPOSITORY_IDENTITY = "hosted:github.com/aliceliddell01/azurpilot-private-ru"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


def _result(
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    timed_out: bool = False,
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
) -> coderabbit._WslCommandResult:
    return coderabbit._WslCommandResult(
        returncode,
        stdout,
        stderr,
        timed_out,
        stdout_truncated,
        stderr_truncated,
    )


class _CandidateRuntime:
    coderabbit_command = "coderabbit"

    def __init__(self, branch: str) -> None:
        self.branch = branch
        self.calls: list[tuple[str, ...]] = []

    def git(self, *arguments: str, timeout: float = 30.0) -> coderabbit._WslCommandResult:
        del timeout
        self.calls.append(arguments)
        if arguments == ("branch", "--show-current"):
            return _result(stdout=self.branch)
        raise AssertionError(arguments)


def _configure_runtime_discovery(
    monkeypatch: pytest.MonkeyPatch,
    runtimes: dict[str, _CandidateRuntime | None],
    clones: tuple[str, ...],
) -> None:
    monkeypatch.setattr(coderabbit.shutil, "which", lambda _name: "wsl.exe")
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_wsl_inventory",
        staticmethod(
            lambda *_args: (
                (coderabbit.WslDistribution("ReviewLinux", "Running", 2),),
                None,
            )
        ),
    )
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_expected_repository",
        staticmethod(lambda *_args: REPOSITORY_IDENTITY),
    )
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_discover_review_clones",
        staticmethod(lambda *_args: (clones, None)),
    )

    def candidate_environment(
        _root: Path,
        _executable: str,
        _distro: coderabbit.WslDistribution,
        clone: str,
        _settings: dict[str, object],
    ) -> tuple[_CandidateRuntime | None, str | None]:
        runtime = runtimes[clone]
        return (
            (runtime, None)
            if runtime is not None
            else (None, "CODERABBIT_REVIEW_CLONE_NOT_CANONICAL")
        )

    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_candidate_environment",
        staticmethod(candidate_environment),
    )


def test_configured_runtime_selects_the_only_detached_clone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    selected = _CandidateRuntime("")
    attached = _CandidateRuntime("feature")
    clones = ("/home/reviewer/attached", "/home/reviewer/selected")
    _configure_runtime_discovery(
        monkeypatch,
        {clones[0]: attached, clones[1]: selected},
        clones,
    )

    runtime, error_code = coderabbit.CodeRabbitAdapter._configured_runtime(
        tmp_path,
        {"wsl_distribution": "ReviewLinux"},
    )

    assert runtime is selected
    assert error_code is None
    assert selected.calls == [("branch", "--show-current")]
    assert attached.calls == [("branch", "--show-current")]


@pytest.mark.parametrize(
    "clones",
    [
        ("/home/reviewer/first", "/home/reviewer/second"),
        ("/home/reviewer/second", "/home/reviewer/first"),
    ],
)
def test_configured_runtime_does_not_resolve_ambiguous_detached_clones_by_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    clones: tuple[str, str],
) -> None:
    runtimes = {clone: _CandidateRuntime("") for clone in clones}
    _configure_runtime_discovery(monkeypatch, runtimes, clones)

    runtime, error_code = coderabbit.CodeRabbitAdapter._configured_runtime(
        tmp_path,
        {"wsl_distribution": "ReviewLinux"},
    )

    assert runtime is None
    assert error_code == "CODERABBIT_REVIEW_CLONE_AMBIGUOUS"


def test_configured_runtime_returns_candidate_validation_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clone = "/home/reviewer/invalid"
    _configure_runtime_discovery(monkeypatch, {clone: None}, (clone,))

    runtime, error_code = coderabbit.CodeRabbitAdapter._configured_runtime(
        tmp_path,
        {"wsl_distribution": "ReviewLinux"},
    )

    assert runtime is None
    assert error_code == "CODERABBIT_REVIEW_CLONE_NOT_CANONICAL"


def test_configured_runtime_auto_discovery_keeps_only_one_valid_detached_clone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entries = (
        coderabbit.WslDistribution("FirstLinux", "Running", 2),
        coderabbit.WslDistribution("SecondLinux", "Running", 2),
    )
    invalid_clone = "/home/reviewer/invalid"
    selected_clone = "/home/reviewer/selected"
    selected = _CandidateRuntime("")
    monkeypatch.setattr(coderabbit.shutil, "which", lambda _name: "wsl.exe")
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_wsl_inventory",
        staticmethod(lambda *_args: (entries, None)),
    )
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_expected_repository",
        staticmethod(lambda *_args: REPOSITORY_IDENTITY),
    )

    def discover(
        _root: Path,
        _executable: str,
        distro: coderabbit.WslDistribution,
        _expected: str,
    ) -> tuple[tuple[str, ...], None]:
        return (
            (invalid_clone,) if distro.name == "FirstLinux" else (selected_clone,),
            None,
        )

    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_discover_review_clones",
        staticmethod(discover),
    )
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_candidate_environment",
        staticmethod(
            lambda _root, _executable, _distro, clone, _settings: (
                (None, "CODERABBIT_REVIEW_CLONE_NOT_CANONICAL")
                if clone == invalid_clone
                else (selected, None)
            )
        ),
    )

    runtime, error_code = coderabbit.CodeRabbitAdapter._configured_runtime(
        tmp_path, {}
    )

    assert runtime is selected
    assert error_code is None


@pytest.mark.parametrize(
    "review_clone",
    ["relative/clone", "/home/reviewer/../other", "/home/reviewer\x00clone", 42],
)
def test_configured_runtime_rejects_unsafe_review_clone_without_discovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    review_clone: object,
) -> None:
    monkeypatch.setattr(coderabbit.shutil, "which", lambda _name: "wsl.exe")
    inventory_calls: list[object] = []

    def unexpected_inventory(*_args: object) -> object:
        inventory_calls.append(True)
        raise AssertionError("WSL/Git discovery не должна выполняться")

    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_wsl_inventory",
        staticmethod(unexpected_inventory),
    )

    runtime, error_code = coderabbit.CodeRabbitAdapter._configured_runtime(
        tmp_path,
        {"review_clone": review_clone},
    )

    assert runtime is None
    assert error_code == "CODERABBIT_REVIEW_CLONE_NOT_CONFIGURED"
    assert inventory_calls == []


def test_configured_runtime_does_not_bypass_corrupt_persisted_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    metadata_path = StateLayout.for_repository(tmp_path / "checkout").path(
        "coderabbit-runtime.json"
    )
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(coderabbit.shutil, "which", lambda _name: "wsl.exe")
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_wsl_inventory",
        staticmethod(
            lambda *_args: (_ for _ in ()).throw(
                AssertionError("повреждённое canonical state нельзя обходить")
            )
        ),
    )

    runtime, error_code = coderabbit.CodeRabbitAdapter._configured_runtime(
        tmp_path / "checkout",
        {},
    )

    assert runtime is None
    assert error_code == "CODERABBIT_RUNTIME_STATE_UNAVAILABLE"


@pytest.mark.parametrize("canonical", [True, False])
def test_candidate_environment_requires_canonical_clone_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    canonical: bool,
) -> None:
    distro = coderabbit.WslDistribution("ReviewLinux", "Running", 2)
    clone = "/home/reviewer/canonical"
    runtime_result = _result(stdout=(clone if canonical else "/home/reviewer/other") + "\n")
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_wsl_identity",
        staticmethod(lambda *_args: (("reviewer", "/home/reviewer"), None)),
    )
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_expected_repository",
        staticmethod(lambda *_args: REPOSITORY_IDENTITY),
    )
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_wsl_path",
        staticmethod(lambda *_args, **_kwargs: ("/usr/bin:/bin", None)),
    )
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_verify_clone_identity",
        staticmethod(lambda *_args: (True, "CODERABBIT_REVIEW_CLONE_IDENTITY_READY")),
    )
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_linux_executable",
        staticmethod(lambda *_args: ("/home/reviewer/bin/coderabbit", None)),
    )
    monkeypatch.setattr(
        coderabbit._WslRuntime,
        "command",
        lambda _self, _command, *_args, **_kwargs: runtime_result,
    )

    runtime, error_code = coderabbit.CodeRabbitAdapter._candidate_environment(
        tmp_path,
        "wsl.exe",
        distro,
        clone,
        {},
    )

    if canonical:
        assert runtime is not None
        assert runtime.clone == clone
        assert runtime.environment.coderabbit_executable == "/home/reviewer/bin/coderabbit"
        assert error_code is None
    else:
        assert runtime is None
        assert error_code == "CODERABBIT_REVIEW_CLONE_NOT_CANONICAL"


class _ManagedRuntime:
    distro = "ReviewLinux"
    user = "reviewer"
    home = "/home/reviewer"
    clone = "/home/reviewer/canonical"

    def __init__(
        self,
        *,
        current_branch: str = "personal/stable",
        status: str = "",
        counts: str = "0 0",
        head: str = "a" * 40,
        remote_head: str = "a" * 40,
        upstream: str = "origin/personal/stable",
        local_branch_exists: bool = False,
        local_counts: str = "0 0",
        merge_returncode: int = 0,
        switch_returncode: int = 0,
        track_returncode: int = 0,
    ) -> None:
        self.current_branch = current_branch
        self.status = status
        self.counts = counts
        self.head = head
        self.remote_head = remote_head
        self.upstream = upstream
        self.local_branch_exists = local_branch_exists
        self.local_counts = local_counts
        self.merge_returncode = merge_returncode
        self.switch_returncode = switch_returncode
        self.track_returncode = track_returncode
        self.after_merge = False
        self.calls: list[tuple[str, ...]] = []

    def git(self, *arguments: str, timeout: float = 30.0) -> coderabbit._WslCommandResult:
        del timeout
        self.calls.append(arguments)
        if arguments == ("fetch", "--no-tags", "--prune", "origin"):
            return _result()
        if arguments == ("rev-parse", "--show-toplevel"):
            return _result(stdout=self.clone)
        if arguments == ("remote", "get-url", "origin"):
            return _result(
                stdout="https://github.com/AliceLiddell01/AzurPilot-private-Ru.git"
            )
        if arguments == ("status", "--porcelain=v1", "--untracked-files=all"):
            return _result(stdout=self.status)
        if arguments == ("status", "--porcelain=v1"):
            return _result(stdout=self.status)
        if arguments == ("rev-parse", "HEAD"):
            head = self.remote_head if self.after_merge else self.head
            return _result(stdout=head)
        if arguments == ("branch", "--show-current"):
            return _result(stdout=self.current_branch)
        if arguments == ("rev-parse", "refs/remotes/origin/personal/stable"):
            return _result(stdout=self.remote_head)
        if arguments == (
            "rev-list",
            "--left-right",
            "--count",
            "HEAD...refs/remotes/origin/personal/stable",
        ):
            return _result(stdout="0 0" if self.after_merge else self.counts)
        if arguments == (
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{upstream}",
        ):
            return _result(stdout=self.upstream)
        if arguments == (
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/personal/stable",
        ):
            return _result(returncode=0 if self.local_branch_exists else 1)
        if arguments == ("rev-parse", "refs/heads/personal/stable"):
            return _result(stdout=self.remote_head)
        if arguments == (
            "rev-list",
            "--left-right",
            "--count",
            "refs/heads/personal/stable...origin/personal/stable",
        ):
            return _result(stdout=self.local_counts)
        if arguments in {
            ("switch", "personal/stable"),
            (
                "switch",
                "--create",
                "personal/stable",
                "--track",
                "origin/personal/stable",
            ),
        }:
            self.current_branch = "personal/stable"
            return _result(returncode=self.switch_returncode)
        if arguments == (
            "branch",
            "--set-upstream-to",
            "origin/personal/stable",
            "personal/stable",
        ):
            if self.track_returncode == 0:
                self.upstream = "origin/personal/stable"
            return _result(returncode=self.track_returncode)
        if arguments == ("merge", "--ff-only", "origin/personal/stable"):
            if self.merge_returncode == 0:
                self.after_merge = True
            return _result(returncode=self.merge_returncode)
        raise AssertionError(arguments)


def test_managed_snapshot_metadata_controls_ownership_without_overriding_risk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    metadata = {
        "distro": "ReviewLinux",
        "linux_user": "reviewer",
        "home": "/home/reviewer",
        "managed_clone": "/home/reviewer/canonical",
        "repository_identity": REPOSITORY_IDENTITY,
        "management_branch": "personal/stable",
        "upstream_ref": "origin/personal/stable",
        "last_sync_at": "2026-09-20T00:00:00+00:00",
    }
    adapter = coderabbit.CodeRabbitAdapter()
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_load_runtime_metadata",
        staticmethod(lambda _root: metadata),
    )

    dirty = adapter._managed_clone_snapshot(
        tmp_path,
        _ManagedRuntime(status=" M tracked.py"),
        expected_repository=REPOSITORY_IDENTITY,
        remote="origin",
        branch="personal/stable",
    )
    ready = adapter._managed_clone_snapshot(
        tmp_path,
        _ManagedRuntime(),
        expected_repository=REPOSITORY_IDENTITY,
        remote="origin",
        branch="personal/stable",
    )

    assert dirty.sync_state == "DIRTY"
    assert dirty.ownership_state == "MANUAL_ATTENTION_REQUIRED"
    assert ready.sync_state == "READY"
    assert ready.ownership_state == "OWNED"

    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_load_runtime_metadata",
        staticmethod(lambda _root: {**metadata, "managed_clone": "/other/clone"}),
    )
    mismatched = adapter._managed_clone_snapshot(
        tmp_path,
        _ManagedRuntime(),
        expected_repository=REPOSITORY_IDENTITY,
        remote="origin",
        branch="personal/stable",
    )
    assert mismatched.ownership_state == "MANUAL_ATTENTION_REQUIRED"


def test_sync_managed_clone_switches_to_existing_clean_management_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_management_contract",
        staticmethod(lambda _root: ("origin", "personal/stable", "origin/personal/stable")),
    )
    runtime = _ManagedRuntime(current_branch="feature", local_branch_exists=True)

    result = coderabbit.CodeRabbitAdapter()._sync_managed_clone(
        tmp_path, runtime, expected_repository=REPOSITORY_IDENTITY
    )

    assert result.sync_state == "READY"
    assert ("switch", "personal/stable") in runtime.calls
    assert not any(
        argument in {"reset", "clean", "checkout"}
        for call in runtime.calls
        for argument in call
    )


def test_sync_managed_clone_blocks_existing_local_branch_with_extra_commits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_management_contract",
        staticmethod(lambda _root: ("origin", "personal/stable", "origin/personal/stable")),
    )
    runtime = _ManagedRuntime(
        current_branch="feature", local_branch_exists=True, local_counts="1 0"
    )

    with pytest.raises(ToolingError) as error:
        coderabbit.CodeRabbitAdapter()._sync_managed_clone(
            tmp_path, runtime, expected_repository=REPOSITORY_IDENTITY
        )

    assert error.value.code is ResultCode.TOOLING_UPDATE_LOCAL_AHEAD
    assert not any(call[0] == "switch" for call in runtime.calls)


def test_sync_managed_clone_repairs_wrong_upstream_without_force_operations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_management_contract",
        staticmethod(lambda _root: ("origin", "personal/stable", "origin/personal/stable")),
    )
    runtime = _ManagedRuntime(upstream="origin/other")

    result = coderabbit.CodeRabbitAdapter()._sync_managed_clone(
        tmp_path, runtime, expected_repository=REPOSITORY_IDENTITY
    )

    assert result.sync_state == "READY"
    assert (
        "branch",
        "--set-upstream-to",
        "origin/personal/stable",
        "personal/stable",
    ) in runtime.calls
    assert not any(
        argument in {"reset", "clean", "checkout"}
        for call in runtime.calls
        for argument in call
    )


@pytest.mark.parametrize(
    ("runtime_kwargs", "expected_code"),
    [
        ({"remote_head": ""}, ResultCode.TOOLING_REMOTE_REF_CONFLICT),
        (
            {
                "current_branch": "feature",
                "local_branch_exists": True,
                "switch_returncode": 1,
            },
            ResultCode.TOOLING_GIT_FAILED,
        ),
        ({"upstream": "origin/other", "track_returncode": 1}, ResultCode.TOOLING_GIT_FAILED),
    ],
)
def test_sync_managed_clone_reports_typed_reconcile_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runtime_kwargs: dict[str, object],
    expected_code: ResultCode,
) -> None:
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_management_contract",
        staticmethod(lambda _root: ("origin", "personal/stable", "origin/personal/stable")),
    )
    runtime = _ManagedRuntime(**runtime_kwargs)

    with pytest.raises(ToolingError) as error:
        coderabbit.CodeRabbitAdapter()._sync_managed_clone(
            tmp_path, runtime, expected_repository=REPOSITORY_IDENTITY
        )

    assert error.value.code is expected_code


def test_sync_managed_clone_reports_impossible_fast_forward(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_management_contract",
        staticmethod(lambda _root: ("origin", "personal/stable", "origin/personal/stable")),
    )
    runtime = _ManagedRuntime(
        head="a" * 40,
        remote_head="b" * 40,
        counts="0 2",
        merge_returncode=1,
    )

    with pytest.raises(ToolingError) as error:
        coderabbit.CodeRabbitAdapter()._sync_managed_clone(
            tmp_path, runtime, expected_repository=REPOSITORY_IDENTITY
        )

    assert error.value.code is ResultCode.TOOLING_UPDATE_DIVERGED
    assert ("merge", "--ff-only", "origin/personal/stable") in runtime.calls
    assert not any(
        argument in {"reset", "clean", "checkout"}
        for call in runtime.calls
        for argument in call
    )


def _ready_snapshot() -> coderabbit.ManagedCloneSnapshot:
    return coderabbit.ManagedCloneSnapshot(
        repository_identity=REPOSITORY_IDENTITY,
        origin_identity=REPOSITORY_IDENTITY,
        management_branch="personal/stable",
        upstream_ref="origin/personal/stable",
        head_sha=HEAD_SHA,
        remote_head_sha=HEAD_SHA,
        clean=True,
        ahead=0,
        behind=0,
        diverged=False,
        sync_state="READY",
        ownership_state="OWNED",
    )


@pytest.mark.parametrize(
    "final_snapshot",
    [
        replace(_ready_snapshot(), clean=False, sync_state="DIRTY"),
        replace(_ready_snapshot(), management_branch="feature", sync_state="WRONG_BRANCH"),
        replace(_ready_snapshot(), remote_head_sha=None, sync_state="REMOTE_UNAVAILABLE"),
        replace(_ready_snapshot(), head_sha="c" * 40, sync_state="SYNCABLE"),
        replace(_ready_snapshot(), upstream_ref="origin/other", sync_state="WRONG_UPSTREAM"),
        replace(_ready_snapshot(), ahead=1, sync_state="AHEAD"),
        replace(_ready_snapshot(), behind=1, sync_state="SYNCABLE"),
        replace(_ready_snapshot(), ahead=1, behind=1, diverged=True, sync_state="DIVERGED"),
    ],
)
def test_sync_managed_clone_fails_closed_on_any_bad_postcondition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    final_snapshot: coderabbit.ManagedCloneSnapshot,
) -> None:
    adapter = coderabbit.CodeRabbitAdapter()
    monkeypatch.setattr(
        adapter,
        "_management_contract",
        lambda _root: ("origin", "personal/stable", "origin/personal/stable"),
    )
    snapshots = iter((_ready_snapshot(), final_snapshot))
    monkeypatch.setattr(
        adapter,
        "_managed_clone_snapshot",
        lambda *_args, **_kwargs: next(snapshots),
    )
    monkeypatch.setattr(
        adapter,
        "_write_runtime_metadata",
        lambda *_args, **_kwargs: pytest.fail("metadata нельзя писать без postcondition"),
    )

    with pytest.raises(ToolingError) as error:
        adapter._sync_managed_clone(
            tmp_path,
            _ManagedRuntime(),
            expected_repository=REPOSITORY_IDENTITY,
        )

    assert error.value.code is ResultCode.TOOLING_VERIFICATION_UNKNOWN


class _ReviewRuntime:
    coderabbit_command = "coderabbit"

    def __init__(self, provider_result: coderabbit._WslCommandResult) -> None:
        self.provider_result = provider_result
        self.calls = 0

    def command(self, *arguments: str, **_kwargs: object) -> coderabbit._WslCommandResult:
        if "--agent" in arguments:
            self.calls += 1
            return self.provider_result
        raise AssertionError(arguments)


def _install_review_runtime(
    monkeypatch: pytest.MonkeyPatch,
    provider_result: coderabbit._WslCommandResult,
) -> _ReviewRuntime:
    runtime = _ReviewRuntime(provider_result)
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_configured_runtime",
        staticmethod(lambda _root, _settings: (runtime, None)),
    )
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_expected_repository",
        staticmethod(lambda _root, _settings: REPOSITORY_IDENTITY),
    )
    monkeypatch.setattr(
        coderabbit.CodeRabbitAdapter,
        "_runtime_preflight",
        lambda _self, _runtime, _command: (
            IntegrationState.READY,
            "CODERABBIT_RUNTIME_READY",
            (),
        ),
    )
    return runtime


def _complete_output(*, finding: bool) -> str:
    events: list[str] = []
    if finding:
        events.append(
            json.dumps(
                {
                    "type": "finding",
                    "finding": {
                        "path": "azurpilot/tooling/process_core.py",
                        "severity": "major",
                        "comment": "Проверить ownership boundary.",
                        "classification": "confirmed",
                    },
                }
            )
        )
    events.append(json.dumps({"type": "complete"}))
    return "\n".join(events) + "\n"


@pytest.mark.parametrize(
    ("provider_result", "expected_code", "expected_state", "provider_state"),
    [
        (_result(timed_out=True), "CODERABBIT_REVIEW_TIMEOUT", IntegrationState.UNAVAILABLE, "timeout"),
        (_result(stdout_truncated=True), "CODERABBIT_REVIEW_OUTPUT_TRUNCATED", IntegrationState.UNKNOWN, "stream_error"),
        (_result(stderr_truncated=True), "CODERABBIT_REVIEW_OUTPUT_TRUNCATED", IntegrationState.UNKNOWN, "stream_error"),
        (_result(returncode=1, stderr="provider crashed"), "CODERABBIT_REVIEW_FAILED", IntegrationState.UNAVAILABLE, "failed"),
        (_result(returncode=1, stderr="429"), "CODERABBIT_RATE_LIMITED", IntegrationState.RATE_LIMITED, coderabbit._RATE_LIMIT_WAITING),
        (_result(returncode=1, stderr="rate limit"), "CODERABBIT_RATE_LIMITED", IntegrationState.RATE_LIMITED, coderabbit._RATE_LIMIT_WAITING),
        (_result(returncode=1, stderr="too many requests"), "CODERABBIT_RATE_LIMITED", IntegrationState.RATE_LIMITED, coderabbit._RATE_LIMIT_WAITING),
    ],
)
def test_review_provider_failure_matrix_persists_incomplete_state_without_iteration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider_result: coderabbit._WslCommandResult,
    expected_code: str,
    expected_state: IntegrationState,
    provider_state: str,
) -> None:
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    runtime = _install_review_runtime(monkeypatch, provider_result)
    root = tmp_path / "checkout"

    outcome = coderabbit.CodeRabbitAdapter().review(
        root,
        IntegrationConfig(),
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )
    state = coderabbit.CodeRabbitAdapter()._load_review_state(root)

    assert runtime.calls == 1
    assert outcome.record.reason_code == expected_code
    assert outcome.record.state is expected_state
    assert state["provider_state"] == provider_state
    assert state["complete_received"] is False
    assert state["substantive_iterations"] == 0
    assert state["base_sha"] == BASE_SHA
    assert state["last_head"] == HEAD_SHA
    if expected_state is IntegrationState.RATE_LIMITED:
        assert state["rate_limited_at"]
        assert state["retry_not_before"] is None
        assert "too many requests" not in json.dumps(state).casefold()


def test_review_parser_error_is_incomplete_and_does_not_consume_iteration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    _install_review_runtime(monkeypatch, _result(stdout="not-json\n"))
    root = tmp_path / "checkout"

    outcome = coderabbit.CodeRabbitAdapter().review(
        root, IntegrationConfig(), base_sha=BASE_SHA, head_sha=HEAD_SHA
    )
    state = coderabbit.CodeRabbitAdapter()._load_review_state(root)

    assert outcome.record.reason_code == "CODERABBIT_NDJSON_INVALID"
    assert outcome.record.state is IntegrationState.UNKNOWN
    assert state["provider_state"] == "stream_error"
    assert state["complete_received"] is False
    assert state["substantive_iterations"] == 0


def test_review_parser_rate_limit_keeps_only_bounded_retry_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    output = json.dumps(
        {"type": "error", "message": "too many requests", "retry_after_seconds": 60}
    )
    _install_review_runtime(monkeypatch, _result(stdout=output + "\n"))
    root = tmp_path / "checkout"

    outcome = coderabbit.CodeRabbitAdapter().review(
        root, IntegrationConfig(), base_sha=BASE_SHA, head_sha=HEAD_SHA
    )
    state = coderabbit.CodeRabbitAdapter()._load_review_state(root)

    assert outcome.record.reason_code == "CODERABBIT_RATE_LIMITED"
    assert outcome.record.state is IntegrationState.RATE_LIMITED
    assert state["provider_state"] == coderabbit._RATE_LIMIT_WAITING
    assert state["rate_limited_at"]
    assert state["retry_not_before"]
    assert state["retry_source"] == "provider"
    assert "too many requests" not in json.dumps(state).casefold()


def test_review_missing_complete_result_is_persisted_as_stream_truncation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    _install_review_runtime(monkeypatch, _result(stdout="{" + '"type":"status"' + "}\n"))
    monkeypatch.setattr(
        coderabbit,
        "parse_agent_ndjson",
        lambda _lines: coderabbit.ParsedCodeRabbitReview((), complete=False),
    )
    root = tmp_path / "checkout"

    outcome = coderabbit.CodeRabbitAdapter().review(
        root, IntegrationConfig(), base_sha=BASE_SHA, head_sha=HEAD_SHA
    )
    state = coderabbit.CodeRabbitAdapter()._load_review_state(root)

    assert outcome.record.reason_code == "CODERABBIT_STREAM_TRUNCATED"
    assert outcome.record.state is IntegrationState.UNKNOWN
    assert state["provider_state"] == "stream_error"
    assert state["complete_received"] is False
    assert state["substantive_iterations"] == 0


@pytest.mark.parametrize("finding", [True, False])
def test_review_complete_persists_exact_head_findings_and_quota(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, finding: bool
) -> None:
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    _install_review_runtime(
        monkeypatch,
        _result(stdout=_complete_output(finding=finding)),
    )
    root = tmp_path / "checkout"

    outcome = coderabbit.CodeRabbitAdapter().review(
        root, IntegrationConfig(), base_sha=BASE_SHA, head_sha=HEAD_SHA
    )
    state = coderabbit.CodeRabbitAdapter()._load_review_state(root)

    assert outcome.record.reason_code == "CODERABBIT_REVIEW_COMPLETE"
    assert state["provider_state"] == "complete"
    assert state["complete_received"] is True
    assert state["substantive_iterations"] == 1
    assert state["terminal"] is (not finding)
    assert state["reviewed_head"] == HEAD_SHA
    assert state["findings_count"] == (1 if finding else 0)
    persisted_findings = tuple(
        coderabbit._parse_finding(item) for item in state["findings"]
    )
    assert state["findings_digest"] == coderabbit._normalized_findings_digest(
        persisted_findings
    )
    assert state["provider_quota"]["state"] == "available"


class _CleanupReviewRuntime(coderabbit._WslRuntime):
    def __init__(self, provider_result: coderabbit._WslCommandResult, root: Path) -> None:
        super().__init__(
            root,
            "wsl.exe",
            distro="ReviewLinux",
            user="reviewer",
            home="/home/reviewer",
            clone="/home/reviewer/canonical",
            repository_identity=REPOSITORY_IDENTITY,
            coderabbit_executable="/home/reviewer/bin/coderabbit",
            coderabbit_command="/home/reviewer/bin/coderabbit",
        )
        self.provider_result = provider_result

    def command(
        self, _command: str, *arguments: str, timeout: float = 30.0
    ) -> coderabbit._WslCommandResult:
        del timeout
        if "--agent" in arguments:
            return self.provider_result
        raise AssertionError(arguments)


@pytest.mark.parametrize(
    "provider_result",
    [
        _result(timed_out=True),
        _result(returncode=1, stderr="provider failed"),
        _result(stdout=_complete_output(finding=False)),
    ],
)
def test_review_cleanup_failure_overrides_provider_outcome_and_requires_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider_result: coderabbit._WslCommandResult,
) -> None:
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "checkout"
    runtime = _CleanupReviewRuntime(provider_result, root)
    checkout = "/home/reviewer/.cache/azurpilot/coderabbit/reviews/coderabbit-op"
    adapter = coderabbit.CodeRabbitAdapter()
    monkeypatch.setattr(
        adapter,
        "_settings",
        lambda _config: {},
    )
    monkeypatch.setattr(
        adapter,
        "_configured_runtime",
        lambda _root, _settings: (runtime, None),
    )
    monkeypatch.setattr(
        adapter,
        "_expected_repository",
        lambda _root, _settings: REPOSITORY_IDENTITY,
    )
    monkeypatch.setattr(
        adapter,
        "_sync_managed_clone",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        adapter,
        "_prepare_review_checkout",
        lambda *_args, **_kwargs: (
            runtime,
            checkout,
            "CODERABBIT_REVIEW_CHECKOUT_READY",
        ),
    )
    monkeypatch.setattr(
        adapter,
        "_runtime_preflight",
        lambda *_args: (IntegrationState.READY, "CODERABBIT_RUNTIME_READY", ()),
    )
    monkeypatch.setattr(
        adapter,
        "_cleanup_review_checkout",
        lambda *_args, **_kwargs: (
            False,
            "CODERABBIT_REVIEW_CHECKOUT_REMOVE_FAILED",
        ),
    )

    outcome = adapter.review(
        root,
        IntegrationConfig(),
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
    )
    state = adapter._load_review_state(root)

    assert outcome.record.reason_code == "CODERABBIT_REVIEW_CHECKOUT_REMOVE_FAILED"
    assert outcome.record.state is IntegrationState.INCOMPATIBLE
    assert "CODERABBIT_REVIEW_COMPLETE" not in outcome.record.reason_code
    assert state["cleanup_state"] == "recovery_required"
    assert state["review_checkout"] == checkout
    assert state["provider_liveness"] == "verified_absent"
    assert state["phase"] == "recovery_required"
    assert any(
        item == "provider_process_liveness=verified_absent"
        for item in outcome.record.evidence.diagnostics
    )


def _probe_state(
    adapter: coderabbit.CodeRabbitAdapter,
    root: Path,
    *,
    provider_state: str,
    active: bool,
    complete_received: bool,
) -> None:
    adapter._save_state(
        root,
        iterations=0,
        head=HEAD_SHA,
        terminal=False,
        base_sha=BASE_SHA,
        repository_identity=REPOSITORY_IDENTITY,
        attempt=1,
        operation_id="coderabbit-0123456789abcdef",
        started_at="2026-09-20T00:00:00+00:00",
        provider_state=provider_state,
        active=active,
        complete_received=complete_received,
        last_event_type="review_start",
    )


class _PreflightRuntime(coderabbit._WslRuntime):
    def __init__(
        self, responses: dict[tuple[str, ...], coderabbit._WslCommandResult]
    ) -> None:
        super().__init__(
            Path.cwd(),
            "wsl.exe",
            distro="ReviewLinux",
            user="reviewer",
            home="/home/reviewer",
            clone="/home/reviewer/canonical",
            repository_identity=REPOSITORY_IDENTITY,
            coderabbit_executable="/home/reviewer/bin/coderabbit",
            coderabbit_command="/home/reviewer/bin/coderabbit",
        )
        self.responses = responses

    def command(
        self, command: str, *arguments: str, timeout: float = 30.0
    ) -> coderabbit._WslCommandResult:
        del timeout
        return self.responses[(command, *arguments)]


@pytest.mark.parametrize(
    ("stage", "result", "expected_state", "expected_reason"),
    [
        ("version", _result(timed_out=True), IntegrationState.INCOMPATIBLE, "CODERABBIT_VERSION_TIMEOUT"),
        ("version", _result(returncode=1), IntegrationState.INCOMPATIBLE, "CODERABBIT_VERSION_UNAVAILABLE"),
        ("version", _result(stdout="not-a-version"), IntegrationState.INCOMPATIBLE, "CODERABBIT_VERSION_INVALID"),
        ("help", _result(timed_out=True), IntegrationState.INCOMPATIBLE, "CODERABBIT_HELP_TIMEOUT"),
        ("help", _result(stdout="review"), IntegrationState.INCOMPATIBLE, "CODERABBIT_REVIEW_SYNTAX_INCOMPATIBLE"),
        ("auth", _result(timed_out=True), IntegrationState.UNAUTHENTICATED, "CODERABBIT_AUTH_TIMEOUT"),
        ("auth", _result(returncode=1), IntegrationState.UNAUTHENTICATED, "CODERABBIT_AUTH_NOT_CONFIGURED"),
    ],
)
def test_runtime_preflight_preserves_typed_failure_stage(
    stage: str,
    result: coderabbit._WslCommandResult,
    expected_state: IntegrationState,
    expected_reason: str,
) -> None:
    version = _result(stdout="CodeRabbit CLI 1.2.3\n")
    help_result = _result(stdout="review --agent --committed --base-commit\n")
    auth = _result(stdout="authenticated")
    responses = {
        ("coderabbit", "--version"): version,
        ("coderabbit", "review", "--help"): help_result,
        ("coderabbit", "auth", "status", "--agent"): auth,
    }
    responses[
        {
            "version": ("coderabbit", "--version"),
            "help": ("coderabbit", "review", "--help"),
            "auth": ("coderabbit", "auth", "status", "--agent"),
        }[stage]
    ] = result
    runtime = _PreflightRuntime(responses)

    state, reason, diagnostics = coderabbit.CodeRabbitAdapter()._runtime_preflight(
        runtime, "coderabbit"
    )

    assert state is expected_state
    assert reason == expected_reason
    assert diagnostics


def test_runtime_preflight_returns_ready_only_after_version_help_and_auth(
) -> None:
    runtime = _PreflightRuntime(
        {
            ("coderabbit", "--version"): _result(stdout="CodeRabbit CLI 1.2.3\n"),
            ("coderabbit", "review", "--help"): _result(
                stdout="review --agent --committed --base-commit\n"
            ),
            ("coderabbit", "auth", "status", "--agent"): _result(),
        }
    )

    state, reason, diagnostics = coderabbit.CodeRabbitAdapter()._runtime_preflight(
        runtime, "coderabbit"
    )

    assert state is IntegrationState.READY
    assert reason == "CODERABBIT_RUNTIME_READY"
    assert "cli_version=1.2.3" in diagnostics
    assert "auth=verified" in diagnostics


@pytest.mark.parametrize(
    ("provider_state", "active", "complete_received", "expected_code", "expected_state"),
    [
        ("reviewing", True, False, "CODERABBIT_ACTIVE_REVIEW", IntegrationState.INCOMPATIBLE),
        ("timeout", False, False, "CODERABBIT_REVIEW_RECOVERY_REQUIRED", IntegrationState.INCOMPATIBLE),
        ("interrupted", False, False, "CODERABBIT_REVIEW_RECOVERY_REQUIRED", IntegrationState.INCOMPATIBLE),
        (coderabbit._RATE_LIMIT_WAITING, False, False, "CODERABBIT_RATE_LIMITED", IntegrationState.RATE_LIMITED),
    ],
)
def test_probe_recovery_guards_do_not_start_another_provider_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider_state: str,
    active: bool,
    complete_received: bool,
    expected_code: str,
    expected_state: IntegrationState,
) -> None:
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "checkout"
    adapter = coderabbit.CodeRabbitAdapter()
    _probe_state(
        adapter,
        root,
        provider_state=provider_state,
        active=active,
        complete_received=complete_received,
    )
    monkeypatch.setattr(
        adapter,
        "_configured_runtime",
        lambda *_args: pytest.fail("probe не должен запускать runtime preflight"),
    )

    outcome = asyncio.run(adapter.probe(root, IntegrationConfig()))

    assert outcome.record.reason_code == expected_code
    assert outcome.record.state is expected_state


class _ProbeRuntime:
    coderabbit_command = "coderabbit"


@pytest.mark.parametrize("sync_state", ["DIRTY", "SYNCABLE"])
def test_probe_stops_before_runtime_preflight_when_managed_clone_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sync_state: str,
) -> None:
    adapter = coderabbit.CodeRabbitAdapter()
    runtime = _ProbeRuntime()
    monkeypatch.setattr(
        adapter,
        "_configured_runtime",
        lambda *_args: (runtime, None),
    )
    monkeypatch.setattr(
        adapter,
        "_management_contract",
        lambda _root: ("origin", "personal/stable", "origin/personal/stable"),
    )
    monkeypatch.setattr(
        adapter,
        "_expected_repository",
        lambda _root, _settings: REPOSITORY_IDENTITY,
    )
    monkeypatch.setattr(
        adapter,
        "_managed_clone_snapshot",
        lambda *_args, **_kwargs: replace(_ready_snapshot(), sync_state=sync_state),
    )
    monkeypatch.setattr(
        adapter,
        "_runtime_preflight",
        lambda *_args: pytest.fail("runtime preflight не должен запускаться"),
    )

    outcome = asyncio.run(adapter.probe(tmp_path, IntegrationConfig()))

    assert outcome.record.reason_code == "CODERABBIT_MANAGED_CLONE_" + sync_state


def test_probe_preserves_typed_runtime_preflight_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    adapter = coderabbit.CodeRabbitAdapter()
    runtime = _ProbeRuntime()
    monkeypatch.setattr(adapter, "_configured_runtime", lambda *_args: (runtime, None))
    monkeypatch.setattr(
        adapter,
        "_management_contract",
        lambda _root: ("origin", "personal/stable", "origin/personal/stable"),
    )
    monkeypatch.setattr(
        adapter,
        "_expected_repository",
        lambda _root, _settings: REPOSITORY_IDENTITY,
    )
    monkeypatch.setattr(
        adapter,
        "_managed_clone_snapshot",
        lambda *_args, **_kwargs: _ready_snapshot(),
    )
    monkeypatch.setattr(
        adapter,
        "_runtime_preflight",
        lambda *_args: (
            IntegrationState.INCOMPATIBLE,
            "CODERABBIT_VERSION_INVALID",
            ("wsl_version=2",),
        ),
    )

    outcome = asyncio.run(adapter.probe(tmp_path, IntegrationConfig()))

    assert outcome.record.reason_code == "CODERABBIT_VERSION_INVALID"
    assert outcome.record.state is IntegrationState.INCOMPATIBLE
    assert outcome.record.evidence.diagnostics == ("wsl_version=2",)
