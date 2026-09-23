from __future__ import annotations

import importlib.util
import json
import os
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


def _state_directory(guards: ModuleType, root: Path, state_home: Path) -> Path:
    return state_home / guards._repository_identity(root)[:24]


def _stop_event(root: Path, *, stop_hook_active: object = False) -> dict[str, object]:
    return {
        "hook_event_name": "Stop",
        "cwd": str(root),
        "stop_hook_active": stop_hook_active,
    }


def _write_delivery_state(
    guards: ModuleType,
    root: Path,
    state_home: Path,
    *,
    phase: str,
    operation_id: str = "delivery-operation",
    root_identity: str | None = None,
) -> Path:
    transaction = (
        _state_directory(guards, root, state_home)
        / "transactions"
        / operation_id
    )
    transaction.mkdir(parents=True)
    (transaction / "state.json").write_text(
        json.dumps(
            {
                "operation_id": operation_id,
                "repository_root_identity": root_identity
                or guards._repository_identity(root),
                "phase": phase,
            }
        ),
        encoding="utf-8",
    )
    return transaction


def test_stop_without_ambiguous_delivery_does_not_block(
    guards: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _state_home = _project(tmp_path, monkeypatch)

    assert guards.process_event(_stop_event(root)) == {}
    assert guards.process_event({"hook_event_name": "PreToolUse"}) is None


@pytest.mark.parametrize("phase", ["push_in_flight", "unknown"])
def test_stop_blocks_matching_ambiguous_delivery(
    guards: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    root, state_home = _project(tmp_path, monkeypatch)
    _write_delivery_state(guards, root, state_home, phase=phase)

    result = guards.process_event(_stop_event(root))

    assert result is not None
    assert result["decision"] == "block"
    assert "azur delivery status/recover" in result["reason"]


@pytest.mark.parametrize(
    ("phase", "operation_id", "root_identity"),
    [
        ("completed", "delivery-operation", None),
        ("unknown", "another-operation", None),
        ("unknown", "delivery-operation", "different-repository"),
    ],
)
def test_stop_ignores_terminal_or_foreign_delivery_state(
    guards: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    operation_id: str,
    root_identity: str | None,
) -> None:
    root, state_home = _project(tmp_path, monkeypatch)
    _write_delivery_state(
        guards,
        root,
        state_home,
        phase=phase,
        operation_id=operation_id,
        root_identity=root_identity,
    )

    assert guards.process_event(_stop_event(root)) == {}


def test_stop_hook_active_prevents_a_second_block(
    guards: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state_home = _project(tmp_path, monkeypatch)
    _write_delivery_state(guards, root, state_home, phase="unknown")

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
