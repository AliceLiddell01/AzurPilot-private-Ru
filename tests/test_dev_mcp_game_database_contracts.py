from __future__ import annotations

from collections.abc import Mapping

from module.dev_mcp.adapter import DevMcpAdapter
from module.dev_runtime import DevResult, DevStatusKind


def _result(code: str, details: dict[str, object]) -> DevResult:
    return DevResult(
        ok=True,
        code=code,
        message="synthetic game database result",
        state=DevStatusKind.NO_SESSION.value,
        details=details,
    )


class _GameDatabaseManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def list_game_observation_capabilities(self) -> DevResult:
        self.calls.append(("list_game_observation_capabilities", None))
        return _result(
            "DEV_GAME_OBSERVATION_CAPABILITIES_READY",
            {
                "capabilities": [
                    {
                        "capability_id": "resources",
                        "kind": "game_observation",
                        "description": "resources",
                        "source": "application.game_read_service",
                        "parameters": [],
                        "secret": "must be removed",
                    }
                ]
            },
        )

    def get_game_observation(
        self,
        capability_id: str,
        parameters: Mapping[str, object] | None = None,
        *,
        session_id: str | None = None,
    ) -> DevResult:
        self.calls.append(("get_game_observation", (capability_id, parameters, session_id)))
        return _result(
            "DEV_GAME_OBSERVATION_READY",
            {
                "observation": {
                    "schema_version": 1,
                    "observation_id": "observation-1",
                    "session_id": session_id,
                    "smoke_id": None,
                    "profile_name": "fixture-target",
                    "target_identity": "a" * 64,
                    "checkpoint_id": "standalone",
                    "capability_id": capability_id,
                    "captured_at": "2026-09-01T12:00:00+00:00",
                    "status": "known",
                    "source": "application.game_read_service",
                    "provenance": {
                        "capability_id": capability_id,
                        "owner": "GameReadService",
                        "freshness": "source_read",
                    },
                    "payload": {
                        "items": [
                            {
                                "key": "oil",
                                "label": "Oil",
                                "value": 1,
                                "limit": None,
                                "total": None,
                                "last_update": None,
                                "token": "must be removed",
                            }
                        ]
                    },
                    "sha256": "b" * 64,
                }
            },
        )

    def capture_smoke_game_checkpoint(self, smoke_id: str, checkpoint_id: str) -> DevResult:
        self.calls.append(("capture_smoke_game_checkpoint", (smoke_id, checkpoint_id)))
        return _result(
            "DEV_GAME_CHECKPOINT_CAPTURED",
            {"game_observations": {"smoke_id": smoke_id, "checkpoint_id": checkpoint_id, "requested": 1, "stored": 1}},
        )

    def get_smoke_game_observations(self, smoke_id: str, checkpoint_id: str | None = None) -> DevResult:
        self.calls.append(("get_smoke_game_observations", (smoke_id, checkpoint_id)))
        return _result(
            "DEV_GAME_OBSERVATIONS_READY",
            {
                "observations": [],
                "summary": {
                    "smoke_id": smoke_id,
                    "count": 0,
                    "required_complete": True,
                    "evidence_refs": [
                        {
                            "source": "game_observation",
                            "reference": "config/state/dev-runtime-smoke/smoke-1/game-observations.json#observation-1",
                            "description": "persisted observation",
                            "secret": "must be removed",
                        }
                    ],
                },
            },
        )

    def get_database_status(self, *, session_id: str | None = None) -> DevResult:
        self.calls.append(("get_database_status", session_id))
        return _result(
            "DEV_DATABASE_STATUS_READY",
            {
                "database_status": {
                    "schema_version": 1,
                    "target_profile": "fixture-target",
                    "marker_ready": False,
                    "connectivity": False,
                    "app_role_ready": False,
                    "expected_schema_head": "0008_dorm_morale_idempotency",
                    "current_schema_head": None,
                    "schema_marker_version": None,
                    "target_resolved": False,
                    "required_tables_ready": False,
                    "domain_consistency": None,
                    "transaction_ready": False,
                    "config_match": False,
                    "checks": [],
                    "password": "must be removed",
                }
            },
        )

    def list_database_checks(self) -> DevResult:
        self.calls.append(("list_database_checks", None))
        return _result(
            "DEV_DATABASE_CHECKS_READY",
            {"database_checks": [{"check_id": "connectivity", "description": "check", "target_scoped": True, "read_only": True}]},
        )

    def run_database_check(self, check_id: str, *, session_id: str | None = None) -> DevResult:
        self.calls.append(("run_database_check", (check_id, session_id)))
        return _result(
            "DEV_DATABASE_CHECK_PASS",
            {"database_check": {"check_id": check_id, "status": "pass", "code": "DEV_DATABASE_CONNECTED", "message": "ok", "observed": True}},
        )

    def list_database_repairs(self) -> DevResult:
        self.calls.append(("list_database_repairs", None))
        return _result("DEV_DATABASE_REPAIRS_READY", {"repairs": []})

    def preview_database_repair(self, repair_id: str, *, session_id: str | None = None) -> DevResult:
        self.calls.append(("preview_database_repair", (repair_id, session_id)))
        return DevResult(
            ok=False,
            code="DEV_DATABASE_REPAIR_UNAVAILABLE",
            message="repair unavailable",
            state=DevStatusKind.NO_SESSION.value,
            session_id=session_id,
            details={"repair": {"repair_id": repair_id, "available": False}},
        )


