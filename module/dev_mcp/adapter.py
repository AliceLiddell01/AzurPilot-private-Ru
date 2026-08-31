"""Тонкая безопасная граница между инструментами MCP и Dev Runtime.

Этот модуль намеренно не создаёт ``DevSessionManager`` при импорте. Единственная
точка, где выбирается runtime, находится в локальном ``_default_manager`` и
использует уже существующий ``DevEnvironment.current()`` внутри менеджера.
"""

from __future__ import annotations

import importlib
import logging
import math
import re
import sys
import threading
from collections.abc import Callable, Mapping
from contextlib import redirect_stdout
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from module.dev_mcp.contract import contract_result
from module.dev_runtime.contracts import DevEnvironment
from module.dev_runtime.evidence import EvidenceScreenshot, validate_session_id
from module.dev_runtime.sanitizer import MAX_SANITIZED_TEXT, redact_text
from module.dev_runtime.smoke import SmokeSpec
from module.dev_runtime.target import DevTargetError, DevTargetRegistry

logger = logging.getLogger(__name__)

DEV_MCP_TOOL_NAMES = (
    "dev_preflight",
    "dev_doctor",
    "dev_get_contract",
    "dev_list_tasks",
    "dev_plan_session",
    "dev_start_session",
    "dev_status",
    "dev_stop_session",
    "dev_cleanup",
    "dev_recover",
    "dev_get_evidence",
    "dev_get_timeline",
    "dev_get_logs",
    "dev_get_screenshot",
    "dev_list_smoke_capabilities",
    "dev_validate_smoke",
    "dev_start_smoke",
    "dev_get_smoke",
    "dev_cancel_smoke",
    "dev_get_smoke_evaluation",
    "dev_submit_smoke_evaluation",
    "dev_get_runtime_status",
    "dev_start_game",
    "dev_stop_game",
    "dev_restart_game",
    "dev_start_emulator",
    "dev_stop_emulator",
    "dev_restart_emulator",
    "dev_restart_adb",
    "dev_get_control_operation",
)

_NO_ARGUMENT_TOOLS = frozenset(
    {
        "dev_preflight",
        "dev_doctor",
        "dev_get_contract",
        "dev_list_tasks",
        "dev_status",
        "dev_cleanup",
        "dev_recover",
        "dev_get_screenshot",
        "dev_list_smoke_capabilities",
        "dev_get_runtime_status",
        "dev_start_game",
        "dev_stop_game",
        "dev_restart_game",
        "dev_start_emulator",
        "dev_stop_emulator",
        "dev_restart_emulator",
        "dev_restart_adb",
    }
)

_MAX_RESULT_TEXT = MAX_SANITIZED_TEXT
_MAX_RESULT_KEY = 128
_MAX_RESULT_DEPTH = 8
_MAX_RESULT_ITEMS = 256
_SAFE_DETAIL_KEYS = frozenset(
    {
        "allowed",
        "allowed_tasks",
        "blockers",
        "catalog",
        "checks",
        "cleanup",
        "cleanup_confirmed",
        "code",
        "command",
        "dependencies",
        "details",
        "enabled",
        "error",
        "excluded_tasks",
        "field",
        "host",
        "items",
        "lifecycle",
        "lifecycle_marked_cleanup_pending",
        "log",
        "message",
        "name",
        "new_dependency",
        "next_run",
        "observed_code",
        "plan",
        "policy_marked",
        "policy_marked_cleanup_pending",
        "policy_removed",
        "policy_state",
        "port",
        "preflight",
        "preserve_task_state",
        "preserved_task_state",
        "present",
        "policy_expected",
        "read_only",
        "reason",
        "relative_log",
        "required_by",
        "root",
        "root_tasks",
        "safe",
        "section",
        "sequence",
        "session_id",
        "state",
        "status",
        "steps",
        "task",
        "task_cleanup",
        "task_lifecycle",
        "task_policy",
        "tasks",
        "tasks_reset",
        "timestamp",
        "tool",
        "type",
        "valid",
        "validation",
        "value",
        "mode",
        "phase",
        "cleanup_required",
        "cleanup_summary",
        "created_at",
        "started_at",
        "stopped_at",
        "roots",
        "excluded",
        "current_task",
        "dependency_summary",
        "duration_seconds",
        "evidence",
        "git_snapshot",
        "evidence_health",
        "timeline",
        "logs",
        "screenshots",
        "last_error",
        "events",
        "next_after_sequence",
        "more",
        "truncated",
        "health",
        "next_cursor",
        "text",
        "screenshot",
        "screenshot_id",
        "mime",
        "width",
        "height",
        "byte_size",
        "sha256",
        "available",
        "changed_paths",
        "head",
        "branch",
        "detached",
        "dirty",
        "reasons",
        "count",
        "latest",
        "source",
        "relative_file",
        "event_count",
        "first_sequence",
        "last_sequence",
        "last_timestamp",
        "fields",
        "confirmed",
        "preserved",
        "updated_at",
        "frames",
        "line",
        "function",
        "module",
        "exception_type",
        "outcome",
        "conflict_state",
        "result",
        "smoke_id",
        "spec_hash",
        "deadline_at",
        "finished_at",
        "source_snapshot",
        "scope",
        "config_override_count",
        "assertion_count",
        "visual_assertion_count",
        "issues",
        "progress",
        "assertions",
        "pending_evaluation",
        "rubric",
        "rubric_hash",
        "screenshot_sha256",
        "overrides",
        "persisted",
        "applied",
        "restored",
        "verified",
        "primary_failure",
        "harness_failure",
        "external_verdict",
        "verdict",
        "rationale",
        "submitted_at",
        "capabilities",
        "capability_id",
        "kind",
        "config_schema",
        "value_type",
        "required",
        "minimum",
        "maximum",
        "enum_values",
        "deterministic",
        "external",
        "description",
        "contract",
        "development_target",
        "emulator",
        "adb",
        "game",
        "dev_session",
        "smoke",
        "control_operation",
        "operation",
        "configured",
        "detected",
        "readiness",
        "reachable",
        "foreground",
        "unrelated_devices",
        "active",
        "action",
        "transitions",
        "control_id",
    }
)

_SAFE_RESULT_KEYS = frozenset(
    {
        "ok",
        "code",
        "message",
        "state",
        "status",
        "session_id",
        "details",
        "confirmed",
        "preserved",
        "updated_at",
    }
)
_SAFE_CONTRACT_KEYS = frozenset(
    {
        "contract_schema_version",
        "product_family",
        "dev_mcp_api_version",
        "smoke_spec_schema_version",
        "smoke_result_schema_version",
        "feature_flags",
        "capability_families",
        "result_outcomes",
    }
)
_SAFE_FEATURE_FLAG_KEYS = frozenset(
    {"task_sandbox", "evidence_api", "universal_smoke_harness", "external_visual_evaluation", "runtime_control", "game_lifecycle", "emulator_lifecycle", "adb_maintenance"}
)
_SAFE_PREFLIGHT_CHECK_KEYS = frozenset({"name", "ok", "code", "message"})
_SAFE_TASK_LIFECYCLE_KEYS = frozenset(
    {"mode", "phase", "cleanup_required", "policy_expected"}
)
_SAFE_TASK_POLICY_KEYS = frozenset(
    {
        "schema_version",
        "present",
        "valid",
        "code",
        "state",
        "session_id",
        "root_tasks",
        "excluded_tasks",
        "allowed_tasks",
        "catalog",
        "dependencies",
        "created_at",
        "updated_at",
    }
)
_SAFE_TASK_PROVENANCE_KEYS = frozenset(
    {"task", "required_by", "root", "reason", "sequence", "timestamp"}
)
_SAFE_TASK_DESCRIPTOR_KEYS = frozenset(
    {"section", "command", "enabled", "next_run"}
)
_SAFE_TASK_CATALOG_KEYS = frozenset({"tasks"})
_SAFE_TASK_PLAN_KEYS = frozenset(
    {"root_tasks", "excluded_tasks", "catalog"}
)
_SAFE_ERROR_KEYS = frozenset({"type", "code", "message", "field", "tasks"})

