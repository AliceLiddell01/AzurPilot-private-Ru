from types import SimpleNamespace

import pytest

import dev_tools.event_datamine_build as build_module
from dev_tools.event_datamine_build import verify_git_revision


def _result(stdout: str):
    return SimpleNamespace(stdout=stdout)


def test_verify_git_revision_requires_clean_source_checkout(monkeypatch, tmp_path):
    calls = []
    results = iter(
        (
            _result("abc123\n"),
            _result(" M EN/sharecfgjson/activity_template.json\n?? EN/sharecfgjson/local.json\n"),
        )
    )

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return next(results)

    monkeypatch.setattr(build_module.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="не воспроизводим"):
        verify_git_revision(tmp_path, "abc123")

    assert calls[0][0][-2:] == ["rev-parse", "HEAD"]
    assert calls[1][0][-3:] == [
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    ]
    assert all(call[1]["check"] is True for call in calls)


def test_verify_git_revision_accepts_exact_clean_checkout(monkeypatch, tmp_path):
    results = iter((_result("abc123\n"), _result("")))
    monkeypatch.setattr(
        build_module.subprocess,
        "run",
        lambda *args, **kwargs: next(results),
    )

    verify_git_revision(tmp_path, "abc123")


def test_verify_git_revision_stops_before_status_on_wrong_head(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _result("wrong\n")

    monkeypatch.setattr(build_module.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="не совпадает"):
        verify_git_revision(tmp_path, "abc123")

    assert len(calls) == 1
