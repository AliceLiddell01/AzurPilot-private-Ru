from __future__ import annotations

import hashlib
import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from azurpilot.cli import build_parser, main
from azurpilot.tooling.contracts import (
    CodeRabbitReview,
    DeliveryChange,
    DeliveryDetails,
    DeliveryEvidence,
    DeliveryPhase,
    GitSnapshot,
    MandatoryGate,
    MandatoryGateState,
    OperationState,
    PrPublicationSpec,
    PullRequestBody,
    ReadinessState,
    RepositoryIdentity,
    ResultCode,
    ToolingResult,
)
from azurpilot.tooling.delivery import DeliveryService, GitleaksScanner
from azurpilot.tooling.errors import ToolingError
from azurpilot.tooling.filesystem import path_identity
from azurpilot.tooling.git import (
    GitClient,
    canonical_remote_identity,
    is_ad_hoc_remote_ref,
    repository_identity_from_remote,
)
from azurpilot.tooling.pull_request import (
    GitHubProvider,
    PullRequestBodyRenderer,
    PullRequestService,
    _head_repository_identity,
    _ValidatedPr,
)
from azurpilot.tooling.repository import ResolvedRepository


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _fixture_repository(tmp_path: Path) -> tuple[Path, Path, str, str, RepositoryIdentity]:
    root = tmp_path / "repository"
    bare = tmp_path / "remote.git"
    root.mkdir()
    (root / "module").mkdir()
    (root / "deploy").mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "azurpilot"\n', encoding="utf-8"
    )
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    (root / "deploy" / "remove-me.txt").write_text("remove\n", encoding="utf-8")
    _git(root, "init", "-b", "personal/stable")
    _git(root, "config", "user.name", "AzurPilot Test")
    _git(root, "config", "user.email", "azurpilot-test@example.invalid")
    _git(root, "add", "--", "module", "deploy", "pyproject.toml", "uv.lock", "README.md")
    _git(root, "commit", "-m", "base")
    base_sha = _git(root, "rev-parse", "HEAD")
    _git(root, "init", "--bare", str(bare))
    _git(root, "remote", "add", "origin", str(bare))
    _git(root, "push", "origin", "refs/heads/personal/stable:refs/heads/personal/stable")
    _git(root, "switch", "-c", "cli/fixture-delivery")
    identity = repository_identity_from_remote(str(bare))
    return root, bare, base_sha, str(bare), identity


def test_ad_hoc_ref_detection_preserves_hyphenated_product_branches() -> None:
    assert is_ad_hoc_remote_ref("codex/base-review")
    assert is_ad_hoc_remote_ref("feature/temporary/review")
    assert is_ad_hoc_remote_ref("feature/transport/review")
    assert not is_ad_hoc_remote_ref("fix/transport-timeout")
    assert not is_ad_hoc_remote_ref("feature/temporary-cache")


def _resolved(root: Path) -> ResolvedRepository:
    from azurpilot.tooling.contracts import RepositoryRootEvidence, RootSource

    return ResolvedRepository(
        root,
        RepositoryRootEvidence(
            source=RootSource.EXPLICIT,
            candidate_count=1,
            validation_checks=("fixture",),
            root_identity=path_identity(root),
        ),
    )


class _NoopScanner:
    def __init__(self, _root: Path, _runner: object) -> None:
        return None

    def scan_staged(self) -> None:
        return None

    def scan_committed_range(self, _start_sha: str, _end_sha: str) -> None:
        return None


def _write_delivery_manifest(
    path: Path,
    identity: RepositoryIdentity,
    *,
    base_sha: str,
    branch: str,
    targets: list[dict[str, object]],
    base_branch: str = "personal/stable",
    publication_intent: str = "commit_and_push",
    base_remote_name: str = "origin",
    remote_name: str = "origin",
    remote_branch: str | None = None,
    expected_remote_sha: str | None = None,
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository": identity.model_dump(mode="json"),
                "expected_branch": branch,
                "expected_local_head": base_sha,
                "expected_base_sha": base_sha,
                "base_remote_name": base_remote_name,
                "base_branch": base_branch,
                "remote_name": remote_name,
                "remote_branch": remote_branch or branch,
                "expected_remote_sha": expected_remote_sha,
                "targets": targets,
                "commit_message": "feat(test): проверить delivery contract",
                "publication_intent": publication_intent,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_delivery_rejects_ad_hoc_remote_topology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _bare, base_sha, _remote_url, identity = _fixture_repository(tmp_path)
    before = b"base\n"
    after = b"changed\n"
    (root / "README.md").write_bytes(after)
    manifest_path = _write_delivery_manifest(
        tmp_path / "delivery.json",
        identity,
        base_sha=base_sha,
        branch="cli/fixture-delivery",
        base_branch="codex/base-coderabbit-native-windows-boundary",
        targets=[
            {
                "path": "README.md",
                "preimage": {
                    "exists": True,
                    "sha256": hashlib.sha256(before).hexdigest(),
                    "size": len(before),
                },
                "postimage": {
                    "exists": True,
                    "sha256": hashlib.sha256(after).hexdigest(),
                    "size": len(after),
                },
            }
        ],
        publication_intent="validate_only",
    )
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))

    with pytest.raises(ToolingError) as error:
        DeliveryService(
            scanner_factory=_NoopScanner,
            allow_non_hosted_remote=True,
        ).validate(manifest_path, root)

    assert error.value.code is ResultCode.TOOLING_AD_HOC_REMOTE_TOPOLOGY


