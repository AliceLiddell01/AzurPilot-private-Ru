"""Контракты публикации coverage в Codecov."""

from __future__ import annotations

import shlex
import tomllib

import yaml

from tests.support.paths import REPOSITORY_ROOT


def _read(path: str) -> str:
    return (REPOSITORY_ROOT / path).read_text(encoding="utf-8")


def _yaml(path: str) -> dict[str, object]:
    value = yaml.safe_load(_read(path))
    assert isinstance(value, dict)
    return value


def _python_job(workflow: dict[str, object]) -> dict[str, object]:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict)
    for job in jobs.values():
        if isinstance(job, dict) and job.get("name") == "Python":
            return job
    raise AssertionError("Job Python не найден.")


def _step(job: dict[str, object], step_id: str) -> dict[str, object]:
    steps = job.get("steps")
    assert isinstance(steps, list)
    for step in steps:
        if isinstance(step, dict) and step.get("id") == step_id:
            return step
    raise AssertionError(f"Step {step_id} не найден.")


def _run_arguments(step: dict[str, object]) -> list[str]:
    run = step.get("run")
    assert isinstance(run, str)
    return shlex.split(run)


def _contains_text(value: object, expected: str) -> bool:
    if isinstance(value, str):
        return expected in value
    if isinstance(value, dict):
        return any(_contains_text(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_contains_text(item, expected) for item in value)
    return False


def _contains_key(value: object, expected: str) -> bool:
    if isinstance(value, dict):
        return any(
            key == expected or _contains_key(item, expected)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_key(item, expected) for item in value)
    return False


def test_ci_generates_branch_coverage_and_uploads_with_oidc() -> None:
    workflow = _yaml(".github/workflows/ci.yml")
    job = _python_job(workflow)

    permissions = job.get("permissions")
    assert isinstance(permissions, dict)
    assert permissions.get("id-token") == "write"

    pytest_step = _step(job, "python_pytest")
    pytest_arguments = _run_arguments(pytest_step)
    assert {"--cov=module", "--cov=campaign", "--cov=tools"} <= set(
        pytest_arguments
    )
    assert "--cov-branch" in pytest_arguments
    assert "--dist=loadgroup" in pytest_arguments
    assert pytest_arguments.count("-n") == 1
    assert pytest_arguments[pytest_arguments.index("-n") + 1] == "auto"
    assert "--cov-report=xml:${artifact_dir}/coverage.xml" in pytest_arguments
    assert "tests" in pytest_arguments
    assert "-W" not in pytest_arguments

    codecov_step = _step(job, "python_codecov")
    uses = codecov_step.get("uses")
    assert isinstance(uses, str)
    assert uses.startswith("codecov/codecov-action@")
    commit_sha = uses.rsplit("@", 1)[-1]
    assert len(commit_sha) == 40
    assert all(character in "0123456789abcdef" for character in commit_sha)
    codecov_with = codecov_step.get("with")
    assert isinstance(codecov_with, dict)
    assert codecov_with.get("use_oidc") is True
    assert codecov_with.get("files") == (
        "${{ runner.temp }}/azurpilot-ci-python/coverage.xml"
    )
    assert "token" not in codecov_with
    assert not _contains_text(codecov_step, "CODECOV_TOKEN")
    assert not _contains_text(job.get("env"), "CODECOV_TOKEN")

    cleanup_step = _step(job, "python_codecov_cleanup")
    assert cleanup_step.get("if") == "always()"
    cleanup_arguments = _run_arguments(cleanup_step)
    assert cleanup_arguments[-6:] == [
        "rm",
        "-f",
        "--",
        "codecov",
        "codecov.SHA256SUM",
        "codecov.SHA256SUM.sig",
    ]

    assert "PYTHONWARNINGS" not in workflow.get("env", {})
    assert "PYTHONWARNINGS" not in job.get("env", {})

def test_ci_group_contains_locked_coverage_dependency() -> None:
    project = tomllib.loads(_read("pyproject.toml"))
    dependencies = project["dependency-groups"]["ci"]

    assert any(dependency.startswith("pytest-cov==") for dependency in dependencies)


def test_codecov_config_keeps_statuses_report_only() -> None:
    config = _yaml("codecov.yml")

    codecov = config.get("codecov")
    assert isinstance(codecov, dict)
    assert codecov.get("branch") == "personal/stable"
    assert codecov.get("require_ci_to_pass") is True
    coverage = config.get("coverage")
    assert isinstance(coverage, dict)
    status = coverage.get("status")
    assert isinstance(status, dict)
    assert status.get("project") in {False, "off"}
    assert status.get("patch") in {False, "off"}
    assert not _contains_key(config, "fail_under")
    assert not _contains_key(config, "threshold")
