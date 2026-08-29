from __future__ import annotations

import json
from pathlib import Path

from module.dev_mcp.adapter import DevMcpAdapter, serialize_dev_result
from module.dev_runtime import (
    DevEnvironment,
    DevResult,
    DevSessionManager,
    DevStatusKind,
)


def _result(code: str = "DEV_SYNTHETIC_OK") -> DevResult:
    return DevResult(
        ok=True,
        code=code,
        message="synthetic result",
        state=DevStatusKind.NO_SESSION.value,
        details={"safe": "value"},
    )


class _FakeManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def preflight(self) -> DevResult:
        self.calls.append(("preflight", None))
        return _result("DEV_PREFLIGHT_OK")

    def doctor(self) -> DevResult:
        self.calls.append(("doctor", None))
        return _result("DEV_DOCTOR_OK")

    def list_tasks(self) -> DevResult:
        self.calls.append(("list_tasks", None))
        return _result("DEV_TASK_CATALOG_READY")

    def plan(self, *, root_tasks: list[str], excluded_tasks: list[str]) -> DevResult:
        self.calls.append(("plan", (root_tasks, excluded_tasks)))
        return _result("DEV_TASK_PLAN_READY")

    def start(self, *, root_tasks: list[str], excluded_tasks: list[str]) -> DevResult:
        self.calls.append(("start", (root_tasks, excluded_tasks)))
        return _result("DEV_SESSION_READY")

    def status(self) -> DevResult:
        self.calls.append(("status", None))
        return _result("DEV_SESSION_STOPPED")

    def stop(self, *, preserve_task_state: bool = False) -> DevResult:
        self.calls.append(("stop", preserve_task_state))
        return _result("DEV_SESSION_STOPPED")

    def cleanup(self) -> DevResult:
        self.calls.append(("cleanup", None))
        return _result("DEV_TASK_CLEANUP_COMPLETED")

    def recover(self) -> DevResult:
        self.calls.append(("recover", None))
        return _result("DEV_RECOVERY_NOT_NEEDED")


def _adapter_with_factory() -> tuple[DevMcpAdapter, _FakeManager, list[int]]:
    manager = _FakeManager()
    factory_calls: list[int] = []

    def factory() -> _FakeManager:
        factory_calls.append(1)
        return manager

    return DevMcpAdapter(factory), manager, factory_calls


def test_manager_is_lazy_and_allowed_tools_delegate_exact_arguments() -> None:
    adapter, manager, factory_calls = _adapter_with_factory()

    assert factory_calls == []
    assert adapter.call("dev_preflight")["ok"] is True
    assert adapter.call("dev_doctor", {})["ok"] is True
    assert adapter.call("dev_list_tasks", {})["ok"] is True
    assert adapter.call(
        "dev_plan_session",
        {"root_tasks": ["RootTask"], "excluded_tasks": ["ExcludedTask"]},
    )["ok"] is True
    assert adapter.call(
        "dev_start_session",
        {"root_tasks": ["RootTask"], "excluded_tasks": []},
    )["ok"] is True
    assert adapter.call("dev_status", {})["ok"] is True
    assert adapter.call("dev_stop_session", {})["ok"] is True
    assert adapter.call("dev_stop_session", {"preserve_task_state": True})["ok"] is True
    assert adapter.call("dev_cleanup", {})["ok"] is True
    assert adapter.call("dev_recover", {})["ok"] is True

    assert len(factory_calls) == 1
    assert manager.calls == [
        ("preflight", None),
        ("doctor", None),
        ("list_tasks", None),
        ("plan", (["RootTask"], ["ExcludedTask"])),
        ("start", (["RootTask"], [])),
        ("status", None),
        ("stop", False),
        ("stop", True),
        ("cleanup", None),
        ("recover", None),
    ]


def test_invalid_and_privileged_arguments_are_rejected_before_manager_creation() -> None:
    adapter, manager, factory_calls = _adapter_with_factory()

    invalid_calls = [
        ("dev_plan_session", {}),
        ("dev_plan_session", {"root_tasks": []}),
        ("dev_plan_session", {"root_tasks": "RootTask"}),
        ("dev_plan_session", {"root_tasks": ["RootTask", 1]}),
        ("dev_plan_session", {"root_tasks": ["RootTask"], "profile": "alas"}),
        ("dev_plan_session", {"root_tasks": ["RootTask"], "repository_path": "x"}),
        ("dev_plan_session", {"root_tasks": ["RootTask"], "policy_file": "x"}),
        ("dev_plan_session", {"root_tasks": ["RootTask"], "excluded_tasks": None}),
        ("dev_stop_session", {"preserve_task_state": "false"}),
        ("dev_status", {"instance": "alas"}),
    ]

    for tool_name, arguments in invalid_calls:
        result = adapter.call(tool_name, arguments)
        assert result["ok"] is False
        assert result["code"] == "DEV_MCP_INPUT_INVALID"

    assert factory_calls == []
    assert manager.calls == []