def test_stacked_delivery_fails_closed_without_temporary_parent_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _bare, base_sha, remote_url, identity = _fixture_repository(tmp_path)
    _git(root, "push", "origin", f"{base_sha}:refs/heads/parent-local")
    _git(root, "switch", "-c", "parent-local")
    (root / "README.md").write_bytes(b"parent\n")
    _git(root, "add", "--", "README.md")
    _git(root, "commit", "-m", "feat(test): добавить parent change")
    parent_sha = _git(root, "rev-parse", "HEAD")
    _git(root, "switch", "-c", "cli/stacked-delivery")
    before = b"parent\n"
    after = b"child\n"
    (root / "README.md").write_bytes(after)
    manifest_path = _write_delivery_manifest(
        tmp_path / "delivery.json",
        identity,
        base_sha=parent_sha,
        branch="cli/stacked-delivery",
        base_branch="parent-local",
        targets=[
            {
                "path": "README.md",
                "preimage": {
                    "exists": True,
                    "sha256": hashlib.sha256(before).hexdigest(),
                    "size": len(before),
                },
                "postimage": {
                    "exists": True,
                    "sha256": hashlib.sha256(after).hexdigest(),
                    "size": len(after),
                },
            }
        ],
        publication_intent="validate_only",
    )
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))

    with pytest.raises(ToolingError) as error:
        DeliveryService(
            scanner_factory=_NoopScanner,
            allow_non_hosted_remote=True,
        ).validate(manifest_path, root)

    assert error.value.code is ResultCode.TOOLING_STACKED_PARENT_UNPUBLISHED
    assert _git(root, "ls-remote", "--refs", remote_url, "refs/heads/codex/base-*") == ""


def test_delivery_publishes_allowlisted_change_to_disposable_bare_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _bare, base_sha, remote_url, identity = _fixture_repository(tmp_path)
    before = b"base\n"
    after = b"published\n"
    (root / "README.md").write_bytes(after)
    manifest = {
        "schema_version": 1,
        "repository": identity.model_dump(mode="json"),
        "expected_branch": "cli/fixture-delivery",
        "expected_local_head": base_sha,
        "expected_base_sha": base_sha,
        "base_remote_name": "origin",
        "base_branch": "personal/stable",
        "remote_name": "origin",
        "remote_branch": "cli/fixture-delivery",
        "expected_remote_sha": None,
        "targets": [
            {
                "path": "README.md",
                "preimage": {
                    "exists": True,
                    "sha256": hashlib.sha256(before).hexdigest(),
                    "size": len(before),
                },
                "postimage": {
                    "exists": True,
                    "sha256": hashlib.sha256(after).hexdigest(),
                    "size": len(after),
                },
            }
        ],
        "commit_message": "feat(test): проверить delivery bare remote",
        "publication_intent": "commit_and_push",
    }
    manifest_path = tmp_path / "delivery.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))

    service = DeliveryService(
        scanner_factory=_NoopScanner,
        allow_non_hosted_remote=True,
    )
    result = service.publish(manifest_path, root)

    assert result.ok is True
    assert result.state is OperationState.READY
    assert result.details is not None
    assert result.details.phase is DeliveryPhase.DELIVERED
    assert result.details.commit_sha
    assert result.details.remote_sha == result.details.commit_sha
    assert _git(root, "status", "--porcelain") == ""
    assert (
        _git(
            root,
            "ls-remote",
            "--refs",
            remote_url,
            "refs/heads/cli/fixture-delivery",
        ).split()[0]
        == result.details.commit_sha
    )

    status = service.status(result.operation_id or "", root)
    assert status.ok is True
    assert status.details is not None
    assert status.details.phase is DeliveryPhase.DELIVERED


def test_delivery_preserves_create_modify_delete_semantics_in_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _bare, base_sha, _remote_url, identity = _fixture_repository(tmp_path)
    before_readme = b"base\n"
    after_readme = b"published\n"
    (root / "README.md").write_bytes(after_readme)
    (root / "module" / "created.txt").write_bytes(b"created\n")
    (root / "deploy" / "remove-me.txt").unlink()
    manifest_path = _write_delivery_manifest(
        tmp_path / "delivery.json",
        identity,
        base_sha=base_sha,
        branch="cli/fixture-delivery",
        targets=[
            {
                "path": "README.md",
                "preimage": {
                    "exists": True,
                    "sha256": hashlib.sha256(before_readme).hexdigest(),
                    "size": len(before_readme),
                },
                "postimage": {
                    "exists": True,
                    "sha256": hashlib.sha256(after_readme).hexdigest(),
                    "size": len(after_readme),
                },
            },
            {
                "path": "module/created.txt",
                "preimage": {"exists": False},
                "postimage": {
                    "exists": True,
                    "sha256": hashlib.sha256(b"created\n").hexdigest(),
                    "size": len(b"created\n"),
                },
            },
            {
                "path": "deploy/remove-me.txt",
                "preimage": {
                    "exists": True,
                    "sha256": hashlib.sha256(b"remove\n").hexdigest(),
                    "size": len(b"remove\n"),
                },
                "postimage": {"exists": False},
            },
        ],
    )
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))

    result = DeliveryService(
        scanner_factory=_NoopScanner,
        allow_non_hosted_remote=True,
    ).publish(manifest_path, root)

    assert result.ok
    assert result.details is not None
    assert result.details.target_count == 3
    assert sorted(
        (item.model_dump(mode="json") for item in result.details.changes),
        key=lambda item: item["path"],
    ) == [
        {"path": "README.md", "change": "M"},
        {"path": "deploy/remove-me.txt", "change": "D"},
        {"path": "module/created.txt", "change": "A"},
    ]
    assert sorted(
        _git(
            root,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            result.details.commit_sha or "",
        ).splitlines()
    ) == ["README.md", "deploy/remove-me.txt", "module/created.txt"]


def test_delivery_rejects_target_toctou_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _bare, base_sha, _remote_url, identity = _fixture_repository(tmp_path)
    after = b"published\n"
    (root / "deploy" / "remove-me.txt").write_bytes(after)
    manifest_path = _write_delivery_manifest(
        tmp_path / "delivery.json",
        identity,
        base_sha=base_sha,
        branch="cli/fixture-delivery",
        targets=[
            {
                "path": "deploy/remove-me.txt",
                "preimage": {
                    "exists": True,
                    "sha256": hashlib.sha256(b"remove\n").hexdigest(),
                },
                "postimage": {
                    "exists": True,
                    "sha256": hashlib.sha256(after).hexdigest(),
                    "size": len(after),
                },
            }
        ],
    )
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    original_stage = GitClient.stage

    def mutate_before_stage(client: GitClient, paths: tuple[str, ...]) -> None:
        (client.root / "deploy" / "remove-me.txt").write_bytes(b"tampered\n")
        original_stage(client, paths)

    monkeypatch.setattr(GitClient, "stage", mutate_before_stage)

    with pytest.raises(ToolingError) as error:
        DeliveryService(
            scanner_factory=_NoopScanner,
            allow_non_hosted_remote=True,
        ).publish(manifest_path, root)

    assert error.value.code is ResultCode.TOOLING_VERIFICATION_UNKNOWN
    assert _git(root, "rev-parse", "HEAD") == base_sha
    assert _git(root, "diff", "--cached", "--name-only") == ""