_SAFE_EVIDENCE_HEALTH_KEYS = frozenset({"status", "reasons"})
_SAFE_GIT_SNAPSHOT_KEYS = frozenset(
    {"head", "branch", "detached", "dirty", "changed_paths", "available", "reason"}
)
_SAFE_TIMELINE_METADATA_KEYS = frozenset(
    {"relative_file", "event_count", "first_sequence", "last_sequence", "last_timestamp", "truncated"}
)
_SAFE_TIMELINE_EVENT_KEYS = frozenset({"sequence", "timestamp", "type", "fields"})
_SAFE_EVENT_FIELDS_KEYS = frozenset(
    {
        "code",
        "confirmed",
        "current_task",
        "dependency_sequence",
        "exception_type",
        "mode",
        "outcome",
        "phase",
        "policy_state",
        "preserved",
        "reason",
        "reason_code",
        "required_by",
        "root",
        "source",
        "state",
        "task",
        "task_mode",
        "type",
    }
)
_SAFE_LIFECYCLE_KEYS = frozenset({"created_at", "started_at", "stopped_at", "duration_seconds"})
_SAFE_DEPENDENCY_SUMMARY_KEYS = frozenset({"count", "last"})
_SAFE_CLEANUP_SUMMARY_KEYS = frozenset({"status", "confirmed", "preserved", "updated_at"})
_SAFE_LOG_SUMMARY_KEYS = frozenset({"available", "source", "truncated"})
_SAFE_SCREENSHOT_METADATA_KEYS = frozenset(
    {"screenshot_id", "timestamp", "mime", "width", "height", "byte_size", "sha256"}
)
_SAFE_SCREENSHOT_SUMMARY_KEYS = frozenset({"count", "latest"})
_SAFE_LOG_ITEM_KEYS = frozenset({"text", "truncated"})
_SAFE_LOG_PAGE_KEYS = frozenset({"session_id", "items", "next_cursor", "more", "truncated", "health"})
_SAFE_TIMELINE_PAGE_KEYS = frozenset(
    {"session_id", "events", "next_after_sequence", "more", "truncated", "health"}
)
_SAFE_FRAME_KEYS = frozenset({"path", "line", "function", "module"})
_SAFE_STRUCTURED_ERROR_KEYS = frozenset(
    {"type", "message", "phase", "task", "timestamp", "sequence", "frames"}
)
_SAFE_EVIDENCE_SUMMARY_KEYS = frozenset(
    {
        "session_id",
        "lifecycle",
        "roots",
        "excluded",
        "current_task",
        "dependency_summary",
        "git_snapshot",
        "evidence_health",
        "timeline",
        "logs",
        "screenshots",
        "last_error",
        "cleanup",
    }
)
_SAFE_SMOKE_SOURCE_KEYS = frozenset(
    {"head", "branch", "detached", "dirty", "changed_paths", "available", "fingerprint"}
)
_SAFE_SMOKE_SCOPE_KEYS = frozenset(
    {"root_tasks", "excluded_tasks", "config_override_count", "assertion_count", "visual_assertion_count"}
)
_SAFE_SMOKE_PROGRESS_KEYS = frozenset(
    {"passed", "failed", "pending", "unavailable", "elapsed_seconds", "current_task", "evidence_health"}
)
_SAFE_SMOKE_ASSERTION_KEYS = frozenset(
    {"assertion_id", "capability_id", "required", "status", "evidence_source", "evidence_refs", "message"}
)
_SAFE_SMOKE_EVIDENCE_REF_KEYS = frozenset({"source", "reference", "description"})
_SAFE_SMOKE_FAILURE_KEYS = frozenset({"code", "message", "assertion_id"})
_SAFE_SMOKE_CLEANUP_KEYS = frozenset(
    {"attempted", "session_stopped", "task_cleanup_confirmed", "scheduler_clean", "overrides_restored", "source_unchanged", "no_owned_orphan", "port_free", "confirmed", "failure_code"}
)
_SAFE_SMOKE_OVERRIDES_KEYS = frozenset({"persisted", "applied", "restored", "verified", "baseline_digest"})
_SAFE_SMOKE_PENDING_KEYS = frozenset(
    {"assertion_id", "screenshot_id", "screenshot_sha256", "rubric", "rubric_hash", "spec_hash", "session_id", "submitted"}
)
_SAFE_SMOKE_VERDICT_KEYS = frozenset(
    {"source", "assertion_id", "screenshot_id", "screenshot_sha256", "spec_hash", "rubric_hash", "verdict", "rationale", "submitted_at"}
)
_SAFE_SMOKE_ISSUE_KEYS = frozenset({"code", "message"})
_SAFE_SMOKE_FIELD_KEYS = frozenset({"name", "value_type", "required", "minimum", "maximum", "enum_values"})
_SAFE_SMOKE_SCHEMA_KEYS = frozenset({"fields"})
_SAFE_SMOKE_CAPABILITY_KEYS = frozenset(
    {"capability_id", "kind", "config_schema", "evidence_source", "deterministic", "external", "available", "description"}
)
_SAFE_SMOKE_RESULT_KEYS = frozenset(
    {"schema_version", "smoke_id", "spec_hash", "outcome", "code", "message", "source", "session_id", "assertions", "cleanup", "primary_failure", "harness_failure", "external_verdict", "finished_at"}
)
_SAFE_RUNTIME_TARGET_KEYS = frozenset({"configured"})
_SAFE_RUNTIME_EMULATOR_KEYS = frozenset({"detected", "running", "readiness"})
_SAFE_RUNTIME_ADB_KEYS = frozenset({"reachable", "state", "unrelated_devices"})
_SAFE_RUNTIME_GAME_KEYS = frozenset({"reachable", "foreground", "running"})
_SAFE_RUNTIME_SESSION_KEYS = frozenset({"state"})
_SAFE_RUNTIME_SMOKE_KEYS = frozenset({"active"})
_SAFE_CONTROL_OPERATION_KEYS = frozenset(
    {
        "control_id",
        "action",
        "target_profile_name",
        "target_identity",
        "runtime_config_fingerprint",
        "state",
        "outcome",
        "created_at",
        "started_at",
        "deadline_at",
        "finished_at",
        "transitions",
    }
)
_SAFE_CONTROL_STATUS_KEYS = frozenset({"active", "operation", "code"})
_SAFE_CONTROL_TRANSITION_KEYS = frozenset({"timestamp", "state", "code"})

_CONTRACT_CHILD_SCHEMAS: dict[str, str | None] = {
    "contract_schema_version": "int",
    "product_family": "string",
    "dev_mcp_api_version": "int",
    "smoke_spec_schema_version": "int",
    "smoke_result_schema_version": "int",
    "feature_flags": "feature_flags",
    "capability_families": "string_list",
    "result_outcomes": "string_list",
}
_FEATURE_FLAG_CHILD_SCHEMAS: dict[str, str | None] = {
    "task_sandbox": "bool",
    "evidence_api": "bool",
    "universal_smoke_harness": "bool",
    "external_visual_evaluation": "bool",
    "runtime_control": "bool",
    "game_lifecycle": "bool",
    "emulator_lifecycle": "bool",
    "adb_maintenance": "bool",
}