def test_unknown_tool_is_rejected_without_runtime_access() -> None:
    adapter, manager, factory_calls = _adapter_with_factory()

    result = adapter.call("run_shell", {"command": "whoami"})

    assert result == {
        "ok": False,
        "code": "DEV_MCP_UNKNOWN_TOOL",
        "message": "Запрошенный инструмент Dev MCP не существует",
        "state": "failed",
        "session_id": None,
        "details": {"tool": "run_shell"},
    }
    assert factory_calls == []
    assert manager.calls == []


def test_unexpected_manager_exception_is_sanitized() -> None:
    class ExplodingManager(_FakeManager):
        def status(self) -> DevResult:
            raise RuntimeError("secret C:\\private\\token.txt")

    adapter = DevMcpAdapter(lambda: ExplodingManager())

    result = adapter.call("dev_status", {})

    assert result == {
        "ok": False,
        "code": "DEV_MCP_INTERNAL_ERROR",
        "message": "Внутренняя ошибка Dev MCP; подробности записаны в stderr",
        "state": "failed",
        "session_id": None,
        "details": {},
    }


def test_serializer_allowlists_result_and_redacts_sensitive_details() -> None:
    result = serialize_dev_result(
        {
            "ok": True,
            "code": "DEV_SESSION_READY",
            "message": "готово C:\\private\\ap.json",
            "state": "running_owned",
            "session_id": "session-1",
            "details": {
                "profile": "ap",
                "relative_log": "config/state/dev-runtime-gui.log",
            "repository_root": "C:\\private\\repo",
            "policy_file": "C:\\private\\policy.json",
            "command_line": ["python", "gui.py"],
            "api_key": "secret-api-key",
            "apiKey": "secret-api-key",
            "x-api-key": "secret-api-key",
            "nested": {"token": "secret", "value": 3},
        },
            "unexpected": "must not cross boundary",
        }
    )

    assert result["message"] == "готово [путь скрыт]"
    assert result["details"] == {
        "profile": "ap",
        "relative_log": "config/state/dev-runtime-gui.log",
        "nested": {"value": 3},
    }
    assert "api_key" not in result["details"]
    assert "apiKey" not in result["details"]
    assert "x-api-key" not in result["details"]
    assert "unexpected" not in result


def test_serializer_redacts_credentials_from_public_text_and_bounds_collections() -> None:
    result = serialize_dev_result(
        {
            "ok": False,
            "code": "DEV_ERROR",
            "message": "failed https://user:password@example.invalid?token=secret password=secret",
            "state": "failed",
            "details": {"items": list(range(300))},
        }
    )

    assert "password=secret" not in result["message"]
    assert "password=***" in result["message"]
    assert "token=secret" not in result["message"]
    assert len(result["details"]["items"]) == 256


def test_read_only_tools_leave_profile_and_runtime_state_unchanged(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "module").mkdir()
    (root / "gui.py").write_text("# synthetic gui\n", encoding="utf-8")
    config_dir = root / "config"
    config_dir.mkdir()
    profile = {
        "Alas": {"Emulator": {}, "General": {}},
        "RootTask": {
            "Scheduler": {
                "Enable": False,
                "Command": "RootTask",
                "NextRun": "2026-08-29 00:00:00",
            }
        },
    }
    profile_path = config_dir / "ap.json"
    profile_path.write_text(json.dumps(profile) + "\n", encoding="utf-8")
    environment = DevEnvironment(
        repository_root=root,
        python_executable=root / ".venv" / "Scripts" / "python.exe",
    )
    manager = DevSessionManager(
        environment,
        storage_probe=lambda _environment: (True, "storage ready"),
        port_probe=lambda _host, _port: False,
    )
    manager._project_python_is_supported = lambda: True
    manager._profile_check = lambda: (True, "profile ready")
    manager._webui_registry_check = lambda: (True, "registry ready")
    adapter = DevMcpAdapter(lambda: manager)

    watched_paths = (profile_path, environment.state_file, environment.task_policy_file)
    before = {
        path: path.read_bytes() if path.exists() else None for path in watched_paths
    }
    results = [
        adapter.call("dev_preflight"),
        adapter.call("dev_doctor"),
        adapter.call("dev_list_tasks"),
        adapter.call("dev_plan_session", {"root_tasks": ["RootTask"]}),
        adapter.call("dev_status"),
    ]
    after = {
        path: path.read_bytes() if path.exists() else None for path in watched_paths
    }

    assert results[0]["code"] == "DEV_PREFLIGHT_OK"
    assert results[1]["code"] == "DEV_DOCTOR_OK"
    assert results[2]["code"] == "DEV_TASK_CATALOG_READY"
    assert results[3]["code"] == "DEV_TASK_PLAN_READY"
    assert results[4]["code"] == "DEV_NO_SESSION"
    assert before == after