def test_delivery_deletion_staged_absence_is_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _bare, base_sha, _remote_url, identity = _fixture_repository(tmp_path)
    (root / "deploy" / "remove-me.txt").unlink()
    manifest_path = _write_delivery_manifest(
        tmp_path / "delivery.json",
        identity,
        base_sha=base_sha,
        branch="cli/fixture-delivery",
        targets=[
            {
                "path": "deploy/remove-me.txt",
                "preimage": {
                    "exists": True,
                    "sha256": hashlib.sha256(b"remove\n").hexdigest(),
                },
                "postimage": {"exists": False},
            }
        ],
    )
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))

    result = DeliveryService(
        scanner_factory=_NoopScanner,
        allow_non_hosted_remote=True,
    ).publish(manifest_path, root)

    assert result.ok
    assert result.details is not None
    assert result.details.changes == (
        DeliveryChange(path="deploy/remove-me.txt", change="D"),
    )


def test_delivery_deletion_rejects_path_still_present_in_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _bare, base_sha, _remote_url, identity = _fixture_repository(tmp_path)
    (root / "deploy" / "remove-me.txt").write_bytes(b"staged but not deleted\n")
    _git(root, "add", "--", "deploy/remove-me.txt")
    (root / "deploy" / "remove-me.txt").unlink()
    manifest_path = _write_delivery_manifest(
        tmp_path / "delivery.json",
        identity,
        base_sha=base_sha,
        branch="cli/fixture-delivery",
        targets=[
            {
                "path": "deploy/remove-me.txt",
                "preimage": {
                    "exists": True,
                    "sha256": hashlib.sha256(b"remove\n").hexdigest(),
                },
                "postimage": {"exists": False},
            }
        ],
    )
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))

    with pytest.raises(ToolingError) as error:
        DeliveryService(
            scanner_factory=_NoopScanner,
            allow_non_hosted_remote=True,
        ).publish(manifest_path, root)

    assert error.value.code is ResultCode.TOOLING_VERIFICATION_UNKNOWN
    assert _git(root, "diff", "--cached", "--name-only") == "deploy/remove-me.txt"


def test_delivery_deletion_fails_if_target_reappears_before_staged_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _bare, base_sha, _remote_url, identity = _fixture_repository(tmp_path)
    (root / "deploy" / "remove-me.txt").unlink()
    manifest_path = _write_delivery_manifest(
        tmp_path / "delivery.json",
        identity,
        base_sha=base_sha,
        branch="cli/fixture-delivery",
        targets=[
            {
                "path": "deploy/remove-me.txt",
                "preimage": {
                    "exists": True,
                    "sha256": hashlib.sha256(b"remove\n").hexdigest(),
                },
                "postimage": {"exists": False},
            }
        ],
    )
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    original_stage = GitClient.stage

    def recreate_before_stage(client: GitClient, paths: tuple[str, ...]) -> None:
        (client.root / "deploy" / "remove-me.txt").write_bytes(b"recreated\n")
        original_stage(client, paths)

    monkeypatch.setattr(GitClient, "stage", recreate_before_stage)

    with pytest.raises(ToolingError) as error:
        DeliveryService(
            scanner_factory=_NoopScanner,
            allow_non_hosted_remote=True,
        ).publish(manifest_path, root)

    assert error.value.code is ResultCode.TOOLING_PRECONDITION_FAILED
    assert _git(root, "rev-parse", "HEAD") == base_sha


@pytest.mark.parametrize(
    "failure_code",
    (ResultCode.TOOLING_TIMEOUT, ResultCode.TOOLING_VERIFICATION_UNKNOWN),
)
def test_delivery_deletion_existence_error_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_code: ResultCode,
) -> None:
    root, _bare, base_sha, _remote_url, identity = _fixture_repository(tmp_path)
    (root / "deploy" / "remove-me.txt").unlink()
    manifest_path = _write_delivery_manifest(
        tmp_path / "delivery.json",
        identity,
        base_sha=base_sha,
        branch="cli/fixture-delivery",
        targets=[
            {
                "path": "deploy/remove-me.txt",
                "preimage": {
                    "exists": True,
                    "sha256": hashlib.sha256(b"remove\n").hexdigest(),
                },
                "postimage": {"exists": False},
            }
        ],
    )
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    original_exists = GitClient.object_exists

    def fail_existence(client: GitClient, revision_path: str) -> bool:
        if revision_path.startswith(":"):
            raise ToolingError(failure_code, "Наличие index object не подтверждено.")
        return original_exists(client, revision_path)

    monkeypatch.setattr(GitClient, "object_exists", fail_existence)

    with pytest.raises(ToolingError) as error:
        DeliveryService(
            scanner_factory=_NoopScanner,
            allow_non_hosted_remote=True,
        ).publish(manifest_path, root)

    assert error.value.code is failure_code
    assert _git(root, "rev-parse", "HEAD") == base_sha