_SCHEMA_KEYS = {
    "details": _SAFE_DETAIL_KEYS,
    "result": _SAFE_RESULT_KEYS,
    "contract": _SAFE_CONTRACT_KEYS,
    "feature_flags": _SAFE_FEATURE_FLAG_KEYS,
    "preflight_check": _SAFE_PREFLIGHT_CHECK_KEYS,
    "task_lifecycle": _SAFE_TASK_LIFECYCLE_KEYS,
    "task_policy": _SAFE_TASK_POLICY_KEYS,
    "task_provenance": _SAFE_TASK_PROVENANCE_KEYS,
    "task_descriptor": _SAFE_TASK_DESCRIPTOR_KEYS,
    "task_catalog": _SAFE_TASK_CATALOG_KEYS,
    "task_plan": _SAFE_TASK_PLAN_KEYS,
    "error": _SAFE_ERROR_KEYS,
    "evidence_summary": _SAFE_EVIDENCE_SUMMARY_KEYS,
    "evidence_health": _SAFE_EVIDENCE_HEALTH_KEYS,
    "git_snapshot": _SAFE_GIT_SNAPSHOT_KEYS,
    "timeline_metadata": _SAFE_TIMELINE_METADATA_KEYS,
    "timeline_page": _SAFE_TIMELINE_PAGE_KEYS,
    "timeline_event": _SAFE_TIMELINE_EVENT_KEYS,
    "event_fields": _SAFE_EVENT_FIELDS_KEYS,
    "lifecycle": _SAFE_LIFECYCLE_KEYS,
    "dependency_summary": _SAFE_DEPENDENCY_SUMMARY_KEYS,
    "cleanup_summary": _SAFE_CLEANUP_SUMMARY_KEYS,
    "log_summary": _SAFE_LOG_SUMMARY_KEYS,
    "log_page": _SAFE_LOG_PAGE_KEYS,
    "log_item": _SAFE_LOG_ITEM_KEYS,
    "screenshot_summary": _SAFE_SCREENSHOT_SUMMARY_KEYS,
    "screenshot_metadata": _SAFE_SCREENSHOT_METADATA_KEYS,
    "structured_error": _SAFE_STRUCTURED_ERROR_KEYS,
    "frame": _SAFE_FRAME_KEYS,
    "smoke_source": _SAFE_SMOKE_SOURCE_KEYS,
    "smoke_scope": _SAFE_SMOKE_SCOPE_KEYS,
    "smoke_progress": _SAFE_SMOKE_PROGRESS_KEYS,
    "smoke_assertion": _SAFE_SMOKE_ASSERTION_KEYS,
    "smoke_evidence_ref": _SAFE_SMOKE_EVIDENCE_REF_KEYS,
    "smoke_failure": _SAFE_SMOKE_FAILURE_KEYS,
    "smoke_cleanup": _SAFE_SMOKE_CLEANUP_KEYS,
    "smoke_overrides": _SAFE_SMOKE_OVERRIDES_KEYS,
    "smoke_pending": _SAFE_SMOKE_PENDING_KEYS,
    "smoke_verdict": _SAFE_SMOKE_VERDICT_KEYS,
    "smoke_issue": _SAFE_SMOKE_ISSUE_KEYS,
    "smoke_field": _SAFE_SMOKE_FIELD_KEYS,
    "smoke_schema": _SAFE_SMOKE_SCHEMA_KEYS,
    "smoke_capability": _SAFE_SMOKE_CAPABILITY_KEYS,
    "smoke_result": _SAFE_SMOKE_RESULT_KEYS,
    "runtime_target": _SAFE_RUNTIME_TARGET_KEYS,
    "runtime_emulator": _SAFE_RUNTIME_EMULATOR_KEYS,
    "runtime_adb": _SAFE_RUNTIME_ADB_KEYS,
    "runtime_game": _SAFE_RUNTIME_GAME_KEYS,
    "runtime_session": _SAFE_RUNTIME_SESSION_KEYS,
    "runtime_smoke": _SAFE_RUNTIME_SMOKE_KEYS,
    "control_operation": _SAFE_CONTROL_OPERATION_KEYS,
    "control_status": _SAFE_CONTROL_STATUS_KEYS,
    "control_transition": _SAFE_CONTROL_TRANSITION_KEYS,
}

_DETAIL_CHILD_SCHEMAS: dict[str, str | None] = {
    "allowed": "bool",
    "allowed_tasks": "string_list",
    "available": "bool",
    "blockers": "string_list",
    "branch": "string",
    "byte_size": "int",
    "catalog": "catalog",
    "changed_paths": "string_list",
    "checks": "preflight_checks",
    "cleanup": "cleanup_or_result",
    "cleanup_confirmed": "bool",
    "cleanup_summary": "cleanup_summary",
    "code": "string",
    "command": "string",
    "conflict_state": "string",
    "count": "int",
    "created_at": "string",
    "current_task": "string",
    "dependencies": "task_provenance_list",
    "dependency_summary": "dependency_summary",
    "details": "details",
    "detached": "bool",
    "dirty": "bool",
    "enabled": "bool",
    "error": "error",
    "events": "timeline_events",
    "evidence": "evidence_summary",
    "evidence_health": "evidence_health",
    "excluded": "string_list",
    "excluded_tasks": "string_list",
    "field": "string",
    "fields": "event_fields",
    "first_sequence": "int",
    "frames": "frame_list",
    "git_snapshot": "git_snapshot",
    "head": "string",
    "health": "evidence_health",
    "height": "int",
    "host": "string",
    "items": "log_items",
    "last_error": "structured_error",
    "last_sequence": "int",
    "last_timestamp": "string",
    "latest": "screenshot_metadata",
    "lifecycle": "lifecycle",
    "lifecycle_marked_cleanup_pending": "bool",
    "log": "string",
    "logs": "log_summary",
    "message": "string",
    "mime": "string",
    "more": "bool",
    "name": "string",
    "new_dependency": "bool",
    "next_after_sequence": "int",
    "next_cursor": "string",
    "next_run": "string",
    "observed_code": "string",
    "outcome": "string",
    "plan": "task_plan",
    "policy_expected": "bool",
    "policy_marked": "bool",
    "policy_marked_cleanup_pending": "bool",
    "policy_removed": "bool",
    "policy_state": "string",
    "port": "int",
    "preflight": "result",
    "preserve_task_state": "bool",
    "preserved_task_state": "bool",
    "present": "bool",
    "read_only": "bool",
    "reason": "string",
    "reasons": "string_list",
    "relative_file": "string",
    "relative_log": "string",
    "required_by": "string",
    "root": "string",
    "roots": "string_list",
    "root_tasks": "string_list",
    "safe": None,
    "screenshot": "screenshot_metadata",
    "screenshots": "screenshot_summary",
    "section": "string",
    "sequence": "int",
    "session_id": "session_id",
    "sha256": "string",
    "screenshot_id": "string",
    "source": "smoke_or_string",
    "started_at": "string",
    "state": "string",
    "status": "result",
    "steps": "result_list",
    "stopped_at": "string",
    "task": "string",
    "task_cleanup": "result",
    "task_lifecycle": "task_lifecycle",
    "task_policy": "task_policy",
    "tasks": "task_descriptor_list",
    "tasks_reset": "int",
    "text": "string",
    "timestamp": "string",
    "timeline": "timeline_metadata",
    "tool": "string",
    "truncated": "bool",
    "type": "string",
    "valid": "bool",
    "validation": "string",
    "width": "int",
    "smoke_id": "session_id",
    "spec_hash": "string",
    "deadline_at": "string",
    "finished_at": "string",
    "source_snapshot": "smoke_source",
    "scope": "smoke_scope",
    "config_override_count": "int",
    "assertion_count": "int",
    "visual_assertion_count": "int",
    "issues": "smoke_issue_list",
    "progress": "smoke_progress",
    "assertions": "smoke_assertion_list",
    "pending_evaluation": "smoke_pending",
    "rubric": "string",
    "rubric_hash": "string",
    "screenshot_sha256": "string",
    "overrides": "smoke_overrides",
    "persisted": "bool",
    "applied": "bool",
    "restored": "bool",
    "verified": "bool",
    "primary_failure": "smoke_failure",
    "harness_failure": "smoke_failure",
    "external_verdict": "smoke_verdict",
    "verdict": "string",
    "rationale": "string",
    "submitted_at": "string",
    "capabilities": "smoke_capability_list",
    "capability_id": "string",
    "kind": "string",
    "config_schema": "smoke_schema",
    "value_type": "string",
    "required": "bool",
    "minimum": "number",
    "maximum": "number",
    "enum_values": "string_list",
    "deterministic": "bool",
    "external": "bool",
    "description": "string",
    "contract": "contract",
    "result": "result_or_smoke",
    "development_target": "runtime_target",
    "emulator": "runtime_emulator",
    "adb": "runtime_adb",
    "game": "runtime_game",
    "dev_session": "runtime_session",
    "smoke": "runtime_smoke",
    "control_operation": "control_status",
    "operation": "control_operation",
    "configured": "bool",
    "detected": "bool",
    "readiness": "bool",
    "reachable": "bool",
    "foreground": "bool",
    "unrelated_devices": "bool",
    "active": "bool",
    "action": "string",
    "target_profile_name": "string",
    "target_identity": "string",
    "runtime_config_fingerprint": "string",
    "transitions": "control_transition_list",
    "control_id": "string",
}