def test_game_database_tools_delegate_and_serializer_keeps_only_known_fields() -> None:
    manager = _GameDatabaseManager()
    adapter = DevMcpAdapter(lambda: manager)

    capabilities = adapter.call("dev_list_game_observation_capabilities", {})
    observation = adapter.call(
        "dev_get_game_observation",
        {"capability_id": "resources", "parameters": {}, "session_id": "session-1"},
    )
    captured = adapter.call(
        "dev_capture_smoke_game_checkpoint",
        {"smoke_id": "smoke-1", "checkpoint_id": "midpoint"},
    )
    stored = adapter.call(
        "dev_get_smoke_game_observations",
        {"smoke_id": "smoke-1", "checkpoint_id": "midpoint"},
    )
    status = adapter.call("dev_get_database_status", {"session_id": "session-1"})
    checks = adapter.call("dev_list_database_checks", {})
    check = adapter.call(
        "dev_run_database_check",
        {"check_id": "connectivity", "session_id": "session-1"},
    )
    repairs = adapter.call("dev_list_database_repairs", {})
    repair = adapter.call(
        "dev_preview_database_repair",
        {"repair_id": "none", "session_id": "session-1"},
    )

    assert capabilities["details"]["capabilities"][0]["capability_id"] == "resources"
    assert "secret" not in capabilities["details"]["capabilities"][0]
    assert "token" not in observation["details"]["observation"]["payload"]["items"][0]
    assert "password" not in status["details"]["database_status"]
    assert stored["details"]["summary"]["evidence_refs"][0]["source"] == "game_observation"
    assert "secret" not in stored["details"]["summary"]["evidence_refs"][0]
    assert captured["ok"] is True
    assert stored["ok"] is True
    assert checks["ok"] is True
    assert check["ok"] is True
    assert repairs["ok"] is True
    assert repair["ok"] is False
    assert manager.calls == [
        ("list_game_observation_capabilities", None),
        ("get_game_observation", ("resources", {}, "session-1")),
        ("capture_smoke_game_checkpoint", ("smoke-1", "midpoint")),
        ("get_smoke_game_observations", ("smoke-1", "midpoint")),
        ("get_database_status", "session-1"),
        ("list_database_checks", None),
        ("run_database_check", ("connectivity", "session-1")),
        ("list_database_repairs", None),
        ("preview_database_repair", ("none", "session-1")),
    ]


def test_game_database_argument_schemas_reject_profile_sql_and_unknown_fields_before_manager_creation() -> None:
    manager = _GameDatabaseManager()
    adapter = DevMcpAdapter(lambda: manager)

    invalid = (
        ("dev_get_game_observation", {"capability_id": "resources", "profile": "fixture-target"}),
        ("dev_get_game_observation", {"capability_id": "resources", "parameters": {"fleet_indices": [1]}, "sql": "SELECT 1"}),
        ("dev_capture_smoke_game_checkpoint", {"smoke_id": "smoke-1", "checkpoint_id": "before"}),
        ("dev_get_smoke_game_observations", {"smoke_id": "smoke-1", "path": "C:\\private"}),
        ("dev_run_database_check", {"check_id": "connectivity", "query": "SELECT 1"}),
        ("dev_preview_database_repair", {"repair_id": "none", "sql": "UPDATE"}),
    )
    for tool_name, arguments in invalid:
        result = adapter.call(tool_name, arguments)
        assert result["ok"] is False
        assert result["code"] == "DEV_MCP_INPUT_INVALID"
    assert manager.calls == []
