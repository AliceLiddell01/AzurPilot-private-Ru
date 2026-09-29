from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture(scope="module")
def guards() -> ModuleType:
    path = Path(__file__).parents[2] / ".codex" / "hooks" / "codex_workflow_guards.py"
    spec = importlib.util.spec_from_file_location("codex_workflow_guards", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    (root / ".codex").mkdir(parents=True)
    (root / ".codex" / "hooks.json").write_text("{}", encoding="utf-8")
    state_home = tmp_path / "state"
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(state_home))
    return root, state_home


def _stop_event(root: Path, *, stop_hook_active: object = False) -> dict[str, object]:
    return {
        "hook_event_name": "Stop",
        "cwd": str(root),
        "stop_hook_active": stop_hook_active,
    }


def _write_publication_marker(
    guards: ModuleType,
    root: Path,
    *,
    workflow: str,
    operation_id: str = "push",
    root_identity: str | None = None,
) -> Path:
    marker = root / ".codex" / "local" / "git-publication.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({
        "workflow": workflow,
        "repository_root_identity": root_identity or guards._repository_identity(root),
        "operation": operation_id,
        "head_sha": "a" * 40,
    }), encoding="utf-8")
    return marker


def test_stop_without_active_publication_does_not_block(
    guards: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = _project(tmp_path, monkeypatch)

    assert guards.process_event(_stop_event(root)) == {}
    assert guards.process_event({"hook_event_name": "PreToolUse"}) is None


def test_stop_blocks_matching_active_publication(
    guards: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = _project(tmp_path, monkeypatch)
    _write_publication_marker(
        guards, root, workflow="azurpilot-git-workflow"
    )

    result = guards.process_event(_stop_event(root))

    assert result is not None
    assert result["decision"] == "block"
    assert "azurpilot-git-workflow" in result["reason"]


@pytest.mark.parametrize(
    ("workflow", "operation_id", "root_identity"),
    [
        ("unknown-workflow", "push", None),
        ("azurpilot-git-workflow", "unknown-operation", None),
        ("azurpilot-git-workflow", "push", "different-repository"),
    ],
)
def test_stop_ignores_nonmatching_or_foreign_publication_marker(
    guards: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workflow: str,
    operation_id: str,
    root_identity: str | None,
) -> None:
    root, _ = _project(tmp_path, monkeypatch)
    _write_publication_marker(
        guards,
        root,
        workflow=workflow,
        operation_id=operation_id,
        root_identity=root_identity,
    )

    assert guards.process_event(_stop_event(root)) == {}


def test_stop_hook_active_prevents_a_second_block(
    guards: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = _project(tmp_path, monkeypatch)
    _write_publication_marker(
        guards, root, workflow="azurpilot-git-workflow"
    )

    assert guards.process_event(_stop_event(root, stop_hook_active=True)) == {}


def test_main_works_without_project_imports(tmp_path: Path) -> None:
    hook_path = Path(__file__).parents[2] / ".codex" / "hooks" / "codex_workflow_guards.py"
    event = {"hook_event_name": "Stop", "cwd": str(tmp_path)}
    environment = os.environ.copy()
    environment["AZURPILOT_STATE_HOME"] = str(tmp_path / "state")
    completed = subprocess.run(
        [sys.executable, str(hook_path)],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env=environment,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {}


def test_main_returns_json_for_invalid_event() -> None:
    hook_path = Path(__file__).parents[2] / ".codex" / "hooks" / "codex_workflow_guards.py"
    completed = subprocess.run(
        [sys.executable, str(hook_path)],
        input="not-json",
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {}


@pytest.mark.skipif(os.name != "nt", reason="Windows-хук запускается через batch-файл")
@pytest.mark.parametrize("working_directory", ["root", "subdirectory"])
def test_windows_hook_launcher_runs_through_cmd(tmp_path: Path, working_directory: str) -> None:
    source_root = Path(__file__).parents[2]
    root = tmp_path / "repository with spaces"
    hooks_directory = root / ".codex" / "hooks"
    hooks_directory.mkdir(parents=True)
    shutil.copy2(source_root / ".codex" / "hooks.json", root / ".codex" / "hooks.json")
    shutil.copy2(
        source_root / ".codex" / "hooks" / "codex_workflow_guards.bat",
        hooks_directory / "codex_workflow_guards.bat",
    )
    shutil.copy2(
        source_root / ".codex" / "hooks" / "codex_workflow_guards.py",
        hooks_directory / "codex_workflow_guards.py",
    )
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    config = json.loads((root / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    command = config["hooks"]["Stop"][0]["hooks"][0]["commandWindows"]
    assert "codex_workflow_guards.bat" in command

    cwd = root if working_directory == "root" else root / "tests" / "hooks"
    cwd.mkdir(parents=True, exist_ok=True)
    event = {"hook_event_name": "Stop", "cwd": str(cwd)}
    completed = subprocess.run(
        f'cmd.exe /d /c "{command}"',
        input=json.dumps(event),
        text=True,
        capture_output=True,
        cwd=cwd,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {}


@pytest.mark.parametrize("operation", ["push", "pr-create", "pr-edit", "pr-ready", "pr-draft", "pr-merge"])
def test_marker_blocks_each_native_publication_operation(guards, tmp_path, monkeypatch, operation):
    root, _ = _project(tmp_path, monkeypatch)
    _write_publication_marker(
        guards,
        root,
        workflow="azurpilot-git-workflow",
        operation_id=operation,
    )
    assert guards.process_event(_stop_event(root))["decision"] == "block"
    assert guards.process_event(_stop_event(root / ".codex" / "local"))["decision"] == "block"


@pytest.mark.parametrize("content", ["{}", "[]", "broken", "x" * 4097])
def test_invalid_or_oversized_marker_is_not_reliable_evidence(guards, tmp_path, monkeypatch, content):
    root, _ = _project(tmp_path, monkeypatch)
    marker = _write_publication_marker(
        guards, root, workflow="azurpilot-git-workflow"
    )
    marker.write_text(content, encoding="utf-8")
    assert guards.process_event(_stop_event(root)) == {}


def test_marker_does_not_disclose_fields_or_accept_unknown_schema(guards, tmp_path, monkeypatch):
    root, _ = _project(tmp_path, monkeypatch)
    marker = _write_publication_marker(
        guards, root, workflow="azurpilot-git-workflow"
    )
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["secret"] = "private-value"
    marker.write_text(json.dumps(payload), encoding="utf-8")
    assert guards.process_event(_stop_event(root)) == {}
    del payload["secret"]
    payload["head_sha"] = "invalid"
    marker.write_text(json.dumps(payload), encoding="utf-8")
    assert guards.process_event(_stop_event(root)) == {}


def test_symlink_marker_is_not_followed(guards, tmp_path, monkeypatch):
    root, _ = _project(tmp_path, monkeypatch)
    marker = _write_publication_marker(
        guards, root, workflow="azurpilot-git-workflow"
    )
    actual = marker.with_name("actual.json")
    marker.rename(actual)
    try:
        marker.symlink_to(actual)
    except OSError:
        pytest.skip("Создание symlink недоступно в этой среде.")
    assert guards.process_event(_stop_event(root)) == {}


def test_ordinary_dirty_ahead_and_draft_artifacts_do_not_block(guards, tmp_path, monkeypatch):
    root, _ = _project(tmp_path, monkeypatch)
    (root / "modified.py").write_text("value = 1", encoding="utf-8")
    local = root / ".codex" / "local"
    local.mkdir()
    (local / "body.md").write_text("Черновой PR", encoding="utf-8")
    assert guards.process_event(_stop_event(root)) == {}