_RESULT_CHILD_SCHEMAS: dict[str, str | None] = {
    "ok": "bool",
    "code": "string",
    "message": "string",
    "state": "string",
    "status": "string",
    "session_id": "session_id",
    "details": "details",
    "confirmed": "bool",
    "preserved": "bool",
    "updated_at": "string",
}

_TASK_POLICY_CHILD_SCHEMAS: dict[str, str | None] = {
    "schema_version": "int",
    "present": "bool",
    "valid": "bool",
    "code": "string",
    "state": "string",
    "session_id": "session_id",
    "root_tasks": "string_list",
    "excluded_tasks": "string_list",
    "allowed_tasks": "string_list",
    "catalog": "string_list",
    "dependencies": "task_provenance_list",
    "created_at": "string",
    "updated_at": "string",
}

_TASK_PROVENANCE_CHILD_SCHEMAS: dict[str, str | None] = {
    "task": "string",
    "required_by": "string",
    "root": "string",
    "reason": "string",
    "sequence": "int",
    "timestamp": "string",
}

_TASK_DESCRIPTOR_CHILD_SCHEMAS: dict[str, str | None] = {
    "section": "string",
    "command": "string",
    "enabled": "bool",
    "next_run": "string",
}

_TASK_CATALOG_CHILD_SCHEMAS: dict[str, str | None] = {
    "tasks": "task_descriptor_list",
}

_TASK_PLAN_CHILD_SCHEMAS: dict[str, str | None] = {
    "root_tasks": "string_list",
    "excluded_tasks": "string_list",
    "catalog": "string_list",
}

_ERROR_CHILD_SCHEMAS: dict[str, str | None] = {
    "type": "string",
    "code": "string",
    "message": "string",
    "field": "string",
    "tasks": "string_list",
}

_EVIDENCE_SUMMARY_CHILD_SCHEMAS: dict[str, str | None] = {
    "session_id": "session_id",
    "lifecycle": "lifecycle",
    "roots": "string_list",
    "excluded": "string_list",
    "current_task": "string",
    "dependency_summary": "dependency_summary",
    "git_snapshot": "git_snapshot",
    "evidence_health": "evidence_health",
    "timeline": "timeline_metadata",
    "logs": "log_summary",
    "screenshots": "screenshot_summary",
    "last_error": "structured_error",
    "cleanup": "cleanup_summary",
}

_EVIDENCE_HEALTH_CHILD_SCHEMAS: dict[str, str | None] = {
    "status": "string",
    "reasons": "string_list",
}

_GIT_SNAPSHOT_CHILD_SCHEMAS: dict[str, str | None] = {
    "head": "string",
    "branch": "string",
    "detached": "bool",
    "dirty": "bool",
    "changed_paths": "string_list",
    "available": "bool",
    "reason": "string",
}

_TIMELINE_METADATA_CHILD_SCHEMAS: dict[str, str | None] = {
    "relative_file": "string",
    "event_count": "int",
    "first_sequence": "int",
    "last_sequence": "int",
    "last_timestamp": "string",
    "truncated": "bool",
}

_TIMELINE_PAGE_CHILD_SCHEMAS: dict[str, str | None] = {
    "session_id": "session_id",
    "events": "timeline_events",
    "next_after_sequence": "int",
    "more": "bool",
    "truncated": "bool",
    "health": "evidence_health",
}

_TIMELINE_EVENT_CHILD_SCHEMAS: dict[str, str | None] = {
    "sequence": "int",
    "timestamp": "string",
    "type": "string",
    "fields": "event_fields",
}

_EVENT_FIELDS_CHILD_SCHEMAS: dict[str, str | None] = {
    "code": "string",
    "confirmed": "bool",
    "current_task": "string",
    "dependency_sequence": "int",
    "exception_type": "string",
    "mode": "string",
    "outcome": "string",
    "phase": "string",
    "policy_state": "string",
    "preserved": "bool",
    "reason": "string",
    "reason_code": "string",
    "required_by": "string",
    "root": "string",
    "source": "string",
    "state": "string",
    "task": "string",
    "task_mode": "string",
    "type": "string",
}

_LIFECYCLE_CHILD_SCHEMAS: dict[str, str | None] = {
    "created_at": "string",
    "started_at": "string",
    "stopped_at": "string",
    "duration_seconds": "int",
}

_DEPENDENCY_SUMMARY_CHILD_SCHEMAS: dict[str, str | None] = {
    "count": "int",
    "last": "task_provenance",
}

_CLEANUP_SUMMARY_CHILD_SCHEMAS: dict[str, str | None] = {
    "status": "string",
    "confirmed": "bool",
    "preserved": "bool",
    "updated_at": "string",
}

_LOG_SUMMARY_CHILD_SCHEMAS: dict[str, str | None] = {
    "available": "bool",
    "source": "string",
    "truncated": "bool",
}

_LOG_PAGE_CHILD_SCHEMAS: dict[str, str | None] = {
    "session_id": "session_id",
    "items": "log_items",
    "next_cursor": "string",
    "more": "bool",
    "truncated": "bool",
    "health": "evidence_health",
}

_LOG_ITEM_CHILD_SCHEMAS: dict[str, str | None] = {
    "text": "string",
    "truncated": "bool",
}

_SCREENSHOT_SUMMARY_CHILD_SCHEMAS: dict[str, str | None] = {
    "count": "int",
    "latest": "screenshot_metadata",
}

_SCREENSHOT_METADATA_CHILD_SCHEMAS: dict[str, str | None] = {
    "screenshot_id": "string",
    "timestamp": "string",
    "mime": "string",
    "width": "int",
    "height": "int",
    "byte_size": "int",
    "sha256": "string",
}

_STRUCTURED_ERROR_CHILD_SCHEMAS: dict[str, str | None] = {
    "type": "string",
    "message": "string",
    "phase": "string",
    "task": "string",
    "timestamp": "string",
    "sequence": "int",
    "frames": "frame_list",
}