def test_delivery_rejects_unrelated_staged_path_before_mutation(tmp_path: Path) -> None:
    root, _bare, base_sha, _remote_url, identity = _fixture_repository(tmp_path)
    (root / "README.md").write_bytes(b"published\n")
    (root / "unrelated.txt").write_text("outside\n", encoding="utf-8")
    _git(root, "add", "--", "unrelated.txt")
    manifest = {
        "repository": identity.model_dump(mode="json"),
        "expected_branch": "cli/fixture-delivery",
        "expected_local_head": base_sha,
        "expected_base_sha": base_sha,
        "remote_name": "origin",
        "remote_branch": "cli/fixture-delivery",
        "targets": [
            {
                "path": "README.md",
                "preimage": {"exists": True, "sha256": hashlib.sha256(b"base\n").hexdigest()},
                "postimage": {"exists": True, "sha256": hashlib.sha256(b"published\n").hexdigest()},
            }
        ],
        "commit_message": "feat(test): scope",
        "publication_intent": "validate_only",
    }
    manifest_path = tmp_path / "delivery.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    service = DeliveryService(allow_non_hosted_remote=True)
    with pytest.raises(ToolingError) as error:
        service.validate(manifest_path, root)
    assert getattr(error.value, "code", None) is ResultCode.TOOLING_DELIVERY_SCOPE_INVALID
    assert _git(root, "diff", "--cached", "--name-only") == "unrelated.txt"


def test_remote_identity_accepts_ssh_https_equivalence_and_keeps_local_explicit() -> None:
    assert canonical_remote_identity("git@github.com:AliceLiddell01/AzurPilot-private-Ru.git") == canonical_remote_identity(
        "https://github.com/AliceLiddell01/AzurPilot-private-Ru.git"
    )
    assert canonical_remote_identity("ssh://git@github.com/AliceLiddell01/AzurPilot-private-Ru.git") == canonical_remote_identity(
        "https://github.com/AliceLiddell01/AzurPilot-private-Ru.git"
    )
    with pytest.raises(ToolingError):
        canonical_remote_identity("https://token:secret@github.com/AliceLiddell01/AzurPilot-private-Ru.git")
    local = repository_identity_from_remote(r"C:\fixture\remote.git")
    assert local.host == "local"
    assert local.owner == "fixture"


def test_delivery_accepts_named_base_remote_with_same_canonical_repository(
    tmp_path: Path,
) -> None:
    root, bare, base_sha, _remote_url, identity = _fixture_repository(tmp_path)
    _git(root, "remote", "add", "base", str(bare))
    (root / "README.md").write_bytes(b"published\n")
    manifest_path = _write_delivery_manifest(
        tmp_path / "delivery.json",
        identity,
        base_sha=base_sha,
        branch="cli/fixture-delivery",
        base_remote_name="base",
        publication_intent="validate_only",
        targets=[
            {
                "path": "README.md",
                "preimage": {
                    "exists": True,
                    "sha256": hashlib.sha256(b"base\n").hexdigest(),
                },
                "postimage": {
                    "exists": True,
                    "sha256": hashlib.sha256(b"published\n").hexdigest(),
                },
            }
        ],
    )

    result = DeliveryService(allow_non_hosted_remote=True).validate(
        manifest_path, root
    )

    assert result.ok
    assert result.evidence is not None
    assert result.evidence.base_remote is not None
    assert result.evidence.base_remote.name == "base"
    assert result.evidence.base_remote.repository == identity


def test_delivery_rejects_base_remote_from_other_repository_even_with_same_sha(
    tmp_path: Path,
) -> None:
    root, _bare, base_sha, _remote_url, identity = _fixture_repository(tmp_path)
    _git(root, "remote", "add", "upstream", "https://github.com/wess09/AzurPilot.git")
    manifest_path = _write_delivery_manifest(
        tmp_path / "delivery.json",
        identity,
        base_sha=base_sha,
        branch="cli/fixture-delivery",
        base_remote_name="upstream",
        publication_intent="validate_only",
        targets=[
            {
                "path": "README.md",
                "preimage": {
                    "exists": True,
                    "sha256": hashlib.sha256(b"base\n").hexdigest(),
                },
                "postimage": {
                    "exists": True,
                    "sha256": hashlib.sha256(b"base\n").hexdigest(),
                },
            }
        ],
    )

    with pytest.raises(ToolingError) as error:
        DeliveryService(allow_non_hosted_remote=True).validate(manifest_path, root)

    assert error.value.code is ResultCode.TOOLING_REMOTE_IDENTITY_UNVERIFIED


def test_delivery_rejects_unsafe_base_remote_name_before_git_lookup(
    tmp_path: Path,
) -> None:
    root, _bare, base_sha, _remote_url, identity = _fixture_repository(tmp_path)
    manifest_path = _write_delivery_manifest(
        tmp_path / "delivery.json",
        identity,
        base_sha=base_sha,
        branch="cli/fixture-delivery",
        base_remote_name="../origin",
        publication_intent="validate_only",
        targets=[
            {
                "path": "README.md",
                "preimage": {"exists": True, "sha256": hashlib.sha256(b"base\n").hexdigest()},
                "postimage": {"exists": True, "sha256": hashlib.sha256(b"base\n").hexdigest()},
            }
        ],
    )

    with pytest.raises(ToolingError) as error:
        DeliveryService(allow_non_hosted_remote=True).validate(manifest_path, root)

    assert error.value.code is ResultCode.TOOLING_MANIFEST_INVALID


def test_git_object_bytes_rejects_truncated_stdout(tmp_path: Path) -> None:
    class Runner:
        def __init__(self) -> None:
            self.spec = None

        def run(self, spec: object) -> SimpleNamespace:
            self.spec = spec
            return SimpleNamespace(
                returncode=0,
                timed_out=False,
                stdout_truncated=True,
                stdout_bytes=b"partial",
            )

    runner = Runner()
    client = GitClient(tmp_path, runner=runner)  # type: ignore[arg-type]

    with pytest.raises(ToolingError) as error:
        client.object_bytes("HEAD:large.bin")

    assert error.value.code is ResultCode.TOOLING_VERIFICATION_UNKNOWN
    assert runner.spec is not None
    assert runner.spec.max_output_bytes == 16 * 1024 * 1024


def test_git_object_path_rejects_option_like_value(tmp_path: Path) -> None:
    client = GitClient(tmp_path)

    with pytest.raises(ToolingError) as error:
        client.object_bytes("--output=/tmp/leak")

    assert error.value.code is ResultCode.TOOLING_INVALID_INVOCATION


