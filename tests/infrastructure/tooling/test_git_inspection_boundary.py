"""Чтение свидетельств Git сохраняется после переноса публикации в навык Codex."""

from __future__ import annotations

import ast
import io
import subprocess
from pathlib import Path

import pytest

from azurpilot.cli import ServiceContainer, main
from azurpilot.tooling import contracts
from azurpilot.tooling.errors import ToolingError
from azurpilot.tooling.git import GitClient
from tests.support.paths import REPOSITORY_ROOT


@pytest.mark.parametrize("command", ["delivery", "pr", "update"])
def test_public_cli_rejects_git_mutations_before_service_creation(monkeypatch, command):
    def forbidden():
        pytest.fail("Удалённая команда не должна создавать сервисы.")

    monkeypatch.setattr(ServiceContainer, "create", forbidden)
    stdout, stderr = io.StringIO(), io.StringIO()
    assert main([command, "--json"], stdout=stdout, stderr=stderr) == 2
    assert 'TOOLING_INVALID_INVOCATION' in stdout.getvalue()
    assert stderr.getvalue() == ""


def test_python_tooling_has_no_publication_framework():
    for module in ("delivery", "pull_request", "update"):
        assert not (REPOSITORY_ROOT / "azurpilot" / "tooling" / f"{module}.py").exists()
    for name in ("DeliveryManifest", "DeliveryJournal", "PrPublicationSpec", "PullRequestBody"):
        assert not hasattr(contracts, name)
    assert not {"update", "delivery", "pull_request"} & ServiceContainer.__dataclass_fields__.keys()
    tree = ast.parse((REPOSITORY_ROOT / "azurpilot/tooling/git.py").read_text(encoding="utf-8"))
    client = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "GitClient")
    public = {n.name for n in client.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}
    assert public == {
        "head", "branch", "upstream", "status_porcelain", "status_z", "staged_paths",
        "remote_url", "object_exists", "object_bytes", "changed_paths", "is_ancestor",
        "active_operation",
    }


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def test_mcp_and_semgrep_inspection_preserves_index_head_and_binary_blobs(tmp_path):
    _git(tmp_path, "init", "-b", "task")
    _git(tmp_path, "config", "user.name", "Fixture")
    _git(tmp_path, "config", "user.email", "fixture@example.invalid")
    binary = bytes(range(256))
    (tmp_path / "blob.bin").write_bytes(binary)
    _git(tmp_path, "add", "blob.bin")
    _git(tmp_path, "commit", "-m", "fixture")
    base = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "scope.py").write_text("value = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "scope.py")
    index_before = (tmp_path / ".git/index").read_bytes()
    client = GitClient(tmp_path)
    assert client.head() == base
    assert client.branch() == "task"
    assert client.staged_paths() == ("scope.py",)
    assert "scope.py" in client.status_z()
    assert client.is_ancestor(base, base)
    assert client.changed_paths(base, base) == ()
    assert client.object_bytes(f"{base}:blob.bin") == binary
    assert client.object_exists(f"{base}:blob.bin")
    assert not client.object_exists(f"{base}:missing.bin")
    assert not client.active_operation()
    assert index_before == (tmp_path / ".git/index").read_bytes()
    assert client.head() == base
    _git(tmp_path, "commit", "-m", "scope")
    assert client.changed_paths(base, client.head()) == ("scope.py",)


@pytest.mark.parametrize("method,args", [("object_bytes", ("-bad",)), ("object_exists", ("-bad",)), ("changed_paths", ("branch", "HEAD"))])
def test_inspection_rejects_unproven_revision_arguments(tmp_path, method, args):
    with pytest.raises(ToolingError):
        getattr(GitClient(tmp_path), method)(*args)