_FRAME_CHILD_SCHEMAS: dict[str, str | None] = {
    "path": "string",
    "line": "int",
    "function": "string",
    "module": "string",
}

_SMOKE_SOURCE_CHILD_SCHEMAS: dict[str, str | None] = {
    "head": "string",
    "branch": "string",
    "detached": "bool",
    "dirty": "bool",
    "changed_paths": "string_list",
    "available": "bool",
    "fingerprint": "string",
}
_SMOKE_SCOPE_CHILD_SCHEMAS: dict[str, str | None] = {
    "root_tasks": "string_list",
    "excluded_tasks": "string_list",
    "config_override_count": "int",
    "assertion_count": "int",
    "visual_assertion_count": "int",
}
_SMOKE_PROGRESS_CHILD_SCHEMAS: dict[str, str | None] = {
    "passed": "int",
    "failed": "int",
    "pending": "int",
    "unavailable": "int",
    "elapsed_seconds": "number",
    "current_task": "string",
    "evidence_health": "string",
}
_SMOKE_EVIDENCE_REF_CHILD_SCHEMAS: dict[str, str | None] = {
    "source": "string",
    "reference": "string",
    "description": "string",
}
_SMOKE_ASSERTION_CHILD_SCHEMAS: dict[str, str | None] = {
    "assertion_id": "string",
    "capability_id": "string",
    "required": "bool",
    "status": "string",
    "evidence_source": "string",
    "evidence_refs": "smoke_evidence_ref_list",
    "message": "string",
}
_SMOKE_FAILURE_CHILD_SCHEMAS: dict[str, str | None] = {
    "code": "string",
    "message": "string",
    "assertion_id": "string",
}
_SMOKE_CLEANUP_CHILD_SCHEMAS: dict[str, str | None] = {
    "attempted": "bool",
    "session_stopped": "bool",
    "task_cleanup_confirmed": "bool",
    "scheduler_clean": "bool",
    "overrides_restored": "bool",
    "source_unchanged": "bool",
    "no_owned_orphan": "bool",
    "port_free": "bool",
    "confirmed": "bool",
    "failure_code": "string",
}
_SMOKE_OVERRIDES_CHILD_SCHEMAS: dict[str, str | None] = {
    "persisted": "bool",
    "applied": "bool",
    "restored": "bool",
    "verified": "bool",
    "baseline_digest": "string",
}
_SMOKE_PENDING_CHILD_SCHEMAS: dict[str, str | None] = {
    "assertion_id": "string",
    "screenshot_id": "string",
    "screenshot_sha256": "string",
    "rubric": "string",
    "rubric_hash": "string",
    "spec_hash": "string",
    "session_id": "session_id",
    "submitted": "bool",
}
_SMOKE_VERDICT_CHILD_SCHEMAS: dict[str, str | None] = {
    "source": "string",
    "assertion_id": "string",
    "screenshot_id": "string",
    "screenshot_sha256": "string",
    "spec_hash": "string",
    "rubric_hash": "string",
    "verdict": "string",
    "rationale": "string",
    "submitted_at": "string",
}
_SMOKE_FIELD_CHILD_SCHEMAS: dict[str, str | None] = {
    "name": "string",
    "value_type": "string",
    "required": "bool",
    "minimum": "number",
    "maximum": "number",
    "enum_values": "string_list",
}
_SMOKE_SCHEMA_CHILD_SCHEMAS: dict[str, str | None] = {"fields": "smoke_field_list"}
_SMOKE_CAPABILITY_CHILD_SCHEMAS: dict[str, str | None] = {
    "capability_id": "string",
    "kind": "string",
    "config_schema": "smoke_schema",
    "evidence_source": "string",
    "deterministic": "bool",
    "external": "bool",
    "available": "bool",
    "description": "string",
}
_SMOKE_RESULT_CHILD_SCHEMAS: dict[str, str | None] = {
    "schema_version": "int",
    "smoke_id": "session_id",
    "spec_hash": "string",
    "outcome": "string",
    "code": "string",
    "message": "string",
    "source": "smoke_source",
    "session_id": "session_id",
    "assertions": "smoke_assertion_list",
    "cleanup": "smoke_cleanup",
    "primary_failure": "smoke_failure",
    "harness_failure": "smoke_failure",
    "external_verdict": "smoke_verdict",
    "finished_at": "string",
}

_SCHEMA_CHILD_SCHEMAS = {
    "details": _DETAIL_CHILD_SCHEMAS,
    "result": _RESULT_CHILD_SCHEMAS,
    "contract": _CONTRACT_CHILD_SCHEMAS,
    "feature_flags": _FEATURE_FLAG_CHILD_SCHEMAS,
    "task_lifecycle": {
        "mode": "string",
        "phase": "string",
        "cleanup_required": "bool",
        "policy_expected": "bool",
    },
    "task_policy": _TASK_POLICY_CHILD_SCHEMAS,
    "task_provenance": _TASK_PROVENANCE_CHILD_SCHEMAS,
    "task_descriptor": _TASK_DESCRIPTOR_CHILD_SCHEMAS,
    "task_catalog": _TASK_CATALOG_CHILD_SCHEMAS,
    "task_plan": _TASK_PLAN_CHILD_SCHEMAS,
    "error": _ERROR_CHILD_SCHEMAS,
    "evidence_summary": _EVIDENCE_SUMMARY_CHILD_SCHEMAS,
    "evidence_health": _EVIDENCE_HEALTH_CHILD_SCHEMAS,
    "git_snapshot": _GIT_SNAPSHOT_CHILD_SCHEMAS,
    "timeline_metadata": _TIMELINE_METADATA_CHILD_SCHEMAS,
    "timeline_page": _TIMELINE_PAGE_CHILD_SCHEMAS,
    "timeline_event": _TIMELINE_EVENT_CHILD_SCHEMAS,
    "event_fields": _EVENT_FIELDS_CHILD_SCHEMAS,
    "lifecycle": _LIFECYCLE_CHILD_SCHEMAS,
    "dependency_summary": _DEPENDENCY_SUMMARY_CHILD_SCHEMAS,
    "cleanup_summary": _CLEANUP_SUMMARY_CHILD_SCHEMAS,
    "log_summary": _LOG_SUMMARY_CHILD_SCHEMAS,
    "log_page": _LOG_PAGE_CHILD_SCHEMAS,
    "log_item": _LOG_ITEM_CHILD_SCHEMAS,
    "screenshot_summary": _SCREENSHOT_SUMMARY_CHILD_SCHEMAS,
    "screenshot_metadata": _SCREENSHOT_METADATA_CHILD_SCHEMAS,
    "structured_error": _STRUCTURED_ERROR_CHILD_SCHEMAS,
    "frame": _FRAME_CHILD_SCHEMAS,
    "smoke_source": _SMOKE_SOURCE_CHILD_SCHEMAS,
    "smoke_scope": _SMOKE_SCOPE_CHILD_SCHEMAS,
    "smoke_progress": _SMOKE_PROGRESS_CHILD_SCHEMAS,
    "smoke_evidence_ref": _SMOKE_EVIDENCE_REF_CHILD_SCHEMAS,
    "smoke_assertion": _SMOKE_ASSERTION_CHILD_SCHEMAS,
    "smoke_failure": _SMOKE_FAILURE_CHILD_SCHEMAS,
    "smoke_cleanup": _SMOKE_CLEANUP_CHILD_SCHEMAS,
    "smoke_overrides": _SMOKE_OVERRIDES_CHILD_SCHEMAS,
    "smoke_pending": _SMOKE_PENDING_CHILD_SCHEMAS,
    "smoke_verdict": _SMOKE_VERDICT_CHILD_SCHEMAS,
    "smoke_field": _SMOKE_FIELD_CHILD_SCHEMAS,
    "smoke_schema": _SMOKE_SCHEMA_CHILD_SCHEMAS,
    "smoke_capability": _SMOKE_CAPABILITY_CHILD_SCHEMAS,
    "smoke_result": _SMOKE_RESULT_CHILD_SCHEMAS,
    "runtime_target": {"configured": "bool"},
    "runtime_emulator": {
        "detected": "bool",
        "running": "bool",
        "readiness": "bool",
    },
    "runtime_adb": {
        "reachable": "bool",
        "state": "string",
        "unrelated_devices": "bool",
    },
    "runtime_game": {
        "reachable": "bool",
        "foreground": "bool",
        "running": "bool",
    },
    "runtime_session": {"state": "string"},
    "runtime_smoke": {"active": "bool"},
    "control_operation": {
        "control_id": "string",
        "action": "string",
        "target_profile_name": "string",
        "target_identity": "string",
        "runtime_config_fingerprint": "string",
        "state": "string",
        "outcome": "string",
        "created_at": "string",
        "started_at": "string",
        "deadline_at": "string",
        "finished_at": "string",
        "transitions": "control_transition_list",
    },
    "control_status": {
        "active": "bool",
        "operation": "control_operation",
        "code": "string",
    },
    "control_transition": {
        "timestamp": "string",
        "state": "string",
        "code": "string",
    },
}


