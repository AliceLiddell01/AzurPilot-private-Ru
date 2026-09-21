from __future__ import annotations

import importlib.util
import json
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


def _pre_tool_event(command: str, *, tool_name: str = "Bash") -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"command": command},
    }


@pytest.mark.parametrize(
    "command",
    [
        "azur integrations coderabbit status",
        "uv lock --check",
        "uv run --locked --no-sync python -m pytest -q tests",
        "echo 'azur integrations coderabbit status'",
        "rg -n azur .",
        "Get-Command azur",
        "wsl.exe -- bash -lc 'printf unrelated'",
        "wsl.exe -- echo coderabbit",
        "wsl bash -lc 'echo coderabbit'",
        "git status --short",
        "git diff -- codex/base-example",
        "git show codex/base-example",
        "git branch --list codex/base-example",
        "powershell -Command \"Write-Output 'azur status'\"",
    ],
)
def test_allowed_commands_have_no_decision(guards: ModuleType, command: str) -> None:
    assert guards.process_event(_pre_tool_event(command)) is None


@pytest.mark.parametrize(
    "command",
    [
        "uv run --locked azur integrations coderabbit status",
        "uv run --locked --no-sync python -m azurpilot integrations coderabbit status",
        "python -m azurpilot integrations coderabbit status",
        "py -m azurpilot integrations coderabbit status",
        r".venv\Scripts\azur.exe integrations coderabbit status",
        r"C:\tools\azur.exe integrations coderabbit status",
        'powershell -NoProfile -Command "azur integrations coderabbit status"',
        'cmd /c "azur integrations coderabbit status"',
        "wsl.exe -d Arch -- coderabbit review --agent",
        "wsl bash -lc 'coderabbit review --agent'",
        "git branch codex/base-helper",
        "git switch -c codex/base-helper",
        "git checkout -b codex/base-helper",
        "git push origin codex/base-helper",
        "git push origin HEAD:codex/base-helper",
        "git update-ref refs/heads/codex/base-helper HEAD",
    ],
)
def test_known_workflow_bypasses_are_denied(guards: ModuleType, command: str) -> None:
    result = guards.process_event(_pre_tool_event(command))
    assert result is not None
    output = result["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert isinstance(output["permissionDecisionReason"], str)


def test_non_bash_pre_tool_use_is_ignored(guards: ModuleType) -> None:
    assert (
        guards.process_event(
            _pre_tool_event("python -m azurpilot", tool_name="apply_patch")
        )
        is None
    )


def test_malformed_and_oversized_inputs_are_silent(guards: ModuleType) -> None:
    assert (
        guards.process_event({"hook_event_name": "PreToolUse", "tool_name": "Bash"})
        is None
    )
    assert (
        guards.process_event(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": 1},
            }
        )
        is None
    )
    assert guards.classify_command("x" * (guards._MAX_COMMAND_CHARS + 1)) is None


def _state_root(guards: ModuleType, root: Path, state_home: Path) -> Path:
    return state_home / guards._repository_identity(root)[:24]


def _write_project(root: Path) -> None:
    (root / ".codex").mkdir()
    (root / ".codex" / "hooks.json").write_text("{}", encoding="utf-8")


def _stop_event(root: Path, *, stop_hook_active: object = False) -> dict[str, object]:
    return {
        "hook_event_name": "Stop",
        "cwd": str(root / "nested"),
        "stop_hook_active": stop_hook_active,
        "transcript_path": str(root / "secret-transcript.jsonl"),
    }


def test_stop_without_unfinished_state_does_not_block(
    guards: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_project(root)
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(tmp_path / "state"))
    event = _stop_event(root)
    event["mcp"] = {"source_reconciled": True, "runtime_state": "stopped"}
    assert guards.process_event(event) == {}


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 4, "cycle_status": "triage_required", "findings_count": 2},
        {"schema_version": 4, "active": True, "phase": "provider"},
        {"schema_version": 4, "provider_state": "running", "phase": "provider"},
        {"schema_version": 4, "cycle_status": "recovery_required"},
        {
            "schema_version": 4,
            "provider_state": "CODERABBIT_REVIEW_LIVENESS_UNKNOWN",
        },
        {"schema_version": 4, "triage_complete": False, "findings_count": 1},
    ],
)
def test_stop_blocks_strong_coderabbit_lifecycle_state(
    guards: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_project(root)
    state_home = tmp_path / "state"
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(state_home))
    state_directory = _state_root(guards, root, state_home)
    state_directory.mkdir(parents=True)
    (state_directory / "coderabbit-review.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    result = guards.process_event(_stop_event(root))
    assert result is not None
    assert result["decision"] == "block"


def test_stop_hook_active_prevents_second_block(
    guards: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_project(root)
    state_home = tmp_path / "state"
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(state_home))
    state_directory = _state_root(guards, root, state_home)
    state_directory.mkdir(parents=True)
    (state_directory / "coderabbit-review.json").write_text(
        json.dumps({"schema_version": 4, "cycle_status": "triage_required"}),
        encoding="utf-8",
    )
    assert guards.process_event(_stop_event(root, stop_hook_active=True)) == {}


@pytest.mark.parametrize("phase", ["push_in_flight", "unknown"])
def test_stop_blocks_unfinished_delivery_transaction(
    guards: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_project(root)
    state_home = tmp_path / "state"
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(state_home))
    state_directory = _state_root(guards, root, state_home)
    transaction = state_directory / "transactions" / "delivery-test-operation"
    transaction.mkdir(parents=True)
    (transaction / "state.json").write_text(
        json.dumps(
            {
                "operation_id": transaction.name,
                "repository_root_identity": guards._repository_identity(root),
                "phase": phase,
            }
        ),
        encoding="utf-8",
    )
    result = guards.process_event(_stop_event(root))
    assert result is not None
    assert result["decision"] == "block"


def test_terminal_delivery_and_unknown_mcp_state_do_not_block(
    guards: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_project(root)
    state_home = tmp_path / "state"
    monkeypatch.setenv("AZURPILOT_STATE_HOME", str(state_home))
    state_directory = _state_root(guards, root, state_home)
    transaction = state_directory / "transactions" / "delivery-terminal"
    transaction.mkdir(parents=True)
    (transaction / "state.json").write_text(
        json.dumps(
            {
                "operation_id": transaction.name,
                "repository_root_identity": guards._repository_identity(root),
                "phase": "push_not_delivered",
            }
        ),
        encoding="utf-8",
    )
    event = _stop_event(root)
    event["mcp"] = {"source_reconciled": True, "runtime_state": "unknown"}
    assert guards.process_event(event) == {}
