"""Контракты публикации coverage в Codecov."""

from __future__ import annotations

import re
import tomllib

from tests.support.paths import REPOSITORY_ROOT


def _read(path: str) -> str:
    return (REPOSITORY_ROOT / path).read_text(encoding="utf-8")


def test_ci_generates_branch_coverage_and_uploads_with_oidc() -> None:
    workflow = _read(".github/workflows/ci.yml")

    assert "id-token: write" in workflow
    assert "--cov=module" in workflow
    assert "--cov=campaign" in workflow
    assert "--cov=tools" in workflow
    assert "--cov-branch" in workflow
    assert "--dist=loadgroup" in workflow
    assert "-n 8" in workflow
    assert "--cov-report=xml:\"${artifact_dir}/coverage.xml\"" in workflow
    assert re.search(r"uses: codecov/codecov-action@[0-9a-f]{40} # v5", workflow)
    assert "use_oidc: true" in workflow
    assert "CODECOV_TOKEN" not in workflow


def test_ci_group_contains_locked_coverage_dependency() -> None:
    project = tomllib.loads(_read("pyproject.toml"))
    dependencies = project["dependency-groups"]["ci"]

    assert any(dependency.startswith("pytest-cov==") for dependency in dependencies)


def test_codecov_config_keeps_statuses_report_only() -> None:
    config = _read("codecov.yml")

    assert "branch: personal/stable" in config
    assert "require_ci_to_pass: true" in config
    assert "project: off" in config
    assert "patch: off" in config
    assert "fail_under" not in config