class DevRuntimeManager(Protocol):
    """Минимальный протокол, нужный адаптеру и его модульным тестам."""

    def preflight(self) -> object: ...

    def doctor(self) -> object: ...

    def list_tasks(self) -> object: ...

    def plan(self, *, root_tasks: list[str], excluded_tasks: list[str]) -> object: ...

    def start(self, *, root_tasks: list[str], excluded_tasks: list[str]) -> object: ...

    def status(self) -> object: ...

    def stop(self, *, preserve_task_state: bool = False) -> object: ...

    def cleanup(self) -> object: ...

    def recover(self) -> object: ...

    def get_evidence(self, *, session_id: str | None = None) -> object: ...

    def get_timeline(
        self,
        *,
        session_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> object: ...

    def get_logs(
        self,
        *,
        session_id: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> object: ...

    def get_screenshot(self) -> EvidenceScreenshot: ...

    def get_historical_screenshot(self, *, session_id: str, screenshot_id: str) -> EvidenceScreenshot: ...

    def list_smoke_capabilities(self) -> object: ...

    def validate_smoke(self, spec: object) -> object: ...

    def start_smoke(self, spec: object) -> object: ...

    def get_smoke(self, smoke_id: str) -> object: ...

    def cancel_smoke(self, smoke_id: str) -> object: ...

    def get_smoke_evaluation(self, smoke_id: str) -> EvidenceScreenshot: ...

    def submit_smoke_evaluation(
        self,
        smoke_id: str,
        assertion_id: str,
        verdict: str,
        rationale: str,
    ) -> object: ...

    def get_runtime_status(self) -> object: ...

    def start_game(self) -> object: ...

    def stop_game(self) -> object: ...

    def restart_game(self) -> object: ...

    def start_emulator(self) -> object: ...

    def stop_emulator(self) -> object: ...

    def restart_emulator(self) -> object: ...

    def restart_adb(self) -> object: ...

    def get_control_operation(self, control_id: str) -> object: ...


def _default_manager() -> DevRuntimeManager:
    """Лениво импортировать корень сборки существующего Dev Runtime."""

    from module.dev_runtime import DevSessionManager

    return DevSessionManager()


_LEGACY_LOGGER_LOCK = threading.Lock()


def _ensure_legacy_logger_stderr() -> None:
    """При необходимости изолировать унаследованный Rich logger от stdio MCP."""

    with _LEGACY_LOGGER_LOCK:
        legacy_logger = sys.modules.get("module.logger")
        # Унаследованный ``module.logger`` выводит начальный баннер при импорте
        # и привязывает RichHandler к текущему stdout. Импортируем унаследованные
        # модули только при явном вызове выполнения и перенаправляем этот вывод
        # в stderr до импорта пакетов WebUI диагностическим контуром.
        with redirect_stdout(sys.stderr):
            if legacy_logger is None:
                legacy_logger = importlib.import_module("module.logger")
            if "deploy.logger" not in sys.modules:
                importlib.import_module("deploy.logger")

        for handler in legacy_logger.logger.handlers:
            console = getattr(handler, "console", None)
            if console is not None:
                try:
                    console.file = sys.stderr
                except (AttributeError, TypeError):
                    continue



class _EmptyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _TaskArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    root_tasks: list[str] = Field(min_length=1)
    excluded_tasks: list[str] = Field(default_factory=list)


class _StopArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    preserve_task_state: bool = False


_SESSION_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"


class _SessionArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    session_id: str | None = Field(default=None, pattern=_SESSION_ID_PATTERN)


class _TimelineArguments(_SessionArguments):
    after_sequence: int = Field(default=0, ge=0, le=10**12)
    limit: int = Field(default=100, ge=1, le=200)


class _LogsArguments(_SessionArguments):
    cursor: str | None = Field(default=None, min_length=1, max_length=2048)
    limit: int = Field(default=100, ge=1, le=200)


class _SmokeIdArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    smoke_id: str = Field(min_length=1, max_length=128, pattern=_SESSION_ID_PATTERN)


class _SmokeEvaluationArguments(_SmokeIdArguments):
    assertion_id: str = Field(min_length=1, max_length=128, pattern=_SESSION_ID_PATTERN)
    verdict: Literal["pass", "fail"]
    rationale: str = Field(min_length=1, max_length=1024)


class _ControlIdArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    control_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-f0-9]{32}$")


@dataclass(frozen=True, slots=True)
class DevMcpResponse:
    """Безопасный ответ адаптера с отдельным вложением изображения MCP."""

    structured: dict[str, object]
    image: bytes
    mime_type: str


def _field(result: object, name: str, default: object = None) -> object:
    if isinstance(result, Mapping):
        return result.get(name, default)
    try:
        return getattr(result, name)
    except (AttributeError, TypeError):
        return default


_redact_text = redact_text


def _safe_schema_key(key: object, allowed_keys: frozenset[str]) -> str | None:
    if not isinstance(key, str) or not key or len(key) > _MAX_RESULT_KEY:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
    return normalized if normalized in allowed_keys else None


def _safe_mapping(
    value: Mapping[object, object],
    *,
    schema: str,
    depth: int,
) -> dict[str, object]:
    allowed_keys = _SCHEMA_KEYS.get(schema)
    if allowed_keys is None:
        return {}

    child_schemas = _SCHEMA_CHILD_SCHEMAS.get(schema, {})
    safe: dict[str, object] = {}
    for index, (raw_key, raw_value) in enumerate(value.items()):
        if index >= _MAX_RESULT_ITEMS:
            break
        key = _safe_schema_key(raw_key, allowed_keys)
        if key is None:
            continue
        safe[key] = _safe_value(
            raw_value,
            schema=child_schemas.get(key),
            depth=depth + 1,
        )
    return safe


def _safe_sequence(
    value: list[object] | tuple[object, ...],
    *,
    item_schema: str | None,
    depth: int,
) -> list[object]:
    safe: list[object] = []
    for item in value[:_MAX_RESULT_ITEMS]:
        if item_schema == "string" and not isinstance(item, str):
            continue
        if item_schema == "bool" and not isinstance(item, bool):
            continue
        if item_schema == "int" and (not isinstance(item, int) or isinstance(item, bool)):
            continue
        safe.append(_safe_value(item, schema=item_schema, depth=depth + 1))
    return safe


def _safe_value(
    value: object,
    *,
    schema: str | None = None,
    depth: int = 0,
) -> object:
    if depth > _MAX_RESULT_DEPTH:
        return "[вложенность скрыта]"
    if schema == "string":
        return _redact_text(value) if isinstance(value, str) else None
    if schema == "smoke_or_string":
        if isinstance(value, Mapping):
            return _safe_mapping(value, schema="smoke_source", depth=depth)
        return _redact_text(value) if isinstance(value, str) else None
    if schema == "cleanup_or_result":
        if isinstance(value, Mapping):
            if "ok" in value:
                child_schema = "result"
            elif "attempted" in value or "scheduler_clean" in value:
                child_schema = "smoke_cleanup"
            else:
                child_schema = "cleanup_summary"
            return _safe_mapping(value, schema=child_schema, depth=depth)
        return None
    if schema == "result_or_smoke":
        if isinstance(value, Mapping):
            child_schema = "result" if "ok" in value else "smoke_result"
            return _safe_mapping(value, schema=child_schema, depth=depth)
        return None
    if schema == "control_status":
        if isinstance(value, Mapping):
            child_schema = "control_operation" if "control_id" in value else "control_status"
            return _safe_mapping(value, schema=child_schema, depth=depth)
        return None
    if schema == "session_id":
        try:
            return validate_session_id(value)
        except (TypeError, ValueError):
            return None
    if schema == "bool":
        return value if isinstance(value, bool) else None
    if schema == "int":
        return (
            value
            if isinstance(value, int)
            and not isinstance(value, bool)
            and abs(value) <= 10**12
            else None
        )

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) <= 10**12 else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, Mapping):
        if schema == "cleanup":
            schema = "result" if "ok" in value else "smoke_cleanup"
        if schema == "catalog":
            return _safe_mapping(value, schema="task_catalog", depth=depth)
        if schema is None:
            return {}
        return _safe_mapping(value, schema=schema, depth=depth)
    if isinstance(value, (list, tuple)):
        if schema == "catalog":
            return _safe_sequence(value, item_schema="string", depth=depth)
        item_schema = {
            "generic_list": None,
            "preflight_checks": "preflight_check",
            "result_list": "result",
            "string_list": "string",
            "task_descriptor_list": "task_descriptor",
            "task_provenance_list": "task_provenance",
            "frame_list": "frame",
            "log_items": "log_item",
            "timeline_events": "timeline_event",
            "smoke_capability_list": "smoke_capability",
            "smoke_issue_list": "smoke_issue",
            "smoke_assertion_list": "smoke_assertion",
            "smoke_evidence_ref_list": "smoke_evidence_ref",
            "smoke_field_list": "smoke_field",
            "control_transition_list": "control_transition",
        }.get(schema)
        if schema not in {
            "generic_list",
            "preflight_checks",
            "result_list",
            "string_list",
            "task_descriptor_list",
            "task_provenance_list",
            "frame_list",
            "log_items",
            "timeline_events",
            "smoke_capability_list",
            "smoke_issue_list",
            "smoke_assertion_list",
            "smoke_evidence_ref_list",
            "smoke_field_list",
            "control_transition_list",
        }:
            return []
        return _safe_sequence(value, item_schema=item_schema, depth=depth)
    return None