@pytest.mark.parametrize(
    "query",
    ("status_z", "staged_paths", "commit_paths", "remote_ref"),
)
def test_git_machine_queries_reject_truncated_output(
    tmp_path: Path, query: str
) -> None:
    class Runner:
        def run(self, _spec: object) -> SimpleNamespace:
            return SimpleNamespace(
                returncode=0,
                timed_out=False,
                stdout="a" * 40 + " refs/heads/main\x00",
                stderr="",
                stdout_truncated=True,
                stderr_truncated=False,
                stdout_bytes=b"partial",
                stderr_bytes=b"",
            )

    client = GitClient(tmp_path, runner=Runner())  # type: ignore[arg-type]
    operation = {
        "status_z": lambda: client.status_z(),
        "staged_paths": lambda: client.staged_paths(),
        "commit_paths": lambda: client.commit_paths("a" * 40),
        "remote_ref": lambda: client.remote_ref("origin", "main"),
    }[query]

    with pytest.raises(ToolingError) as error:
        operation()
    assert error.value.code is ResultCode.TOOLING_VERIFICATION_UNKNOWN


def test_git_object_exists_distinguishes_missing_blob(tmp_path: Path) -> None:
    root, _bare, base_sha, _remote_url, _identity = _fixture_repository(tmp_path)
    client = GitClient(root)

    assert client.object_exists(f"{base_sha}:README.md") is True
    assert client.object_exists(f"{base_sha}:missing.txt") is False


def test_gitleaks_scanner_requires_and_parses_json_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Runner:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout
            self.spec = None

        def run(self, spec: object) -> SimpleNamespace:
            self.spec = spec
            return SimpleNamespace(
                returncode=0,
                stdout=self.stdout,
                stderr="",
                stdout_truncated=False,
                stderr_truncated=False,
                timed_out=False,
            )

    monkeypatch.setattr("azurpilot.tooling.delivery.which", lambda _name: "gitleaks")
    runner = Runner("[]")
    scanner = GitleaksScanner(tmp_path, runner=runner)  # type: ignore[arg-type]
    scanner.scan_committed_range("a" * 40, "b" * 40)
    assert runner.spec is not None
    assert "--report-format=json" in runner.spec.argv
    assert "--report-path=-" in runner.spec.argv

    invalid = GitleaksScanner(tmp_path, runner=Runner("not-json"))  # type: ignore[arg-type]
    with pytest.raises(ToolingError) as error:
        invalid.scan_staged()
    assert error.value.code is ResultCode.TOOLING_SECRET_SCAN_FAILED


def test_github_provider_classifies_unknown_json_field_as_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Runner:
        def run(self, _spec: object) -> SimpleNamespace:
            return SimpleNamespace(
                returncode=1,
                timed_out=False,
                stdout="",
                stderr="Unknown JSON field: baseRefOid",
            )

    monkeypatch.setattr("azurpilot.tooling.pull_request.which", lambda _name: "gh")
    provider = GitHubProvider(cwd=tmp_path, runner=Runner())  # type: ignore[arg-type]

    with pytest.raises(ToolingError) as error:
        provider._run(("pr", "view", "279", "--json", "baseRefOid"))

    assert error.value.code is ResultCode.TOOLING_PROVIDER_UNAVAILABLE
    assert "2.63.0" in str(error.value)


def test_github_provider_rejects_truncated_machine_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Runner:
        def run(self, _spec: object) -> SimpleNamespace:
            return SimpleNamespace(
                returncode=0,
                timed_out=False,
                stdout='{"number":279}',
                stderr="",
                stdout_truncated=True,
                stderr_truncated=False,
            )

    monkeypatch.setattr("azurpilot.tooling.pull_request.which", lambda _name: "gh")
    provider = GitHubProvider(cwd=tmp_path, runner=Runner())  # type: ignore[arg-type]

    with pytest.raises(ToolingError) as error:
        provider._run(("pr", "view", "279", "--json", "number"))

    assert error.value.code is ResultCode.TOOLING_PR_PUBLICATION_UNKNOWN
    assert error.value.state is OperationState.IN_FLIGHT


def _pr_test_context(tmp_path: Path) -> tuple[_ValidatedPr, Path]:
    identity = RepositoryIdentity(
        host="github.com",
        owner="AliceLiddell01",
        repository="AzurPilot-private-Ru",
    )
    spec = PrPublicationSpec(
        repository=identity,
        base_ref="personal/stable",
        base_sha="a" * 40,
        head_ref="cli/fixture-delivery",
        head_sha="b" * 40,
        remote_name="origin",
        title="Проверка PR edit read-back",
        draft=True,
        pr_number=279,
        body=PullRequestBody(
            goal="goal",
            scope="scope",
            implementation="implementation",
            checks="checks",
            ci="ci",
            security_secret_scan="security",
            migration_rollback="rollback",
            limitations="limitations",
        ),
    )
    desired_body = "desired body\n"
    body_file = tmp_path / "body.md"
    body_file.write_text(desired_body, encoding="utf-8")
    return (
        _ValidatedPr(
            spec=spec,
            repository=_resolved(tmp_path),
            git=GitClient(tmp_path),
            rendered_body=desired_body,
            body_sha256=PullRequestBodyRenderer.body_sha256(desired_body),
        ),
        body_file,
    )


def _pr_payload(
    context: _ValidatedPr,
    *,
    body: str = "old body\n",
    **updates: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "number": 279,
        "state": "OPEN",
        "isDraft": True,
        "body": body,
        "baseRefName": context.spec.base_ref,
        "baseRefOid": context.spec.base_sha,
        "headRefName": context.spec.head_ref,
        "headRefOid": context.spec.head_sha,
        "headRepository": {
            "name": context.spec.repository.repository,
        },
        "headRepositoryOwner": {
            "login": context.spec.repository.owner,
        },
        "isCrossRepository": False,
    }
    payload.update(updates)
    return payload


