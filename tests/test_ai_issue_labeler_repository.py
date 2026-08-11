import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/ai_issue_labeler.py"


def _load_labeler_module():
    spec = importlib.util.spec_from_file_location("ai_issue_labeler_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_repo_parts_accepts_exact_owner_repo(monkeypatch):
    module = _load_labeler_module()
    monkeypatch.setenv("GITHUB_REPOSITORY", "AliceLiddell01/AzurPilot-private-Ru")

    assert module.repo_parts({}, "github") == (
        "AliceLiddell01",
        "AzurPilot-private-Ru",
    )


@pytest.mark.parametrize(
    "repository",
    ["", "owner", "owner/", "/repo", "owner/repo/extra"],
)
def test_repo_parts_rejects_malformed_repository(monkeypatch, repository):
    module = _load_labeler_module()
    monkeypatch.setenv("GITHUB_REPOSITORY", repository)

    with pytest.raises(RuntimeError, match="Missing or invalid GITHUB_REPOSITORY"):
        module.repo_parts({}, "github")