def serialize_dev_result(result: object) -> dict[str, object]:
    """Сериализовать только публичные поля DevResult через разрешённый список."""

    raw_ok = _field(result, "ok", False)
    raw_code = _field(result, "code", "DEV_MCP_INVALID_RESULT")
    raw_message = _field(result, "message", "Результат Dev Runtime имеет некорректную форму")
    raw_state = _field(result, "state", "failed")
    raw_session_id = _field(result, "session_id")
    raw_details = _field(result, "details", {})

    ok = raw_ok if isinstance(raw_ok, bool) else False
    code = _redact_text(raw_code) if isinstance(raw_code, str) else "DEV_MCP_INVALID_RESULT"
    message = (
        _redact_text(raw_message)
        if isinstance(raw_message, str)
        else "Результат Dev Runtime имеет некорректную форму"
    )
    state = _redact_text(raw_state) if isinstance(raw_state, str) else "failed"
    try:
        session_id = validate_session_id(raw_session_id) if raw_session_id is not None else None
    except (TypeError, ValueError):
        session_id = None
    details = _safe_value(raw_details, schema="details")
    if not isinstance(details, dict):
        details = {}
    return {
        "ok": ok,
        "code": code,
        "message": message,
        "state": state,
        "session_id": session_id,
        "details": details,
    }


def _input_error(tool_name: str) -> dict[str, object]:
    return {
        "ok": False,
        "code": "DEV_MCP_INPUT_INVALID",
        "message": "Входные аргументы Dev MCP не прошли строгую проверку",
        "state": "failed",
        "session_id": None,
        "details": {"tool": tool_name, "validation": "schema"},
    }


def _unknown_tool_error(tool_name: str) -> dict[str, object]:
    return {
        "ok": False,
        "code": "DEV_MCP_UNKNOWN_TOOL",
        "message": "Запрошенный инструмент Dev MCP не существует",
        "state": "failed",
        "session_id": None,
        "details": {"tool": _redact_text(tool_name)},
    }


def _internal_error() -> dict[str, object]:
    return {
        "ok": False,
        "code": "DEV_MCP_INTERNAL_ERROR",
        "message": "Внутренняя ошибка Dev MCP; подробности записаны в stderr",
        "state": "failed",
        "session_id": None,
        "details": {},
    }


