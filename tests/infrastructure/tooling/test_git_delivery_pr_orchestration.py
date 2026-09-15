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
    DeliveryPhase,
    OperationState,
    PullRequestBody,
    RepositoryIdentity,
    ResultCode,
    ToolingResult,
)
from azurpilot.tooling.delivery import DeliveryService
from azurpilot.tooling.errors import ToolingError
from azurpilot.tooling.filesystem import path_identity
from azurpilot.tooling.git import (
    canonical_remote_identity,
    repository_identity_from_remote,
)
from azurpilot.tooling.pull_request import PullRequestBodyRenderer
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


def test_delivery_rejects_unrelated_staged_path_before_mutation(tmp_path: Path) -> None:
    root, _bare, base_sha, _remote_url, identity = _fixture_repository(tmp_path)
    (root / "README.md").write_text("published\n", encoding="utf-8")
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
    local = repository_identity_from_remote(r"C:\fixture\remote.git")
    assert local.host == "local"
    assert local.owner == "fixture"


def test_structured_pr_body_contains_required_sections_and_exact_review_head() -> None:
    body = PullRequestBody(
        goal="Цель изменения.",
        scope="Scope изменения.",
        implementation="Реализация.",
        checks="Тесты.",
        ci="Python, Windows, Security.",
        security_secret_scan="Scoped scan.",
        migration_rollback="Rollback: no migration.",
        limitations="CodeRabbit выполняется внешним checkpoint.",
    )
    rendered = PullRequestBodyRenderer.render(
        body,
        base_sha="a" * 40,
        head_sha="b" * 40,
    )
    assert all(heading in rendered for heading in PullRequestBodyRenderer.sections())
    assert rendered.endswith("\n")
    assert PullRequestBodyRenderer.body_sha256(rendered)


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