class _FakePrProvider:
    def __init__(
        self,
        payloads: list[dict[str, object]],
        *,
        edit_error: ToolingError | None = None,
        view_error: ToolingError | None = None,
    ) -> None:
        self.payloads = payloads
        self.edit_error = edit_error
        self.view_error = view_error
        self.edit_calls = 0
        self.view_calls = 0

    def edit(self, _spec: PrPublicationSpec, _number: int, _body_file: Path) -> None:
        self.edit_calls += 1
        if self.edit_error is not None:
            raise self.edit_error

    def view(self, _spec: PrPublicationSpec, _number: int) -> dict[str, object]:
        self.view_calls += 1
        if self.view_error is not None:
            raise self.view_error
        if not self.payloads:
            raise AssertionError("unexpected provider read-back")
        return self.payloads.pop(0)


def test_pr_publish_timeout_edit_accepts_applied_body_after_exact_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, _body_file = _pr_test_context(tmp_path)
    provider = _FakePrProvider(
        [
            _pr_payload(context),
            _pr_payload(context, body=context.rendered_body),
        ],
        edit_error=ToolingError(
            ResultCode.TOOLING_TIMEOUT,
            "edit timeout",
            state=OperationState.IN_FLIGHT,
        ),
    )
    service = PullRequestService(
        provider_factory=lambda _root, _runner: provider,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(service, "_validate", lambda _spec, _root: context)
    monkeypatch.setattr(
        "azurpilot.tooling.pull_request.load_pr_spec",
        lambda _path: context.spec,
    )

    result = service.publish(tmp_path / "spec.json", tmp_path)

    assert result.ok
    assert result.details is not None
    assert result.details.identity.number == 279
    assert provider.edit_calls == 1
    assert provider.view_calls == 2


def test_pr_edit_timeout_without_applied_body_is_unknown_without_retry(
    tmp_path: Path,
) -> None:
    context, body_file = _pr_test_context(tmp_path)
    provider = _FakePrProvider(
        [_pr_payload(context)],
        edit_error=ToolingError(
            ResultCode.TOOLING_TIMEOUT,
            "edit timeout",
            state=OperationState.IN_FLIGHT,
        ),
    )

    with pytest.raises(ToolingError) as error:
        PullRequestService()._update_body_with_readback(
            context, provider, 279, body_file  # type: ignore[arg-type]
        )

    assert error.value.code is ResultCode.TOOLING_PR_PUBLICATION_UNKNOWN
    assert error.value.state is OperationState.IN_FLIGHT
    assert provider.edit_calls == 1
    assert provider.view_calls == 1


def test_pr_edit_timeout_with_unavailable_readback_is_unknown_without_retry(
    tmp_path: Path,
) -> None:
    context, body_file = _pr_test_context(tmp_path)
    provider = _FakePrProvider(
        [],
        edit_error=ToolingError(
            ResultCode.TOOLING_TIMEOUT,
            "edit timeout",
            state=OperationState.IN_FLIGHT,
        ),
        view_error=ToolingError(
            ResultCode.TOOLING_PROVIDER_FAILED,
            "provider unavailable",
        ),
    )

    with pytest.raises(ToolingError) as error:
        PullRequestService()._update_body_with_readback(
            context, provider, 279, body_file  # type: ignore[arg-type]
        )

    assert error.value.code is ResultCode.TOOLING_PR_PUBLICATION_UNKNOWN
    assert error.value.state is OperationState.IN_FLIGHT
    assert provider.edit_calls == 1
    assert provider.view_calls == 1


@pytest.mark.parametrize(
    "updates",
    (
        {"number": 280},
        {"baseRefOid": "c" * 40},
        {"headRefOid": "c" * 40},
        {
            "headRepository": {
                "name": "AzurPilot",
            },
            "headRepositoryOwner": {"login": "wess09"},
        },
        {"isCrossRepository": True},
    ),
)
def test_pr_edit_readback_identity_mismatch_fails_closed(
    tmp_path: Path, updates: dict[str, object]
) -> None:
    context, body_file = _pr_test_context(tmp_path)
    provider = _FakePrProvider(
        [_pr_payload(context, body=context.rendered_body, **updates)],
    )

    with pytest.raises(ToolingError) as error:
        PullRequestService()._update_body_with_readback(
            context, provider, 279, body_file  # type: ignore[arg-type]
        )

    assert error.value.code is ResultCode.TOOLING_PR_IDENTITY_MISMATCH
    assert provider.edit_calls == 1
    assert provider.view_calls == 1


@pytest.mark.parametrize(
    "updates",
    (
        {"headRepository": {"nameWithOwner": "AliceLiddell01/AzurPilot-private-Ru"}},
        {"headRepositoryOwner": {}},
        {"headRepository": None},
        {"headRepositoryOwner": None},
    ),
)
def test_head_repository_identity_rejects_missing_expected_gh_fields(
    tmp_path: Path, updates: dict[str, object]
) -> None:
    context, _body_file = _pr_test_context(tmp_path)
    payload = _pr_payload(context, **updates)

    with pytest.raises(ToolingError) as error:
        _head_repository_identity(payload, context.spec)

    assert error.value.code is ResultCode.TOOLING_PR_IDENTITY_MISMATCH


def test_structured_pr_body_contains_required_sections_and_exact_review_head() -> None:
    body = PullRequestBody(
        goal=(
            "Добавить безопасную fail-closed публикацию Git-изменений и draft PR.\n\n"
            "Оператор должен видеть не только результат команды, но и доказательства того, "
            "какой repository, branch, base и head были проверены."
        ),
        scope=(
            "В область изменения входят следующие подсистемы:\n"
            "- typed delivery manifest и exact repository state;\n"
            "- allowlist staging, scoped Gitleaks и ordinary push;\n"
            "- structured PR body, provider read-back и human/JSON CLI.\n\n"
            "За пределами области остаются MCP migration, игровые runtime-сценарии и merge."
        ),
        implementation=(
            "Реализация разделена на несколько связанных границ:\n"
            "- `azurpilot.tooling.delivery` проверяет preimage/postimage, staged scope, commit parent, "
            "remote SHA и read-only recovery;\n"
            "- `azurpilot.tooling.pull_request` строит body из typed model, пишет временный body-file, "
            "вызывает `gh --repo` и сверяет identity после provider call;\n"
            "- `azurpilot.tooling.git` сохраняет бинарные Git object bytes и не принимает усечённый stdout;\n"
            "- CLI предоставляет одинаковую capability для человека и agent-oriented JSON."
        ),
        checks=(
            "Проверены не только отдельные функции, но и сквозной сценарий:\n"
            "- disposable bare remote с реальным commit/push и journal status;\n"
            "- отказ при unrelated staged path и проверка exact identity;\n"
            "- structured body, старый `gh` JSON field и truncated Git object;\n"
            "- human output и один закрытый JSON envelope через live CLI;\n"
            "- полный локальный pytest и финальные scoped secret scans."
        ),
        ci=(
            "Exact-head CI проверяет текущий commit, а не только имя ветки:\n"
            "- `Python` запускает полный pytest и project tooling checks;\n"
            "- `Windows` выполняет Windows-specific parser и regression gates;\n"
            "- `Security` выполняет security/privacy и secret checks;\n"
            "- дополнительный `macOS core tooling` подтверждает кроссплатформенный CLI contract."
        ),
        security_secret_scan=(
            "Secret scope ограничен фактическим изменением:\n"
            "- staged Gitleaks проверяет только allowlist index;\n"
            "- committed-range Gitleaks проверяет диапазон от base SHA до exact head;\n"
            "- URL с credentials, secrets, logs и случайные артефакты не добавлялись;\n"
            "- оба запуска завершились без findings."
        ),
        migration_rollback=(
            "Схема данных и Alembic head не изменяются; миграция данных не требуется.\n"
            "- До merge rollback — закрыть draft PR и удалить task branch после отдельного решения;\n"
            "- при unknown push использовать только `delivery status/recover`, без blind retry;\n"
            "- после merge откат выполняется обычным согласованным Git rollback-процессом."
        ),
        limitations=(
            "Ограничения текущего checkpoint:\n"
            "- PR остаётся Draft до финального ChatGPT review пользователя; merge не выполняется;\n"
            "- physical device, MuMu, ADB и игровой acceptance в scope не входят;\n"
            "- CodeRabbit является внешним review checkpoint через native Windows provider "
            "в canonical checkout;\n"
            "- provider требует GitHub CLI `gh >= 2.63.0` для поля `baseRefOid`."
        ),
    )
    rendered = PullRequestBodyRenderer.render(
        body,
        base_sha="a" * 40,
        head_sha="b" * 40,
    )
    assert all(heading in rendered for heading in PullRequestBodyRenderer.sections())
    assert rendered.endswith("\n")
    assert PullRequestBodyRenderer.body_sha256(rendered)


def test_structured_pr_body_rejects_thin_operator_report() -> None:
    body = PullRequestBody(
        goal="Короткая цель.",
        scope="Короткий scope.",
        implementation="Короткая реализация.",
        checks="Короткие проверки.",
        ci="CI.",
        security_secret_scan="Сканирование.",
        migration_rollback="Откат.",
        limitations="Ограничения.",
    )

    with pytest.raises(ToolingError) as error:
        PullRequestBodyRenderer.render(body, base_sha="a" * 40, head_sha="b" * 40)

    assert error.value.code is ResultCode.TOOLING_PR_BODY_INVALID


def test_readiness_state_blocks_ready_when_mandatory_gate_is_blocked() -> None:
    gate = MandatoryGate(
        name="product_live_acceptance",
        state=MandatoryGateState.BLOCKED_PRECONDITION,
        evidence="Текущее live observation Oil недоступно.",
    )
    blocked = ReadinessState(
        implementation_status="COMPLETE",
        mandatory_gates=(gate,),
        overall_outcome="BLOCKED",
    )
    assert blocked.ready_for_chatgpt_review is False
    assert blocked.merge_ready is False

    with pytest.raises(ValueError):
        ReadinessState(
            implementation_status="COMPLETE",
            mandatory_gates=(gate,),
            overall_outcome="IN_PROGRESS",
        )
    with pytest.raises(ValueError):
        ReadinessState(
            implementation_status="COMPLETE",
            mandatory_gates=(gate,),
            overall_outcome="BLOCKED",
            ready_for_chatgpt_review=True,
        )


def test_readiness_rate_limit_is_independent_from_product_gate() -> None:
    readiness = ReadinessState(
        implementation_status="COMPLETE",
        mandatory_gates=(
            MandatoryGate(
                name="product_live_acceptance",
                state=MandatoryGateState.PASS,
                evidence="Свежий current observation и postcondition подтверждены.",
            ),
        ),
        external_reviewer_status="RATE_LIMITED",
        reviewer_limitation="Provider rate limit; substantive review не завершён.",
        overall_outcome="READY",
        ready_for_chatgpt_review=True,
    )
    assert readiness.merge_ready is False
    assert readiness.overall_outcome == "READY"


def test_structured_pr_body_keeps_prior_coderabbit_head_under_rate_limit() -> None:
    body = PullRequestBody(
        goal=(
            "Цель описана достаточно подробно, чтобы оператор понимал причину и ожидаемый результат изменения delivery orchestration. "
            "Контракт должен одинаково объяснять безопасную публикацию, точный head и проверяемое состояние provider. " * 2
        ),
        scope=(
            "Область изменения содержит несколько явно перечисленных подсистем и границ ответственности:\n"
            "- Git delivery и PR body;\n"
            "- human и agent CLI adapters;\n"
            "- exact remote verification и journal recovery.\n"
            "- staged и committed-range security checks;\n" * 2
        ),
        implementation=(
            "Реализация подробно фиксирует typed contracts, allowlist staging, staged и committed-range scan, provider read-back и сохранение machine-readable evidence.\n"
            "- Все мутации выполняются только после exact precondition checks.\n"
            "- Текст PR строится из единой модели.\n"
            "- Terminal delivery states и неоднозначные remote outcomes различаются явно.\n" * 2
        ),
        checks=(
            "Проверки перечислены как воспроизводимые факты текущего checkpoint:\n"
            "- targeted pytest и live CLI acceptance;\n"
            "- Ruff, compileall и exact-head integration;\n"
            "- disposable remote и read-only status.\n"
            "- machine-readable envelope проверен отдельно от human output.\n" * 2
        ),
        ci=(
            "Hosted CI проверяет текущий exact head:\n"
            "- Python и Windows jobs;\n"
            "- Security и кроссплатформенный tooling gate;\n"
            "- постоянные job names без stage-specific baseline.\n"
            "- результат каждого обязательного context читается по exact SHA.\n" * 2
        ),
        security_secret_scan=(
            "Security scope описан отдельно:\n"
            "- allowlist staged scan;\n"
            "- committed-range Gitleaks;\n"
            "- отсутствие секретов и credentials в diff.\n"
            "- machine-readable Gitleaks report разбирается, а не заменяется одним exit code.\n" * 2
        ),
        coderabbit_review=CodeRabbitReview(
            reviewed_head="c" * 40,
            base_sha="a" * 40,
            findings=(),
            rate_limit="Повторный review текущего head временно недоступен из-за provider rate limit.",
        ),
        migration_rollback=(
            "Миграций данных нет; rollback до merge выполняется закрытием Draft PR и удалением ветки после отдельного решения.\n"
            "- Неизвестный push восстанавливается только read-only recovery.\n"
            "- После merge используется согласованный Git rollback-процесс без ручной подмены remote ref.\n" * 2
        ),
        limitations=(
            "PR остаётся Draft до финального review:\n"
            "- physical device и игровой acceptance не входят в этот scope;\n"
            "- текущий CodeRabbit head требует отдельного повторного запуска после снятия rate limit.\n"
            "- provider rate limit не трактуется как product approval или как успешный review.\n" * 2
        ),
    )

    rendered = PullRequestBodyRenderer.render(
        body,
        base_sha="a" * 40,
        head_sha="b" * 40,
    )

    assert "Последний проверенный head: `" + "c" * 40 in rendered
    assert "Текущий head: `" + "b" * 40 in rendered
    assert "rate limit" in rendered


def test_nested_cli_parser_exposes_delivery_and_pr_actions() -> None:
    parser = build_parser()
    delivery = parser.parse_args(["delivery", "status", "delivery-test", "--json"])
    pr = parser.parse_args(["pr", "verify", "42", "--spec", "spec.json", "--json"])
    assert delivery.command == "delivery"
    assert delivery.delivery_command == "status"
    assert delivery.json is True
    assert pr.command == "pr"
    assert pr.pr_command == "verify"
    assert pr.number == 42
    assert pr.spec == "spec.json"


def test_cli_machine_mode_keeps_exactly_one_json_document_for_stubbed_delivery() -> None:
    class DeliveryStub:
        def validate(self, _manifest: str, _root: object) -> ToolingResult:
            return ToolingResult(
                ok=True,
                code=ResultCode.OK,
                state=OperationState.READY,
                message="Проверка delivery завершена.",
            )

    stdout = io.StringIO()
    stderr = io.StringIO()
    services = SimpleNamespace(delivery=DeliveryStub())
    assert main(
        ["delivery", "validate", "manifest.json", "--json"],
        services=services,
        stdout=stdout,
        stderr=stderr,
    ) == 0
    assert json.loads(stdout.getvalue())["code"] == ResultCode.OK.value
    assert stdout.getvalue().count("\n") == 1
    assert stderr.getvalue() == ""


def test_cli_delivery_validate_human_mode_renders_read_only_preview() -> None:
    identity = RepositoryIdentity(
        host="github.com",
        owner="AliceLiddell01",
        repository="AzurPilot-private-Ru",
    )
    snapshot = GitSnapshot(
        repository=identity,
        root_identity="a" * 16,
        branch="cli/fixture-delivery",
        head_sha="a" * 40,
        base_sha="b" * 40,
        base_branch="personal/stable",
        remote_name="origin",
        remote_branch="cli/fixture-delivery",
        remote_sha="c" * 40,
        upstream=None,
        dirty_paths=("README.md",),
        staged_paths=(),
        active_operation=False,
    )
    result = ToolingResult(
        ok=True,
        code=ResultCode.OK,
        state=OperationState.READY,
        message="Manifest и exact repository state подтверждены.",
        details=DeliveryDetails(
            phase=DeliveryPhase.VALIDATED,
            target_paths=("README.md",),
            target_count=1,
            changes=(DeliveryChange(path="README.md", change="M"),),
        ),
        evidence=DeliveryEvidence(snapshot=snapshot),
    )

    class DeliveryStub:
        def validate(self, _manifest: str, _root: object) -> ToolingResult:
            return result

    stdout = io.StringIO()
    stderr = io.StringIO()
    services = SimpleNamespace(delivery=DeliveryStub())
    assert main(
        ["delivery", "validate", "manifest.json", "--no-color"],
        services=services,
        stdout=stdout,
        stderr=stderr,
    ) == 0
    output = stdout.getvalue()
    assert "Delivery Package" in output
    assert "README.md" in output
    assert "Изменения не применены." in output
    assert output.rstrip().endswith("Изменения не применены.")
    assert "a" * 40 not in output
    assert "\x1b[" not in output
    assert stderr.getvalue() == ""

    json_stdout = io.StringIO()
    json_stderr = io.StringIO()
    assert main(
        ["delivery", "validate", "manifest.json", "--json"],
        services=services,
        stdout=json_stdout,
        stderr=json_stderr,
    ) == 0
    machine = json.loads(json_stdout.getvalue())
    assert machine["details"]["target_count"] == 1
    assert machine["details"]["changes"] == [{"path": "README.md", "change": "M"}]
    assert machine["evidence"]["snapshot"]["head_sha"] == "a" * 40
    assert "Delivery Package" not in json_stdout.getvalue()
    assert "\x1b[" not in json_stdout.getvalue()
    assert json_stderr.getvalue() == ""