class DevMcpAdapter:
    """Передавать MCP-инструменты target-bound Dev Runtime manager.

    Manager сохраняется между вызовами только пока repository-scoped target
    остаётся тем же. Уже созданные DevSession/evidence state не переносятся и
    продолжают разрешать собственный записанный profile через manager API.
    """

    def __init__(
        self,
        manager_factory: Callable[[], DevRuntimeManager] | None = None,
    ) -> None:
        self._manager_factory = manager_factory or _default_manager
        self._uses_default_manager = manager_factory is None
        self._manager: DevRuntimeManager | None = None
        self._manager_lock = threading.Lock()

    @staticmethod
    def _target_changed(manager: DevRuntimeManager) -> bool:
        environment = getattr(manager, "environment", None)
        if not isinstance(environment, DevEnvironment):
            # Синтетические manager implementations могут не владеть runtime
            # environment; для них сохраняется прежний factory contract.
            return False
        current = DevTargetRegistry.load_for_environment(
            environment.repository_root,
            fallback=environment.dev_target,
        )
        return current != environment.dev_target

    def _get_manager(self) -> DevRuntimeManager:
        manager = self._manager
        if manager is not None and not self._target_changed(manager):
            return manager
        with self._manager_lock:
            manager = self._manager
            if manager is None or self._target_changed(manager):
                manager = self._manager_factory()
                if self._target_changed(manager):
                    raise DevTargetError(
                        "DEV_TARGET_REBIND_FAILED",
                        "Новый Dev Runtime manager не соответствует назначенному development target",
                    )
                self._manager = manager
            return manager

    @staticmethod
    def _arguments(arguments: Mapping[str, object] | None) -> dict[str, object]:
        if arguments is None:
            return {}
        if not isinstance(arguments, Mapping):
            raise TypeError("arguments должен быть объектом")
        return dict(arguments)

    def _validated(
        self,
        tool_name: str,
        arguments: Mapping[str, object] | None,
    ) -> (
        _EmptyArguments
        | _TaskArguments
        | _StopArguments
        | _SessionArguments
        | _TimelineArguments
        | _LogsArguments
        | _SmokeIdArguments
        | _SmokeEvaluationArguments
        | _ControlIdArguments
        | SmokeSpec
        | None
    ):
        try:
            raw = self._arguments(arguments)
            if tool_name in _NO_ARGUMENT_TOOLS:
                return _EmptyArguments.model_validate(raw, strict=True)
            if tool_name in {"dev_validate_smoke", "dev_start_smoke"}:
                return SmokeSpec.model_validate(raw, strict=True)
            if tool_name in {"dev_get_smoke", "dev_cancel_smoke", "dev_get_smoke_evaluation"}:
                return _SmokeIdArguments.model_validate(raw, strict=True)
            if tool_name == "dev_submit_smoke_evaluation":
                return _SmokeEvaluationArguments.model_validate(raw, strict=True)
            if tool_name == "dev_get_control_operation":
                return _ControlIdArguments.model_validate(raw, strict=True)
            if tool_name in {"dev_plan_session", "dev_start_session"}:
                return _TaskArguments.model_validate(raw, strict=True)
            if tool_name == "dev_stop_session":
                return _StopArguments.model_validate(raw, strict=True)
            if tool_name == "dev_get_evidence":
                parsed = _SessionArguments.model_validate(raw, strict=True)
            elif tool_name == "dev_get_timeline":
                parsed = _TimelineArguments.model_validate(raw, strict=True)
            elif tool_name == "dev_get_logs":
                parsed = _LogsArguments.model_validate(raw, strict=True)
            else:
                return None
            if parsed.session_id is not None:
                validate_session_id(parsed.session_id)
            return parsed
        except (TypeError, ValueError, ValidationError):
            return None

    def call(
        self,
        tool_name: str,
        arguments: Mapping[str, object] | None = None,
    ) -> dict[str, object] | DevMcpResponse:
        """Выполнить разрешённый инструмент без динамического доступа и входных путей."""

        if tool_name not in DEV_MCP_TOOL_NAMES:
            return _unknown_tool_error(tool_name)
        parsed = self._validated(tool_name, arguments)
        if parsed is None:
            return _input_error(tool_name)
        if tool_name == "dev_get_contract":
            return serialize_dev_result(contract_result())

        try:
            if self._uses_default_manager:
                _ensure_legacy_logger_stderr()
            manager = self._get_manager()
            if tool_name == "dev_preflight":
                result = manager.preflight()
            elif tool_name == "dev_doctor":
                result = manager.doctor()
            elif tool_name == "dev_list_tasks":
                result = manager.list_tasks()
            elif tool_name == "dev_plan_session":
                assert isinstance(parsed, _TaskArguments)
                result = manager.plan(
                    root_tasks=parsed.root_tasks,
                    excluded_tasks=parsed.excluded_tasks,
                )
            elif tool_name == "dev_start_session":
                assert isinstance(parsed, _TaskArguments)
                result = manager.start(
                    root_tasks=parsed.root_tasks,
                    excluded_tasks=parsed.excluded_tasks,
                )
            elif tool_name == "dev_status":
                result = manager.status()
            elif tool_name == "dev_stop_session":
                assert isinstance(parsed, _StopArguments)
                result = manager.stop(preserve_task_state=parsed.preserve_task_state)
            elif tool_name == "dev_cleanup":
                result = manager.cleanup()
            elif tool_name == "dev_recover":
                result = manager.recover()
            elif tool_name == "dev_get_evidence":
                assert isinstance(parsed, _SessionArguments)
                result = manager.get_evidence(session_id=parsed.session_id)
            elif tool_name == "dev_get_timeline":
                assert isinstance(parsed, _TimelineArguments)
                result = manager.get_timeline(
                    session_id=parsed.session_id,
                    after_sequence=parsed.after_sequence,
                    limit=parsed.limit,
                )
            elif tool_name == "dev_get_logs":
                assert isinstance(parsed, _LogsArguments)
                result = manager.get_logs(
                    session_id=parsed.session_id,
                    cursor=parsed.cursor,
                    limit=parsed.limit,
                )
            elif tool_name == "dev_list_smoke_capabilities":
                result = manager.list_smoke_capabilities()
            elif tool_name == "dev_validate_smoke":
                assert isinstance(parsed, SmokeSpec)
                result = manager.validate_smoke(parsed)
            elif tool_name == "dev_start_smoke":
                assert isinstance(parsed, SmokeSpec)
                result = manager.start_smoke(parsed)
            elif tool_name == "dev_get_smoke":
                assert isinstance(parsed, _SmokeIdArguments)
                result = manager.get_smoke(parsed.smoke_id)
            elif tool_name == "dev_cancel_smoke":
                assert isinstance(parsed, _SmokeIdArguments)
                result = manager.cancel_smoke(parsed.smoke_id)
            elif tool_name == "dev_get_smoke_evaluation":
                assert isinstance(parsed, _SmokeIdArguments)
                screenshot = manager.get_smoke_evaluation(parsed.smoke_id)
                safe_result = serialize_dev_result(screenshot.result)
                if (
                    screenshot.image is not None
                    and screenshot.mime_type == "image/png"
                    and len(screenshot.image) > 0
                ):
                    return DevMcpResponse(safe_result, screenshot.image, screenshot.mime_type)
                return safe_result
            elif tool_name == "dev_submit_smoke_evaluation":
                assert isinstance(parsed, _SmokeEvaluationArguments)
                result = manager.submit_smoke_evaluation(
                    parsed.smoke_id,
                    parsed.assertion_id,
                    parsed.verdict,
                    parsed.rationale,
                )
            elif tool_name == "dev_get_runtime_status":
                result = manager.get_runtime_status()
            elif tool_name == "dev_start_game":
                result = manager.start_game()
            elif tool_name == "dev_stop_game":
                result = manager.stop_game()
            elif tool_name == "dev_restart_game":
                result = manager.restart_game()
            elif tool_name == "dev_start_emulator":
                result = manager.start_emulator()
            elif tool_name == "dev_stop_emulator":
                result = manager.stop_emulator()
            elif tool_name == "dev_restart_emulator":
                result = manager.restart_emulator()
            elif tool_name == "dev_restart_adb":
                result = manager.restart_adb()
            elif tool_name == "dev_get_control_operation":
                assert isinstance(parsed, _ControlIdArguments)
                result = manager.get_control_operation(parsed.control_id)
            else:
                assert tool_name == "dev_get_screenshot"
                screenshot = manager.get_screenshot()
                safe_result = serialize_dev_result(screenshot.result)
                if (
                    screenshot.image is not None
                    and screenshot.mime_type == "image/png"
                    and len(screenshot.image) > 0
                ):
                    return DevMcpResponse(safe_result, screenshot.image, screenshot.mime_type)
                return safe_result
        except DevTargetError as exc:
            return serialize_dev_result(
                {
                    "ok": False,
                    "code": exc.code,
                    "message": "Development target не настроен или не прошёл безопасную проверку",
                    "state": "failed",
                    "session_id": None,
                    "details": {"development_target": {"configured": False}},
                }
            )
        except Exception as exc:  # noqa: BLE001 — граница обязана очищать ошибки выполнения
            logger.error(
                "[Dev MCP] инструмент %s завершился неожиданной ошибкой: %s",
                tool_name,
                type(exc).__name__,
            )
            return _internal_error()
        return serialize_dev_result(result)


__all__ = [
    "DEV_MCP_TOOL_NAMES",
    "DevMcpAdapter",
    "DevMcpResponse",
    "serialize_dev_result",
]
