from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from module.dev_mcp.adapter import DevMcpAdapter, DevMcpResponse, serialize_dev_result
from module.dev_runtime import (
    DevEnvironment,
    DevResult,
    DevSessionManager,
    DevStatusKind,
    DevTarget,
    DevTargetRegistry,
    EvidenceScreenshot,
    EvidenceStore,
    ProcessBackend,
    ProcessIdentity,
)
from module.dev_runtime.smoke import SmokeSpec
from module.dev_runtime.task_sandbox import TaskSandboxError
from tests.dev_mcp_contract_helpers import EXPECTED_CONTRACT


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

    def get_evidence(self, *, session_id: str | None = None) -> DevResult:
        self.calls.append(("get_evidence", session_id))
        return _result("DEV_EVIDENCE_READY")

    def get_timeline(
        self,
        *,
        session_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> DevResult:
        self.calls.append(("get_timeline", (session_id, after_sequence, limit)))
        return _result("DEV_TIMELINE_READY")

    def get_logs(
        self,
        *,
        session_id: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> DevResult:
        self.calls.append(("get_logs", (session_id, cursor, limit)))
        return _result("DEV_LOGS_READY")

    def get_screenshot(self):
        self.calls.append(("get_screenshot", None))
        return EvidenceScreenshot(_result("DEV_SCREENSHOT_READY"))

    def get_historical_screenshot(self, *, session_id: str, screenshot_id: str):
        self.calls.append(("get_historical_screenshot", (session_id, screenshot_id)))
        return EvidenceScreenshot(_result("DEV_SCREENSHOT_READY"))

    def list_smoke_capabilities(self) -> DevResult:
        self.calls.append(("list_smoke_capabilities", None))
        return _result("DEV_SMOKE_CAPABILITIES_READY")

    def validate_smoke(self, spec: object) -> DevResult:
        assert isinstance(spec, SmokeSpec)
        self.calls.append(("validate_smoke", spec.name))
        return _result("DEV_SMOKE_VALID")

    def start_smoke(self, spec: object) -> DevResult:
        assert isinstance(spec, SmokeSpec)
        self.calls.append(("start_smoke", spec.name))
        return _result("DEV_SMOKE_STARTED")

    def get_smoke(self, smoke_id: str) -> DevResult:
        self.calls.append(("get_smoke", smoke_id))
        return _result("DEV_SMOKE_RESULT_READY")

    def cancel_smoke(self, smoke_id: str) -> DevResult:
        self.calls.append(("cancel_smoke", smoke_id))
        return _result("DEV_SMOKE_CANCELLED")

    def get_smoke_evaluation(self, smoke_id: str):
        self.calls.append(("get_smoke_evaluation", smoke_id))
        return EvidenceScreenshot(_result("DEV_SMOKE_EVALUATION_READY"), b"image", "image/png")

    def submit_smoke_evaluation(self, smoke_id: str, assertion_id: str, verdict: str, rationale: str) -> DevResult:
        self.calls.append(("submit_smoke_evaluation", (smoke_id, assertion_id, verdict, rationale)))
        return _result("DEV_SMOKE_PASS")

    def get_runtime_status(self) -> DevResult:
        self.calls.append(("get_runtime_status", None))
        return _result("DEV_RUNTIME_STATUS_READY")

    def start_game(self) -> DevResult:
        self.calls.append(("start_game", None))
        return _result("DEV_CONTROL_ACCEPTED")

    def stop_game(self) -> DevResult:
        self.calls.append(("stop_game", None))
        return _result("DEV_CONTROL_ACCEPTED")

    def restart_game(self) -> DevResult:
        self.calls.append(("restart_game", None))
        return _result("DEV_CONTROL_ACCEPTED")

    def start_emulator(self) -> DevResult:
        self.calls.append(("start_emulator", None))
        return _result("DEV_CONTROL_ACCEPTED")

    def stop_emulator(self) -> DevResult:
        self.calls.append(("stop_emulator", None))
        return _result("DEV_CONTROL_ACCEPTED")

    def restart_emulator(self) -> DevResult:
        self.calls.append(("restart_emulator", None))
        return _result("DEV_CONTROL_ACCEPTED")

    def restart_adb(self) -> DevResult:
        self.calls.append(("restart_adb", None))
        return _result("DEV_CONTROL_ACCEPTED")

    def get_control_operation(self, control_id: str) -> DevResult:
        self.calls.append(("get_control_operation", control_id))
        return _result("DEV_CONTROL_OPERATION_READY")


class _TargetAwareFakeManager(_FakeManager):
    def __init__(self, environment: DevEnvironment) -> None:
        super().__init__()
        self.environment = environment


class _TaskSandboxErrorManager(_FakeManager):
    def status(self) -> DevResult:
        raise TaskSandboxError("DEV_TASK_STATE_CORRUPT", "synthetic task state error")


def _adapter_with_factory() -> tuple[DevMcpAdapter, _FakeManager, list[int]]:
    manager = _FakeManager()
    factory_calls: list[int] = []

    def factory() -> _FakeManager:
        factory_calls.append(1)
        return manager

    return DevMcpAdapter(factory), manager, factory_calls


def test_contract_is_static_safe_and_does_not_construct_runtime_manager() -> None:
    adapter, manager, factory_calls = _adapter_with_factory()

    result = adapter.call("dev_get_contract", {})

    assert result["ok"] is True
    assert result["code"] == "DEV_MCP_CONTRACT_READY"
    assert result["details"]["contract"] == EXPECTED_CONTRACT
    assert factory_calls == []
    assert manager.calls == []


def test_adapter_serializes_task_sandbox_error_from_manager() -> None:
    result = DevMcpAdapter(lambda: _TaskSandboxErrorManager()).call("dev_status", {})

    assert result["ok"] is False
    assert result["code"] == "DEV_TASK_STATE_CORRUPT"
    assert result["details"]["error"]["code"] == "DEV_TASK_STATE_CORRUPT"


def test_adapter_serializes_mixed_game_and_smoke_capabilities_by_item_schema() -> None:
    result = serialize_dev_result(
        DevResult(
            ok=True,
            code="DEV_CAPABILITIES_READY",
            message="Список capabilities готов",
            state="no_session",
            details={
                "capabilities": [
                    {
                        "capability_id": "resources",
                        "kind": "game_observation",
                        "description": "Ресурсы",
                        "source": "tests.synthetic",
                        "parameters": [],
                    },
                    {
                        "capability_id": "task_state",
                        "kind": "smoke",
                        "config_schema": {"fields": []},
                        "evidence_source": "tests.synthetic",
                        "deterministic": True,
                        "external": False,
                        "available": True,
                        "description": "Состояние задачи",
                    },
                ]
            },
        )
    )

    capabilities = result["details"]["capabilities"]
    assert capabilities[0] == {
        "capability_id": "resources",
        "kind": "game_observation",
        "description": "Ресурсы",
        "source": "tests.synthetic",
        "parameters": [],
    }
    assert capabilities[1] == {
        "capability_id": "task_state",
        "kind": "smoke",
        "config_schema": {"fields": []},
        "evidence_source": "tests.synthetic",
        "deterministic": True,
        "external": False,
        "available": True,
        "description": "Состояние задачи",
    }


def test_adapter_rebinds_manager_when_registry_target_changes(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    config = root / "config"
    config.mkdir(parents=True)
    for profile_name in ("adapter-a", "adapter-b"):
        (config / f"{profile_name}.json").write_text(
            json.dumps(
                {
                    "Alas": {"Emulator": {}},
                    "General": {},
                    "SyntheticTask": {"Scheduler": {}},
                }
            ),
            encoding="utf-8",
        )
    DevTargetRegistry.configure(
        root,
        profile_name="adapter-a",
        explicit_consent=True,
    )
    python = root / ".venv" / "Scripts" / "python.exe"
    environment_a = DevEnvironment(root, python, DevTarget("adapter-a"))
    environment_b = DevEnvironment(root, python, DevTarget("adapter-b"))
    manager_a = _TargetAwareFakeManager(environment_a)
    manager_b = _TargetAwareFakeManager(environment_b)
    managers = iter((manager_a, manager_b))
    adapter = DevMcpAdapter(lambda: next(managers))

    first = adapter.call("dev_status", {})
    same_target = adapter.call("dev_status", {})
    DevTargetRegistry.configure(
        root,
        profile_name="adapter-b",
        explicit_consent=True,
    )
    second = adapter.call("dev_status", {})

    assert first["code"] == "DEV_SESSION_STOPPED"
    assert same_target["code"] == "DEV_SESSION_STOPPED"
    assert second["code"] == "DEV_SESSION_STOPPED"
    assert manager_a.calls == [("status", None), ("status", None)]
    assert manager_b.calls == [("status", None)]


class _SyntheticProcessBackend:
    def __init__(self) -> None:
        self.alive = False
        self.identity: ProcessIdentity | None = None

    def launch(self, environment: DevEnvironment, session_id: str) -> int:
        self.alive = True
        self.identity = ProcessIdentity(
            pid=42001,
            created_at=1001.0,
            executable=str(environment.python_executable),
            command_line=tuple(ProcessBackend.expected_command(environment, session_id)),
            cwd=str(environment.repository_root),
        )
        return self.identity.pid

    def capture(self, pid: int) -> ProcessIdentity | None:
        if self.alive and self.identity is not None and self.identity.pid == pid:
            return self.identity
        return None

    def matches(self, identity: ProcessIdentity) -> bool | None:
        if not self.alive:
            return None
        return identity == self.identity

    def find_by_session(
        self, _environment: DevEnvironment, _session_id: str
    ) -> tuple[ProcessIdentity, ...]:
        return ()

    def is_descendant(self, _child_pid: int, _parent: ProcessIdentity) -> bool:
        return self.alive

    def listens_on(self, _pid: int, _host: str, _port: int) -> bool:
        return self.alive

    def request_stop(self, identity: ProcessIdentity) -> bool:
        if self.matches(identity) is not True:
            return False
        self.alive = False
        return True

    def wait_exit(self, _identity: ProcessIdentity, _timeout: float) -> bool:
        return not self.alive

    def force_stop(self, identity: ProcessIdentity) -> bool:
        return self.request_stop(identity)


def _real_runtime_manager(
    tmp_path: Path,
    *,
    screenshot_provider: Callable[[str], object] | None = None,
) -> tuple[DevSessionManager, _SyntheticProcessBackend]:
    root = tmp_path.resolve()
    (root / "module").mkdir()
    (root / "gui.py").write_text("# тестовый gui\n", encoding="utf-8")
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
    (config_dir / "ap.json").write_text(
        json.dumps(profile, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    environment = DevEnvironment(
        repository_root=root,
        python_executable=root / ".venv" / "Scripts" / "python.exe",
        dev_target=DevTarget("ap"),
    )
    backend = _SyntheticProcessBackend()
    manager = DevSessionManager(
        environment,
        process_backend=backend,
        shared_webui=False,
        storage_probe=lambda _environment: (True, "storage ready"),
        port_probe=lambda _host, _port: False,
        readiness_probe=lambda _environment, _identity: (True, "ready"),
        now=lambda: datetime(2026, 8, 29, tzinfo=UTC),
        session_id_factory=lambda: "sandbox-session",
        screenshot_provider=screenshot_provider,
        ready_timeout=0.01,
        stop_timeout=0.01,
    )
    manager._project_python_is_supported = lambda: True
    manager._profile_check = lambda: (True, "profile ready")
    manager._webui_registry_check = lambda: (True, "registry ready")
    return manager, backend


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
    assert adapter.call("dev_get_evidence", {})["ok"] is True
    assert adapter.call(
        "dev_get_timeline",
        {"session_id": "session-1", "after_sequence": 2, "limit": 3},
    )["ok"] is True
    assert adapter.call(
        "dev_get_logs",
        {"session_id": "session-1", "cursor": "cursor", "limit": 4},
    )["ok"] is True
    assert adapter.call("dev_get_screenshot", {})["ok"] is True
    smoke_spec = {
        "name": "adapter-smoke",
        "objective": "Проверить MCP adapter",
        "session": {"root_tasks": ["RootTask"]},
        "assertions": [
            {
                "assertion_id": "ready",
                "capability_id": "event_occurred",
                "event_type": "session_ready",
            }
        ],
    }
    assert adapter.call("dev_list_smoke_capabilities", {})["ok"] is True
    assert adapter.call("dev_validate_smoke", smoke_spec)["ok"] is True
    assert adapter.call("dev_start_smoke", smoke_spec)["ok"] is True
    assert adapter.call("dev_get_smoke", {"smoke_id": "smoke-1"})["ok"] is True
    assert adapter.call("dev_cancel_smoke", {"smoke_id": "smoke-1"})["ok"] is True
    evaluation = adapter.call("dev_get_smoke_evaluation", {"smoke_id": "smoke-1"})
    assert isinstance(evaluation, DevMcpResponse)
    assert evaluation.image == b"image"
    assert adapter.call(
        "dev_submit_smoke_evaluation",
        {
            "smoke_id": "smoke-1",
            "assertion_id": "visual",
            "verdict": "pass",
            "rationale": "Проверено",
        },
    )["ok"] is True
    assert adapter.call("dev_get_runtime_status", {})["ok"] is True
    for tool_name in (
        "dev_start_game",
        "dev_stop_game",
        "dev_restart_game",
        "dev_start_emulator",
        "dev_stop_emulator",
        "dev_restart_emulator",
        "dev_restart_adb",
    ):
        assert adapter.call(tool_name, {})["ok"] is True
    assert adapter.call("dev_get_control_operation", {"control_id": "a" * 32})["ok"] is True

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
        ("get_evidence", None),
        ("get_timeline", ("session-1", 2, 3)),
        ("get_logs", ("session-1", "cursor", 4)),
        ("get_screenshot", None),
        ("list_smoke_capabilities", None),
        ("validate_smoke", "adapter-smoke"),
        ("start_smoke", "adapter-smoke"),
        ("get_smoke", "smoke-1"),
        ("cancel_smoke", "smoke-1"),
        ("get_smoke_evaluation", "smoke-1"),
        ("submit_smoke_evaluation", ("smoke-1", "visual", "pass", "Проверено")),
        ("get_runtime_status", None),
        ("start_game", None),
        ("stop_game", None),
        ("restart_game", None),
        ("start_emulator", None),
        ("stop_emulator", None),
        ("restart_emulator", None),
        ("restart_adb", None),
        ("get_control_operation", "a" * 32),
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
        ("dev_get_evidence", {"session_id": "../foreign"}),
        ("dev_get_timeline", {"after_sequence": -1}),
        ("dev_get_timeline", {"limit": 201}),
        ("dev_get_logs", {"cursor": ""}),
        ("dev_get_logs", {"path": "C:\\private\\logs"}),
        ("dev_validate_smoke", {"name": "bad", "objective": "bad", "profile": "ap"}),
        (
            "dev_validate_smoke",
            {
                "name": "bad",
                "objective": "bad",
                "session": {"root_tasks": ["RootTask"]},
                "assertions": [
                    {
                        "assertion_id": "x",
                        "capability_id": "event_occurred",
                        "event_type": "session_ready",
                        "path": "x",
                    }
                ],
            },
        ),
        ("dev_get_smoke", {"smoke_id": "../foreign"}),
        (
            "dev_submit_smoke_evaluation",
            {"smoke_id": "smoke-1", "assertion_id": "visual", "verdict": "pass"},
        ),
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
        },
            "unexpected": "must not cross boundary",
        }
    )

    assert result["message"] == "готово [путь скрыт]"
    assert result["details"] == {"relative_log": "config/state/dev-runtime-gui.log"}
    assert "api_key" not in result["details"]
    assert "apiKey" not in result["details"]
    assert "x-api-key" not in result["details"]
    assert "unexpected" not in result


def test_serializer_preserves_smoke_result_and_active_conflict_state() -> None:
    result = serialize_dev_result(
        {
            "ok": False,
            "code": "DEV_SMOKE_ACTIVE_CONFLICT",
            "message": "Новый SmokeRun отклонён",
            "state": "preparing",
            "details": {
                "conflict_state": "running",
                "result": {
                    "schema_version": 1,
                    "smoke_id": "smoke-1",
                    "outcome": "PASS",
                },
            },
        }
    )

    assert result["details"]["conflict_state"] == "running"
    assert result["details"]["result"] == {
        "schema_version": 1,
        "smoke_id": "smoke-1",
        "outcome": "PASS",
    }


def test_serializer_preserves_event_capability_schema() -> None:
    result = serialize_dev_result(
        {
            "ok": True,
            "code": "DEV_SMOKE_CAPABILITIES_READY",
            "message": "готово",
            "state": "no_session",
            "details": {
                "capabilities": [
                    {
                        "capability_id": "event_occurred",
                        "kind": "event",
                        "evidence_source": "timeline",
                        "description": "Событие присутствует в timeline",
                    }
                ]
            },
        }
    )

    assert result["details"]["capabilities"] == [
        {
            "capability_id": "event_occurred",
            "kind": "event",
            "evidence_source": "timeline",
            "description": "Событие присутствует в timeline",
        }
    ]


def test_serializer_preserves_control_target_binding_without_internal_fields() -> None:
    result = serialize_dev_result(
        {
            "ok": True,
            "code": "DEV_CONTROL_ACCEPTED",
            "message": "принято",
            "state": "created",
            "details": {
                "control_operation": {
                    "active": True,
                    "operation": {
                        "control_id": "a" * 32,
                        "action": "start_game",
                        "target_profile_name": "ap",
                        "target_identity": "b" * 64,
                        "runtime_config_fingerprint": "c" * 64,
                        "state": "created",
                        "outcome": None,
                        "created_at": "2026-08-31T00:00:00+00:00",
                        "transitions": [],
                        "supervisor_pid": 1234,
                    },
                }
            },
        }
    )

    operation = result["details"]["control_operation"]["operation"]
    assert "target_profile_name" not in operation
    assert operation["target_identity"] == "b" * 64
    assert operation["runtime_config_fingerprint"] == "c" * 64
    assert "supervisor_pid" not in operation


def test_serializer_preserves_canonical_smoke_evaluation_source_only() -> None:
    result = serialize_dev_result(
        {
            "ok": True,
            "code": "DEV_SMOKE_PASS",
            "message": "готово",
            "state": "finished",
            "details": {
                "external_verdict": {
                    "source": "mcp_client",
                    "external_agent": "Codex",
                    "assertion_id": "visual",
                    "screenshot_id": "shot-1",
                    "screenshot_sha256": "a" * 64,
                    "spec_hash": "b" * 64,
                    "rubric_hash": "c" * 64,
                    "verdict": "pass",
                    "rationale": "Проверено",
                    "submitted_at": "2026-08-30T00:00:00+00:00",
                }
            },
        }
    )

    assert result["details"]["external_verdict"] == {
        "source": "mcp_client",
        "assertion_id": "visual",
        "screenshot_id": "shot-1",
        "screenshot_sha256": "a" * 64,
        "spec_hash": "b" * 64,
        "rubric_hash": "c" * 64,
        "verdict": "pass",
        "rationale": "Проверено",
        "submitted_at": "2026-08-30T00:00:00+00:00",
    }


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


def test_serializer_keeps_depth_and_json_safety_bounds() -> None:
    nested: dict[str, object] = {"value": "leaf"}
    for _ in range(10):
        nested = {"details": nested}

    result = serialize_dev_result(
        {
            "ok": True,
            "code": "DEV_OK",
            "message": "ready",
            "state": "no_session",
            "details": {
                "details": nested,
                "items": [math.nan, math.inf, object()],
            },
        }
    )

    assert result["details"]["items"] == [None, None, None]
    assert "[вложенность скрыта]" in json.dumps(result, ensure_ascii=False)
    json.dumps(result, ensure_ascii=False, allow_nan=False)


@pytest.mark.parametrize(
    ("message", "secret"),
    [
        ('failed {"password":"secret-json"}', "secret-json"),
        ('failed {"password": "secret-json-spaced"}', "secret-json-spaced"),
        ("failed {'password':'secret-dict'}", "secret-dict"),
        ("failed {'password': 'secret-dict-spaced'}", "secret-dict-spaced"),
        ('failed {"api_key": "secret-api"}', "secret-api"),
        ('failed {"Authorization": "Bearer secret-bearer"}', "secret-bearer"),
        ("failed {'authorization': 'Bearer secret-dict-bearer'}", "secret-dict-bearer"),
        ("failed password=secret-assignment", "secret-assignment"),
        ("failed token: secret-colon", "secret-colon"),
        ("failed Bearer secret-bearer-standalone", "secret-bearer-standalone"),
        ('failed {"cookie": "secret-cookie"}', "secret-cookie"),
        ("failed private_key=secret-private-key", "secret-private-key"),
        ("failed openai_api_key=secret-openai", "secret-openai"),
        ('failed {"password":"secret\\"quoted"}', 'secret\\"quoted'),
        ("failed https://user:password@example.invalid/", "password"),
        ("failed https://example.invalid/?api_key=secret-query", "secret-query"),
    ],
)
def test_serializer_redacts_quoted_credentials_and_common_secret_forms(
    message: str, secret: str
) -> None:
    result = serialize_dev_result(
        DevResult(
            ok=False,
            code="DEV_ERROR",
            message=message,
            state=DevStatusKind.FAILED.value,
        )
    )

    assert secret not in result["message"]
    assert "***" in result["message"]
    assert "failed" in result["message"]


def test_serializer_rejects_invalid_session_id_in_public_fields() -> None:
    result = serialize_dev_result(
        DevResult(
            ok=True,
            code="DEV_OK",
            message="готово",
            state="stopped",
            session_id="../foreign",
            details={
                "evidence": {
                    "session_id": "../foreign",
                }
            },
        )
    )

    assert result["session_id"] is None
    assert result["details"] == {"evidence": {"session_id": None}}


@pytest.mark.parametrize(
    "path",
    [
        "file:///C:/private/token.txt",
        "file:///home/user/private/token.txt",
        r"C:\private\token.txt",
        "C:/private/token.txt",
        "/home/user/private/token.txt",
        r"\\server\share\private\token.txt",
    ],
)
def test_serializer_redacts_absolute_path_forms(path: str) -> None:
    result = serialize_dev_result(
        DevResult(
            ok=False,
            code="DEV_ERROR",
            message=f"failed {path}",
            state=DevStatusKind.FAILED.value,
        )
    )

    assert path not in result["message"]
    assert "[путь скрыт]" in result["message"]


def test_real_preflight_preserves_per_check_machine_fields(tmp_path: Path) -> None:
    manager, _backend = _real_runtime_manager(tmp_path)
    adapter = DevMcpAdapter(lambda: manager)

    result = adapter.call("dev_preflight")

    checks = result["details"]["checks"]
    assert checks
    for check in checks:
        assert set(check) == {"name", "ok", "code", "message"}
        assert isinstance(check["name"], str)
        assert isinstance(check["ok"], bool)
        assert isinstance(check["code"], str)
        assert isinstance(check["message"], str)


def test_real_doctor_preserves_nested_dev_result_machine_fields(tmp_path: Path) -> None:
    manager, _backend = _real_runtime_manager(tmp_path)
    adapter = DevMcpAdapter(lambda: manager)

    result = adapter.call("dev_doctor")

    details = result["details"]
    assert details["preflight"]["ok"] is True
    assert details["preflight"]["code"] == "DEV_PREFLIGHT_OK"
    assert details["status"]["ok"] is True
    assert details["status"]["code"] == "DEV_NO_SESSION"
    assert details["read_only"] is True


def test_real_status_preserves_task_lifecycle_and_policy_snapshot(tmp_path: Path) -> None:
    manager, _backend = _real_runtime_manager(tmp_path)
    adapter = DevMcpAdapter(lambda: manager)

    started = adapter.call("dev_start_session", {"root_tasks": ["RootTask"]})
    assert started["ok"] is True
    try:
        result = adapter.call("dev_status")

        assert result["ok"] is True
        assert result["details"]["task_lifecycle"] == {
            "mode": "task_aware",
            "phase": "running",
            "cleanup_required": True,
            "policy_expected": True,
        }
        assert result["details"]["task_policy"] == {
            "present": True,
            "valid": True,
            "state": "active",
            "session_id": "sandbox-session",
            "root_tasks": ["RootTask"],
            "excluded_tasks": [],
            "allowed_tasks": ["RootTask"],
            "dependencies": [],
        }
        assert "repository_root" not in result["details"]["task_policy"]
        assert "policy_file" not in result["details"]["task_policy"]
    finally:
        stopped = adapter.call("dev_stop_session")
        assert stopped["ok"] is True


def test_real_evidence_tools_expose_lifecycle_timeline_logs_and_image(tmp_path: Path) -> None:
    manager, _backend = _real_runtime_manager(
        tmp_path,
        screenshot_provider=lambda _session_id: np.zeros((2, 3, 3), dtype=np.uint8),
    )
    adapter = DevMcpAdapter(lambda: manager)

    started = adapter.call("dev_start_session", {"root_tasks": ["RootTask"]})
    assert started["ok"] is True
    try:
        evidence = adapter.call("dev_get_evidence")
        assert evidence["ok"] is True
        assert "profile" not in evidence["details"]
        assert evidence["details"]["git_snapshot"]["available"] is False
        assert evidence["details"]["logs"]["available"] is True
        assert evidence["details"]["current_task"] is None
        assert "cleanup" in evidence["details"]
        assert set(evidence["details"]["cleanup"]) == {
            "status",
            "confirmed",
            "preserved",
            "updated_at",
        }

        store = EvidenceStore.for_session(manager.environment, started["session_id"])
        store.record_task("RootTask", timestamp="2026-08-29T00:00:00+00:00")
        active_evidence = adapter.call("dev_get_evidence")
        assert active_evidence["details"]["current_task"] == "RootTask"

        timeline = adapter.call("dev_get_timeline", {"limit": 100})
        event_types = [event["type"] for event in timeline["details"]["events"]]
        assert event_types[:4] == [
            "session_created",
            "policy_prepared",
            "process_started",
            "session_ready",
        ]
        assert timeline["details"]["more"] is False

        manager.environment.log_file.write_text(
            "новая запись password=секрет\n",
            encoding="utf-8",
        )
        logs = adapter.call("dev_get_logs", {"limit": 10})
        assert logs["ok"] is True
        assert logs["details"]["items"] == [
            {"text": "новая запись password=***", "truncated": False}
        ]

        screenshot = adapter.call("dev_get_screenshot")
        assert screenshot.structured["ok"] is True
        assert screenshot.mime_type == "image/png"
        assert screenshot.image
        assert "screenshot" in screenshot.structured["details"]
        metadata = screenshot.structured["details"]["screenshot"]
        assert metadata["byte_size"] == len(screenshot.image)
        assert metadata["sha256"] == hashlib.sha256(screenshot.image).hexdigest()
        assert "base64" not in json.dumps(screenshot.structured, ensure_ascii=False)
    finally:
        stopped = adapter.call("dev_stop_session")
        assert stopped["ok"] is True

    after_stop = adapter.call("dev_get_evidence")
    assert after_stop["ok"] is True
    assert after_stop["details"]["current_task"] is None
    assert after_stop["details"]["cleanup"]["confirmed"] is True
    assert after_stop["details"]["lifecycle"]["duration_seconds"] == 0


def test_historical_evidence_lookup_does_not_replace_active_store(tmp_path: Path) -> None:
    manager, _backend = _real_runtime_manager(tmp_path)
    adapter = DevMcpAdapter(lambda: manager)

    started = adapter.call("dev_start_session", {"root_tasks": ["RootTask"]})
    assert started["ok"] is True
    try:
        historical = EvidenceStore.create(
            manager.environment,
            session_id="historical-session",
            root_tasks=["RootTask"],
            excluded_tasks=[],
            timestamp="2026-08-29T00:00:00+00:00",
        )
        historical.finalize(
            stopped_at="2026-08-29T00:00:00+00:00",
            cleanup_confirmed=True,
        )
        selected = adapter.call(
            "dev_get_evidence",
            {"session_id": "historical-session"},
        )
        assert selected["ok"] is True
    finally:
        assert adapter.call("dev_stop_session")["ok"] is True

    active_events = EvidenceStore.for_session(
        manager.environment, started["session_id"]
    ).timeline_page(limit=100)["events"]
    historical_events = EvidenceStore.for_session(
        manager.environment, "historical-session"
    ).timeline_page(limit=100)["events"]
    assert "process_stopped" in [event["type"] for event in active_events]
    assert historical_events == []


def test_historical_evidence_uses_manifest_profile_after_target_switch(tmp_path: Path) -> None:
    manager, _backend = _real_runtime_manager(tmp_path)
    historical_environment = replace(
        manager.environment,
        dev_target=DevTarget("historical-target"),
    )
    historical = EvidenceStore.create(
        historical_environment,
        session_id="historical-profile-session",
        root_tasks=["RootTask"],
        excluded_tasks=[],
        timestamp="2026-08-29T00:00:00+00:00",
    )
    historical.finalize(
        stopped_at="2026-08-29T00:00:00+00:00",
        cleanup_confirmed=True,
    )

    result = manager.get_evidence(session_id="historical-profile-session")

    assert result.ok is True
    assert result.details["profile"] == "historical-target"


def test_serializer_drops_unknown_fields_in_known_nested_structures() -> None:
    result = serialize_dev_result(
        {
            "ok": True,
            "code": "DEV_STATUS_OK",
            "message": "ready",
            "state": "running_owned",
            "details": {
                "task_lifecycle": {
                    "mode": "task_aware",
                    "phase": "running",
                    "cleanup_required": True,
                    "policy_expected": True,
                    "unexpected": "drop-me",
                    "repository_root": "C:\\private\\repo",
                },
                "task_policy": {
                    "present": True,
                    "valid": True,
                    "state": "active",
                    "session_id": "session-1",
                    "profile": "ap",
                    "root_tasks": ["RootTask"],
                    "repository_root": "C:\\private\\repo",
                    "policy_file": "C:\\private\\policy.json",
                    "unexpected": {"token": "secret"},
                },
                "preflight": {
                    "ok": True,
                    "code": "DEV_PREFLIGHT_OK",
                    "message": "ready",
                    "state": "no_session",
                    "details": {
                        "checks": [
                            {
                                "name": "profile",
                                "ok": True,
                                "code": "OK",
                                "message": "ready",
                                "unexpected": "drop-me",
                            }
                        ],
                        "unexpected": "drop-me",
                    },
                    "unexpected": "drop-me",
                },
                "unexpected": "drop-me",
            },
            "repository_root": "C:\\private\\repo",
        }
    )

    assert result["details"]["task_lifecycle"] == {
        "mode": "task_aware",
        "phase": "running",
        "cleanup_required": True,
        "policy_expected": True,
    }
    assert "profile" not in result["details"]["task_policy"]
    assert "repository_root" not in result["details"]["task_policy"]
    assert "policy_file" not in result["details"]["task_policy"]
    assert "unexpected" not in result["details"]
    assert "unexpected" not in result["details"]["preflight"]
    assert "unexpected" not in result["details"]["preflight"]["details"]
    assert "unexpected" not in result["details"]["preflight"]["details"]["checks"][0]
    assert "repository_root" not in result


def test_read_only_tools_leave_profile_and_runtime_state_unchanged(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "module").mkdir()
    (root / "gui.py").write_text("# тестовый gui\n", encoding="utf-8")
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
        dev_target=DevTarget("ap"),
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
    assert results[2]["details"] == {
        "tasks": [
            {
                "section": "RootTask",
                "command": "RootTask",
                "enabled": False,
                "next_run": "2026-08-29 00:00:00",
            }
        ],
    }
    assert results[3]["details"]["plan"] == {
        "root_tasks": ["RootTask"],
        "excluded_tasks": [],
        "catalog": ["RootTask"],
    }
    assert before == after
