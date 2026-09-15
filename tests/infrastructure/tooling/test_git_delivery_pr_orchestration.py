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
    GitClient,
    canonical_remote_identity,
    repository_identity_from_remote,
)
from azurpilot.tooling.pull_request import GitHubProvider, PullRequestBodyRenderer
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
            "- CodeRabbit является внешним review checkpoint в постоянном WSL2 Arch clone;\n"
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
