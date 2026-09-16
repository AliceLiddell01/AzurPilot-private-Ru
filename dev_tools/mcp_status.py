"""Read-only диагностика first-party MCP и прямых внешних интеграций.

Collector намеренно разделяет repository source, runtime contract и внешние
integration adapters. Он не запускает mutating tools, не сохраняет provider
payload и не считает недоступную внешнюю Codex session подтверждённой.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import tomllib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from azurpilot.integrations import IntegrationService
from azurpilot.tooling.process import safe_environment
from module.mcp_shared.catalog import tool_catalog_sha256_from_tools
from module.mcp_shared.versioning import (
    SOURCE_REVISION_ENV,
    UNKNOWN_SOURCE_REVISION,
    VersioningError,
    load_server_versions,
    version_satisfies,
)
from tools.paths import REPOSITORY_ROOT

STATUS_SCHEMA_VERSION = 1
STATUS_TIMEOUT_SECONDS = 20.0
REMOTE_TIMEOUT_SECONDS = 5.0
EXTERNAL_PROBE_TIMEOUT_SECONDS = 180.0
METRICS_TIMEOUT_SECONDS = 5.0
MCP_STATUS_WATCH_MIN_INTERVAL_SECONDS = 10.0
MCP_STATUS_WATCH_MAX_INTERVAL_SECONDS = 3600.0

SERVER_NAMES = ("azurpilot-dev", "azurpilot-game")
SERVER_MODULES = {
    "azurpilot-dev": ("module.dev_mcp", "dev_get_contract"),
    "azurpilot-game": ("module.game_mcp", "game_get_contract"),
}
CODEX_LOCAL_HTTP_REGISTRATION_KEYS = {
    "azurpilot-dev": "azurpilot_dev",
    "azurpilot-game": "azurpilot_game",
}
CODEX_LOCAL_HTTP_URLS = {
    "azurpilot-dev": "http://127.0.0.1:8775/mcp",
    "azurpilot-game": "http://127.0.0.1:8776/mcp",
}
CODEX_LOCAL_HTTP_TOKEN_ENV_VARS = {
    "azurpilot-dev": "AZURPILOT_DEV_LOCAL_MCP_TOKEN",
    "azurpilot-game": "AZURPILOT_GAME_LOCAL_MCP_TOKEN",
}
CODEX_SERVER_ARGS = {
    name: ("run", "--locked", "--no-sync", "python", "-m", module_name)
    for name, (module_name, _contract_tool) in SERVER_MODULES.items()
}
CODEX_SERVER_TIMEOUTS: dict[str, tuple[int, int]] = {
    "azurpilot-dev": (5, 180),
    "azurpilot-game": (10, 180),
}
PLUGIN_RELATIVE_ROOT = Path("plugins") / "azurpilot"
PLUGIN_REQUIRED_SKILLS = frozenset(
    {
        "azurpilot-development",
        "azurpilot-game-control",
        "azurpilot-troubleshooting",
    }
)
DIRECT_INTEGRATION_NAMES = (
    "coderabbit",
    "semgrep",
    "grafana",
    "context7",
    "docker-docs",
    "docker-hub",
)

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_VERSION_RE = re.compile(r"^v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_JSON_BYTES = 256 * 1024
_MAX_TOOLS = 256
_MAX_SKILL_BYTES = 64 * 1024


class StatusError(RuntimeError):
    """Bounded collector error represented by a stable machine code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _safe_type_name(value: object) -> str:
    name = type(value).__name__
    return name if _SAFE_IDENTIFIER.fullmatch(name) else "UnknownError"


def _safe_sha(value: object) -> str:
    if isinstance(value, str) and _SHA_RE.fullmatch(value.strip().lower()):
        return value.strip().lower()
    return UNKNOWN_SOURCE_REVISION


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _bounded_tool_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > _MAX_TOOLS:
        return ()
    names: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _SAFE_IDENTIFIER.fullmatch(item):
            return ()
        names.append(item)
    if len(names) != len(set(names)):
        return ()
    return tuple(names)


def _run_process(
    arguments: Sequence[str], *, timeout: float, command: str | None = None
) -> subprocess.CompletedProcess[str]:
    executable = command or shutil.which(arguments[0])
    if executable is None:
        raise StatusError("COMMAND_UNAVAILABLE")
    options: dict[str, object] = {
        "capture_output": True,
        "stdin": subprocess.DEVNULL,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.run([executable, *arguments[1:]], check=False, **options)
    except subprocess.TimeoutExpired as exc:
        raise StatusError("COMMAND_TIMEOUT") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise StatusError("COMMAND_FAILED") from exc


def _git_source_snapshot(root: Path) -> tuple[str, str]:
    """Получить только commit SHA и clean/modified state."""

    executable = shutil.which("git.exe") or shutil.which("git")
    if executable is None:
        return UNKNOWN_SOURCE_REVISION, "unknown"
    try:
        revision_result = _run_process(
            ("git", "-C", str(root), "rev-parse", "HEAD"),
            timeout=5,
            command=executable,
        )
        status_result = _run_process(
            ("git", "-C", str(root), "status", "--porcelain"),
            timeout=5,
            command=executable,
        )
    except StatusError:
        return UNKNOWN_SOURCE_REVISION, "unknown"
    revision = _safe_sha(revision_result.stdout)
    if revision == UNKNOWN_SOURCE_REVISION or status_result.returncode != 0:
        return revision, "unknown"
    return revision, "clean" if not status_result.stdout.strip() else "modified"


def _child_environment(revision: str | None = None) -> dict[str, str]:
    explicit = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if isinstance(revision, str) and _SHA_RE.fullmatch(revision):
        explicit[SOURCE_REVISION_ENV] = revision.lower()
    return safe_environment(explicit)


def _codex_skill_status(skill_path: Path, expected_name: str) -> dict[str, object]:
    if not skill_path.is_file():
        return {"status": "drift", "reason_code": "CODEX_PLUGIN_SKILL_FILE_MISSING"}
    try:
        raw = skill_path.read_bytes()
    except OSError:
        return {"status": "unavailable", "reason_code": "CODEX_PLUGIN_SKILL_UNAVAILABLE"}
    if len(raw) > _MAX_SKILL_BYTES:
        return {"status": "invalid", "reason_code": "CODEX_PLUGIN_SKILL_TOO_LARGE"}
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"status": "invalid", "reason_code": "CODEX_PLUGIN_SKILL_UTF8_INVALID"}
    match = re.match(r"\A---\r?\n(?P<body>.*?)\r?\n---(?:\r?\n|\Z)", content, re.DOTALL)
    if match is None:
        return {"status": "invalid", "reason_code": "CODEX_PLUGIN_SKILL_FRONTMATTER_INVALID"}
    try:
        import yaml
    except ImportError:
        return {"status": "unavailable", "reason_code": "CODEX_PLUGIN_SKILL_UNAVAILABLE"}
    try:
        metadata = yaml.safe_load(match.group("body"))
    except yaml.YAMLError:
        return {"status": "invalid", "reason_code": "CODEX_PLUGIN_SKILL_FRONTMATTER_INVALID"}
    if not isinstance(metadata, Mapping):
        return {"status": "invalid", "reason_code": "CODEX_PLUGIN_SKILL_FRONTMATTER_INVALID"}
    if metadata.get("name") != expected_name:
        return {"status": "drift", "reason_code": "CODEX_PLUGIN_SKILL_NAME_DRIFT"}
    if not isinstance(metadata.get("description"), str) or not metadata["description"].strip():
        return {"status": "invalid", "reason_code": "CODEX_PLUGIN_SKILL_DESCRIPTION_INVALID"}
    return {"status": "ready", "reason_code": "CODEX_PLUGIN_SKILL_VALID"}


def _codex_plugin_status(root: Path) -> dict[str, object]:
    plugin_root = root / PLUGIN_RELATIVE_ROOT
    manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
    try:
        raw = manifest_path.read_bytes()
        if len(raw) > _MAX_JSON_BYTES:
            return {"status": "invalid", "reason_code": "CODEX_PLUGIN_MANIFEST_TOO_LARGE"}
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"status": "unavailable", "reason_code": "CODEX_PLUGIN_MANIFEST_UNAVAILABLE"}
    if not isinstance(manifest, Mapping) or manifest.get("name") != "azurpilot":
        return {"status": "invalid", "reason_code": "CODEX_PLUGIN_MANIFEST_INVALID"}
    if manifest.get("skills") != "./skills/":
        return {"status": "drift", "reason_code": "CODEX_PLUGIN_SKILLS_DRIFT"}
    if "apps" in manifest or "mcpServers" in manifest:
        return {"status": "drift", "reason_code": "CODEX_PLUGIN_LEGACY_REGISTRATION_DECLARED"}
    if (plugin_root / ".app.json").exists() or (plugin_root / ".mcp.json").exists():
        return {"status": "drift", "reason_code": "CODEX_PLUGIN_LEGACY_FILE_DISCOVERABLE"}
    skills_root = plugin_root / "skills"
    try:
        skill_names = {item.name for item in skills_root.iterdir() if item.is_dir()}
    except OSError:
        return {"status": "unavailable", "reason_code": "CODEX_PLUGIN_SKILLS_UNAVAILABLE"}
    if skill_names != PLUGIN_REQUIRED_SKILLS:
        return {"status": "drift", "reason_code": "CODEX_PLUGIN_SKILLS_DRIFT"}
    for name in sorted(PLUGIN_REQUIRED_SKILLS):
        result = _codex_skill_status(skills_root / name / "SKILL.md", name)
        if result.get("status") != "ready":
            return {**result, "skill": name}
    version = manifest.get("version")
    return {
        "status": "ready",
        "reason_code": "CODEX_PLUGIN_ROUTING_READY",
        "plugin_version": version if isinstance(version, str) else "unknown",
        "mcp_registration": "project_config",
    }


def _load_codex_config(root: Path) -> Mapping[str, object]:
    path = root / ".codex" / "config.toml"
    try:
        raw = path.read_bytes()
        if len(raw) > _MAX_JSON_BYTES:
            raise StatusError("CODEX_SOURCE_CONFIG_TOO_LARGE")
        payload = tomllib.loads(raw.decode("utf-8"))
    except StatusError:
        raise
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise StatusError("CODEX_SOURCE_CONFIG_INVALID") from exc
    return payload if isinstance(payload, Mapping) else {}


def _codex_entry_status(
    config: Mapping[str, object],
    name: str,
    *,
    expected_command: str,
    expected_args: tuple[str, ...],
    expected_startup_timeout_sec: int,
    expected_tool_timeout_sec: int,
    expected_required: bool,
) -> dict[str, object]:
    servers = config.get("mcp_servers")
    entry = servers.get(name) if isinstance(servers, Mapping) else None
    expected = {
        "command": expected_command,
        "args": list(expected_args),
        "cwd": ".",
        "enabled": True,
        "required": expected_required,
        "startup_timeout_sec": expected_startup_timeout_sec,
        "tool_timeout_sec": expected_tool_timeout_sec,
    }
    if not isinstance(entry, Mapping) or any(entry.get(key) != value for key, value in expected.items()):
        return {"status": "drift", "reason_code": "CODEX_SOURCE_ENTRY_DRIFT"}
    return {"status": "configured", "reason_code": "CODEX_SOURCE_ENTRY_CONFIGURED"}


def _codex_url_entry_status(
    config: Mapping[str, object],
    name: str,
    *,
    expected_url: str,
    expected_bearer_token_env_var: str | None = None,
    expected_startup_timeout_sec: int = 20,
    expected_tool_timeout_sec: int = 120,
    expected_required: bool = False,
) -> dict[str, object]:
    servers = config.get("mcp_servers")
    entry = servers.get(name) if isinstance(servers, Mapping) else None
    if not isinstance(entry, Mapping):
        return {"status": "drift", "reason_code": "CODEX_SOURCE_ENTRY_DRIFT"}
    expected = {
        "url": expected_url,
        "enabled": True,
        "required": expected_required,
        "startup_timeout_sec": expected_startup_timeout_sec,
        "tool_timeout_sec": expected_tool_timeout_sec,
    }
    if expected_bearer_token_env_var is not None:
        expected["bearer_token_env_var"] = expected_bearer_token_env_var
    if any(entry.get(key) != value for key, value in expected.items()):
        return {"status": "drift", "reason_code": "CODEX_SOURCE_ENTRY_DRIFT"}
    return {"status": "configured", "reason_code": "CODEX_SOURCE_ENTRY_CONFIGURED"}


def _codex_source_summary(entries: Mapping[str, object]) -> dict[str, object]:
    statuses = [item.get("status") for item in entries.values() if isinstance(item, Mapping)]
    if statuses and all(status == "configured" for status in statuses):
        status, reason = "ready", "CODEX_SOURCE_CONFIG_READY"
    elif any(status in {"drift", "invalid"} for status in statuses):
        status, reason = "drift", "CODEX_SOURCE_CONFIG_DRIFT"
    else:
        status, reason = "partial", "CODEX_SOURCE_CONFIG_PARTIAL"
    return {
        "status": status,
        "reason_code": reason,
        "evidence_kind": "repository_source_config",
        "path": ".codex/config.toml",
        "servers": dict(entries),
    }


def _codex_effective_registration_status(name: str) -> dict[str, object]:
    return {
        "status": "not_observable",
        "reason_code": "CODEX_EFFECTIVE_REGISTRATION_NOT_OBSERVABLE",
        "evidence_kind": "external_live_codex_session",
        "server_name": name,
        "canonical_route": "direct_local_stdio",
        "transport": "stdio",
        "runtime_reachable": False,
        "runtime_ready": False,
    }


def first_party_source_registration(root: Path) -> dict[str, object]:
    """Собрать только tracked registration Dev/Game для общего tooling layer."""

    try:
        config = _load_codex_config(root)
    except StatusError as error:
        return {"status": "unavailable", "reason_code": error.code}
    servers: dict[str, dict[str, object]] = {}
    entries: dict[str, object] = {}
    for name in SERVER_NAMES:
        stdio = _codex_entry_status(
            config,
            name,
            expected_command="uv",
            expected_args=CODEX_SERVER_ARGS[name],
            expected_startup_timeout_sec=CODEX_SERVER_TIMEOUTS[name][0],
            expected_tool_timeout_sec=CODEX_SERVER_TIMEOUTS[name][1],
            expected_required=False,
        )
        stdio = {**stdio, "evidence_kind": "repository_source_config", "source_path": ".codex/config.toml"}
        local_name = CODEX_LOCAL_HTTP_REGISTRATION_KEYS[name]
        loopback = _codex_url_entry_status(
            config,
            local_name,
            expected_url=CODEX_LOCAL_HTTP_URLS[name],
            expected_bearer_token_env_var=CODEX_LOCAL_HTTP_TOKEN_ENV_VARS[name],
            expected_startup_timeout_sec=10,
            expected_tool_timeout_sec=180,
        )
        loopback = {
            **loopback,
            "evidence_kind": "repository_source_config",
            "source_path": ".codex/config.toml",
            "canonical_server_name": name,
            "registration_key": local_name,
        }
        server_summary = _codex_source_summary(
            {"stdio": stdio, "loopback_http": loopback}
        )
        servers[name] = {
            "status": server_summary["status"],
            "reason_code": server_summary["reason_code"],
            "source_config": stdio,
            "local_http_source_config": loopback,
        }
        entries[f"{name}.stdio"] = stdio
        entries[f"{name}.loopback_http"] = loopback
    result = _codex_source_summary(entries)
    result["servers"] = servers
    return result


async def _probe_local_stdio(
    server_name: str, *, root: Path, revision: str | None = None
) -> dict[str, object]:
    """Выполнить bounded initialize/tools/list/contract call first-party server."""

    from mcp.client import Client
    from mcp.client.stdio import StdioServerParameters

    executable = shutil.which("uv.exe") or shutil.which("uv")
    if executable is None:
        return {"status": "unavailable", "reason_code": "LOCAL_COMMAND_UNAVAILABLE"}
    module_name, contract_tool = SERVER_MODULES[server_name]
    parameters = StdioServerParameters(
        command=executable,
        args=["run", "--locked", "--no-sync", "python", "-m", module_name],
        cwd=root,
        env=_child_environment(revision),
    )
    try:
        async with Client(parameters, mode="auto", read_timeout_seconds=STATUS_TIMEOUT_SECONDS) as client:
            listed = await client.list_tools()
            contract_result = await client.call_tool(contract_tool, {})
            server_info = getattr(client, "server_info", None)
            observed_name = getattr(server_info, "name", None)
            observed_version = getattr(server_info, "version", None)
            protocol = getattr(client, "protocol_version", None)
    except TimeoutError:
        return {"status": "unavailable", "reason_code": "LOCAL_PROBE_TIMEOUT"}
    except Exception as error:  # noqa: BLE001 - bounded boundary.
        return {"status": "unavailable", "reason_code": "LOCAL_PROBE_FAILED", "error_type": _safe_type_name(error)}
    tool_items = getattr(listed, "tools", None)
    tool_names = _bounded_tool_names(
        [getattr(item, "name", None) for item in tool_items]
        if isinstance(tool_items, list)
        else None
    )
    structured = getattr(contract_result, "structured_content", None)
    details = structured.get("details") if isinstance(structured, Mapping) else None
    contract = details.get("contract") if isinstance(details, Mapping) else None
    try:
        catalog_hash = tool_catalog_sha256_from_tools(tool_items)
    except (TypeError, ValueError):
        catalog_hash = None
    if not isinstance(contract, Mapping):
        return {"status": "unavailable", "reason_code": "LOCAL_CONTRACT_PAYLOAD_INVALID"}
    if observed_name is None:
        observed_name = contract.get("server_name")
    if observed_version is None:
        observed_version = contract.get("server_version")
    capability_hash = contract.get("capability_catalog_sha256")
    contract_revision = contract.get("contract_revision")
    if (
        not isinstance(observed_name, str)
        or not isinstance(observed_version, str)
        or not _SAFE_IDENTIFIER.fullmatch(observed_name)
        or not _VERSION_RE.fullmatch(observed_version)
        or not isinstance(protocol, str)
        or not _SAFE_TOKEN.fullmatch(protocol)
        or not tool_names
        or contract.get("tool_count") != len(tool_names)
        or contract.get("tool_catalog_sha256") != catalog_hash
        or not isinstance(capability_hash, str)
        or not _SHA256_RE.fullmatch(capability_hash)
        or not isinstance(contract_revision, str)
        or not _SHA256_RE.fullmatch(contract_revision)
        or contract.get("server_name") != server_name
        or contract.get("server_version") != observed_version
    ):
        return {"status": "unavailable", "reason_code": "LOCAL_CONTRACT_IDENTITY_INVALID"}
    return {
        "status": "ready",
        "reason_code": "LOCAL_CONTRACT_READY",
        "evidence_kind": "representative_local_probe",
        "server_name": observed_name,
        "server_version": observed_version,
        "protocol_version": protocol,
        "contract_schema_version": contract.get("contract_schema_version"),
        "source_revision": _safe_sha(contract.get("source_revision")),
        "tool_count": len(tool_names),
        "tool_catalog_sha256": contract.get("tool_catalog_sha256"),
        "capability_catalog_sha256": capability_hash,
        "contract_revision": contract_revision,
        "runtime_reachable": True,
        "runtime_ready": True,
    }


def _surface_status(
    probe: Mapping[str, object],
    *,
    expected_version: str,
    expected_revision: str,
    working_tree: str,
) -> dict[str, object]:
    result = dict(probe)
    if probe.get("status") != "ready":
        return result
    observed_version = probe.get("server_version")
    try:
        version_status = (
            "compatible"
            if isinstance(observed_version, str)
            and version_satisfies(observed_version, f"={expected_version}")
            else "drift"
        )
    except ValueError:
        version_status = "drift"
    observed_revision = probe.get("source_revision")
    if probe.get("evidence_kind") == "representative_local_probe":
        source_status = "not_applicable"
    elif (
        expected_revision != UNKNOWN_SOURCE_REVISION
        and isinstance(observed_revision, str)
        and _SHA_RE.fullmatch(observed_revision)
    ):
        source_status = "aligned" if observed_revision == expected_revision else "drift"
    else:
        source_status = "unknown"
    if working_tree != "clean" and source_status in {"aligned", "not_applicable"}:
        source_status = "modified"
    runtime_ready = version_status == "compatible" and source_status in {"aligned", "not_applicable"} and working_tree == "clean"
    result["version_status"] = version_status
    result["source_status"] = source_status
    result["runtime_reachable"] = True
    result["runtime_ready"] = runtime_ready
    result["status"] = "ready" if runtime_ready else "partial" if version_status == "compatible" else "drift"
    if result["status"] == "drift":
        result["reason_code"] = "MCP_VERSION_OR_SOURCE_DRIFT"
    elif result["status"] == "partial":
        result["reason_code"] = "MCP_SOURCE_PROVENANCE_UNKNOWN"
    return result


def _version_guard(root: Path, expected_versions: Mapping[str, str]) -> dict[str, object]:
    try:
        from module.dev_mcp.contract import (
            contract_compatibility_issues,
            server_bundle_drift_issues,
        )
        from module.dev_mcp.contract import (
            contract_payload as dev_contract_payload,
        )
        from module.game_mcp.contract import contract_payload as game_contract_payload

        contracts = {
            "azurpilot-dev": dev_contract_payload(),
            "azurpilot-game": game_contract_payload(),
        }
        issues: list[str] = []
        for name, expected in expected_versions.items():
            actual = contracts.get(name)
            if not isinstance(actual, Mapping) or actual.get("server_name") != name:
                issues.append(f"{name}.identity")
            elif actual.get("server_version") != expected:
                issues.append(f"{name}.server_version")
        compatibility = json.loads(
            (root / "plugins" / "azurpilot" / "compatibility.json").read_text(encoding="utf-8")
        )
        for name, contract in contracts.items():
            issues.extend(f"{name}.{item}" for item in contract_compatibility_issues(compatibility, contract))
            issues.extend(f"{name}.{item}" for item in server_bundle_drift_issues(compatibility, contract))
    except (
        OSError,
        TypeError,
        ValueError,
        KeyError,
        AttributeError,
        ImportError,
        json.JSONDecodeError,
        VersioningError,
    ) as error:
        return {"status": "unavailable", "reason_code": "MCP_VERSION_GUARD_FAILED", "error_type": _safe_type_name(error)}
    if issues:
        return {"status": "drift", "reason_code": "MCP_VERSION_GUARD_DRIFT", "issues": sorted(set(issues))}
    return {"status": "ready", "reason_code": "MCP_VERSION_GUARD_READY"}


def _not_configured_remote(name: str) -> dict[str, object]:
    return {
        "status": "not_configured",
        "reason_code": "REMOTE_PUBLIC_ROUTE_NOT_CONFIGURED",
        "server_name": name,
        "runtime_reachable": False,
        "runtime_ready": False,
    }


LocalProbe = Callable[[str, Path, str], Awaitable[dict[str, object]]]
RemoteProbe = Callable[[str], Awaitable[dict[str, object]]]


def _integration_payload(result: object) -> dict[str, object]:
    details = getattr(result, "details", None)
    records = getattr(details, "integrations", ()) if details is not None else ()
    payload: dict[str, object] = {}
    for record in records:
        data = record.model_dump(mode="json") if hasattr(record, "model_dump") else {}
        name = data.get("name")
        if isinstance(name, str):
            data["state"] = str(data.get("state", "UNKNOWN")).lower()
            payload[name] = data
    return payload


async def collect_status_async(
    root: Path | str | None = None,
    *,
    local_probe: LocalProbe | None = None,
    remote_probe: RemoteProbe | None = None,
    integration_service: IntegrationService | None = None,
    now: Callable[[], str] | None = None,
) -> dict[str, object]:
    """Собрать bounded status без profile или implicit route assumptions."""

    repository_root = (Path(root) if root is not None else REPOSITORY_ROOT).resolve()
    generated_at = now() if now is not None else _utc_now()
    revision, working_tree = _git_source_snapshot(repository_root)
    try:
        expected_versions = load_server_versions(repository_root)
    except (OSError, ValueError, VersioningError):
        expected_versions = {}
    source_config = first_party_source_registration(repository_root)
    plugin = _codex_plugin_status(repository_root)
    version_guard = _version_guard(repository_root, expected_versions)
    effective = {
        name: _codex_effective_registration_status(name) for name in SERVER_NAMES
    }
    effective_summary = {
        "status": "not_observable",
        "reason_code": "CODEX_EFFECTIVE_REGISTRATION_NOT_OBSERVABLE",
        "evidence_kind": "external_live_codex_session",
        "servers": effective,
    }
    local_runner = local_probe or (
        lambda name, item_root, item_revision: _probe_local_stdio(
            name, root=item_root, revision=item_revision
        )
    )
    remote_runner = remote_probe or (
        lambda name: asyncio.sleep(0, result=_not_configured_remote(name))
    )
    server_items: dict[str, dict[str, object]] = {}
    local_results = await asyncio.gather(
        *(local_runner(name, repository_root, revision) for name in SERVER_NAMES),
        return_exceptions=True,
    )
    remote_results = await asyncio.gather(
        *(remote_runner(name) for name in SERVER_NAMES), return_exceptions=True
    )
    for name, local_result, remote_result in zip(
        SERVER_NAMES, local_results, remote_results, strict=True
    ):
        expected_version = expected_versions.get(name, "unknown")
        local = (
            {
                "status": "unavailable",
                "reason_code": "LOCAL_PROBE_FAILED",
                "error_type": _safe_type_name(local_result),
            }
            if isinstance(local_result, BaseException)
            else local_result
        )
        remote = (
            {
                "status": "unavailable",
                "reason_code": "REMOTE_PROBE_FAILED",
                "error_type": _safe_type_name(remote_result),
            }
            if isinstance(remote_result, BaseException)
            else remote_result
        )
        local_mapping = (
            local
            if isinstance(local, Mapping)
            else {"status": "unavailable", "reason_code": "LOCAL_PROBE_PAYLOAD_INVALID"}
        )
        remote_mapping = (
            remote if isinstance(remote, Mapping) else _not_configured_remote(name)
        )
        server_source = source_config.get("servers")
        server_items[name] = {
            "expected_version": expected_version,
            "local_direct": _surface_status(
                local_mapping,
                expected_version=expected_version,
                expected_revision=revision,
                working_tree=working_tree,
            ),
            "codex": {
                "source_config": server_source.get(name, {}) if isinstance(server_source, Mapping) else {},
                "effective_codex_registration": effective[name],
            },
            "remote_backend": dict(remote_mapping),
        }
    service = integration_service or IntegrationService()
    try:
        integration_result = await asyncio.wait_for(
            service.doctor_async(repository_root), timeout=EXTERNAL_PROBE_TIMEOUT_SECONDS
        )
        integrations = _integration_payload(integration_result)
        integration_probe_status = "ready"
    except TimeoutError:
        integrations = {
            name: {
                "name": name,
                "state": "unavailable",
                "reason_code": "EXTERNAL_PROBE_TIMEOUT",
            }
            for name in DIRECT_INTEGRATION_NAMES
        }
        integration_probe_status = "unavailable"
    except Exception as error:  # noqa: BLE001 - status boundary.
        integrations = {
            name: {
                "name": name,
                "state": "unknown",
                "reason_code": "EXTERNAL_PROBE_FAILED",
                "error_type": _safe_type_name(error),
            }
            for name in DIRECT_INTEGRATION_NAMES
        }
        integration_probe_status = "unknown"
    for name in DIRECT_INTEGRATION_NAMES:
        integrations.setdefault(
            name,
            {
                "name": name,
                "state": "unknown",
                "reason_code": "EXTERNAL_RECORD_MISSING",
            },
        )
    canonical_status = (
        "ready"
        if source_config.get("status") == "ready"
        and plugin.get("status") == "ready"
        and version_guard.get("status") == "ready"
        and working_tree == "clean"
        and all(
            isinstance(item.get("local_direct"), Mapping)
            and item["local_direct"].get("status") == "ready"
            for item in server_items.values()
        )
        else "partial"
    )
    external_ready = all(
        isinstance(item, Mapping) and item.get("state") == "ready"
        for item in integrations.values()
    )
    overall_status = "ready" if canonical_status == "ready" and external_ready else "partial"
    if version_guard.get("status") == "drift" or source_config.get("status") == "drift" or plugin.get("status") == "drift":
        overall_status = "drift"
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": overall_status,
        "reason_code": "MCP_STATUS_READY" if overall_status == "ready" else "MCP_STATUS_DRIFT" if overall_status == "drift" else "MCP_STATUS_PARTIAL",
        "canonical_status": canonical_status,
        "canonical_reason_code": "MCP_CANONICAL_SOURCE_READY" if canonical_status == "ready" else "MCP_CANONICAL_SOURCE_PARTIAL",
        "source": {"revision": revision, "working_tree": working_tree},
        "source_config": source_config,
        "effective_codex_registration": effective_summary,
        "plugin": plugin,
        "servers": server_items,
        "version_guard": version_guard,
        "integrations": integrations,
        "probe": {"status": integration_probe_status, "generated_at": generated_at},
    }


def collect_status(root: Path | str | None = None, **kwargs: object) -> dict[str, object]:
    return asyncio.run(collect_status_async(root, **kwargs))


@dataclass(frozen=True, slots=True)
class MetricSample:
    name: str
    value: float
    attributes: Mapping[str, str]


_METRIC_ATTRIBUTE_NAMES = frozenset(
    {"server", "surface", "version", "protocol", "required_runtime"}
)
_METRIC_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


def _surface_samples(
    *,
    server: str,
    surface: str,
    value: Mapping[str, object],
    required: bool,
    probe_timestamp: float | None = None,
) -> list[MetricSample]:
    status = value.get("status", value.get("state"))
    reachable = value.get("runtime_reachable") is True or value.get("reachable") is True
    ready = value.get("runtime_ready") is True or status in {"ready", "configured"}
    evidence = value.get("evidence")
    evidence_configured = (
        evidence.get("configured") if isinstance(evidence, Mapping) else None
    )
    if isinstance(evidence_configured, bool):
        configured = evidence_configured
    else:
        configured = status in {"ready", "configured", "partial", "drift", "degraded"}
    protocol = str(value.get("protocol_version", "unknown"))
    observed_version = value.get("server_version")
    version = (
        observed_version
        if isinstance(observed_version, str) and _SAFE_TOKEN.fullmatch(observed_version)
        else "unknown"
    )
    attributes = {
        "server": server,
        "surface": surface,
        "version": version,
        "protocol": protocol if _SAFE_TOKEN.fullmatch(protocol) else "unknown",
        "required_runtime": "1" if required else "0",
    }
    samples = [
        MetricSample("azurpilot_mcp_surface_configured", float(configured), attributes),
        MetricSample("azurpilot_mcp_endpoint_up", float(reachable), attributes),
        MetricSample("azurpilot_mcp_surface_reachable", float(reachable), attributes),
        MetricSample("azurpilot_mcp_surface_runtime_ready", float(ready), attributes),
    ]
    version_status = value.get("version_status")
    if version_status in {"compatible", "drift"}:
        samples.append(
            MetricSample(
                "azurpilot_mcp_version_drift",
                1.0 if version_status == "drift" else 0.0,
                attributes,
            )
        )
    if version != "unknown":
        samples.append(
            MetricSample("azurpilot_mcp_observed_version_info", 1.0, attributes)
        )
    if ready and probe_timestamp is not None:
        samples.append(
            MetricSample(
                "azurpilot_mcp_last_successful_probe_timestamp_seconds",
                probe_timestamp,
                attributes,
            )
        )
    return samples


def status_metric_samples(report: Mapping[str, object]) -> tuple[MetricSample, ...]:
    samples: list[MetricSample] = []
    probe_timestamp: float | None = None
    generated_at = report.get("generated_at")
    if isinstance(generated_at, str):
        try:
            probe_timestamp = datetime.fromisoformat(generated_at).timestamp()
        except ValueError:
            probe_timestamp = None
    servers = report.get("servers")
    if isinstance(servers, Mapping):
        for name, item in servers.items():
            if not isinstance(name, str) or not _SAFE_IDENTIFIER.fullmatch(name) or not isinstance(item, Mapping):
                continue
            local = item.get("local_direct")
            if isinstance(local, Mapping):
                samples.extend(
                    _surface_samples(
                        server=name,
                        surface="local_direct",
                        value=local,
                        required=True,
                        probe_timestamp=probe_timestamp,
                    )
                )
            codex = item.get("codex")
            if isinstance(codex, Mapping):
                source = codex.get("source_config")
                effective = codex.get("effective_codex_registration")
                if isinstance(source, Mapping):
                    samples.extend(
                        _surface_samples(
                            server=name,
                            surface="codex_source",
                            value=source,
                            required=True,
                            probe_timestamp=probe_timestamp,
                        )
                    )
                if isinstance(effective, Mapping):
                    samples.extend(
                        _surface_samples(
                            server=name,
                            surface="codex_effective",
                            value=effective,
                            required=True,
                            probe_timestamp=probe_timestamp,
                        )
                    )
    integrations = report.get("integrations")
    if isinstance(integrations, Mapping):
        for name, value in integrations.items():
            if isinstance(name, str) and name in DIRECT_INTEGRATION_NAMES and isinstance(value, Mapping):
                samples.extend(
                    _surface_samples(
                        server=name,
                        surface="external_direct",
                        value=value,
                        required=True,
                        probe_timestamp=probe_timestamp,
                    )
                )
    for sample in samples:
        if not _METRIC_NAME_RE.fullmatch(sample.name) or set(sample.attributes) - _METRIC_ATTRIBUTE_NAMES:
            raise StatusError("MCP_METRIC_ATTRIBUTES_INVALID")
    return tuple(samples)


def _metrics_endpoint() -> str | None:
    metrics_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "").strip()
    if metrics_endpoint:
        return metrics_endpoint
    for variable in ("AZURPILOT_OBSERVABILITY_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        endpoint = os.environ.get(variable, "").strip()
        if endpoint:
            return endpoint.rstrip("/") + "/v1/metrics"
    return None


@dataclass(frozen=True, slots=True)
class MetricEmission:
    emitted: bool
    reason_code: str
    sample_count: int


def emit_metrics(report: Mapping[str, object]) -> MetricEmission:
    try:
        samples = status_metric_samples(report)
    except StatusError as error:
        return MetricEmission(False, error.code, 0)
    endpoint = _metrics_endpoint()
    if not endpoint:
        return MetricEmission(False, "MCP_METRICS_ENDPOINT_UNCONFIGURED", len(samples))
    try:
        from module.observability.metrics import emit_metric_samples_once

        emitted = emit_metric_samples_once(
            samples,
            endpoint=endpoint,
            timeout_millis=int(METRICS_TIMEOUT_SECONDS * 1000),
            repository_root=REPOSITORY_ROOT,
        )
    except Exception:  # noqa: BLE001 - status command must stay bounded.
        return MetricEmission(False, "MCP_METRICS_EXPORT_FAILED", len(samples))
    return MetricEmission(
        emitted,
        "MCP_METRICS_EXPORTED" if emitted else "MCP_METRICS_EXPORT_FAILED",
        len(samples),
    )


def _strict_failure(report: Mapping[str, object], emission: MetricEmission | None) -> bool:
    if report.get("status") != "ready" or report.get("canonical_status") != "ready":
        return True
    source = report.get("source")
    if not isinstance(source, Mapping) or source.get("working_tree") != "clean":
        return True
    for key in ("source_config", "plugin", "version_guard"):
        item = report.get(key)
        if not isinstance(item, Mapping) or item.get("status") != "ready":
            return True
    effective = report.get("effective_codex_registration")
    if not isinstance(effective, Mapping) or effective.get("status") not in {
        "ready",
        "not_observable",
    }:
        return True
    servers = report.get("servers")
    if not isinstance(servers, Mapping):
        return True
    for name in SERVER_NAMES:
        item = servers.get(name)
        if not isinstance(item, Mapping):
            return True
        local = item.get("local_direct")
        if not isinstance(local, Mapping) or local.get("status") != "ready":
            return True
    integrations = report.get("integrations")
    if not isinstance(integrations, Mapping) or set(integrations) != set(DIRECT_INTEGRATION_NAMES):
        return True
    if any(not isinstance(item, Mapping) or item.get("state") != "ready" for item in integrations.values()):
        return True
    return emission is not None and not emission.emitted


_HUMAN_STATUS_LABELS = {
    "ready": "OK",
    "configured": "OK",
    "partial": "PARTIAL",
    "drift": "DRIFT",
    "not_configured": "NOT CONFIGURED",
    "not_observable": "UNKNOWN",
    "unavailable": "UNAVAILABLE",
    "unknown": "UNKNOWN",
}


def _human_status_label(value: object) -> str:
    if not isinstance(value, str):
        return "UNKNOWN"
    return _HUMAN_STATUS_LABELS.get(value, value.upper().replace("_", " "))


def _human_reason(value: object) -> str:
    return value if isinstance(value, str) and _SAFE_TOKEN.fullmatch(value) else "UNKNOWN"


def _print_human_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def _print_human_notes(notes: Sequence[str]) -> None:
    if notes:
        print()
        print("Notes")
        print("-----")
        for note in notes:
            print(f"- {note}")


def _print_human(report: Mapping[str, object], emission: MetricEmission | None) -> None:
    print("Статус MCP AzurPilot")
    print("====================")
    print(f"OVERALL  {_human_status_label(report.get('status'))} ({_human_reason(report.get('reason_code'))})")
    source = report.get("source")
    if isinstance(source, Mapping):
        revision = source.get("revision", UNKNOWN_SOURCE_REVISION)
        shown_revision = revision[:12] if isinstance(revision, str) and revision != UNKNOWN_SOURCE_REVISION else UNKNOWN_SOURCE_REVISION
        print(f"SOURCE   {shown_revision} ({source.get('working_tree', 'unknown')})")
    for label, key in (("PLUGIN", "plugin"), ("SOURCE CONFIG", "source_config"), ("CODEX SESSION", "effective_codex_registration"), ("VERSION", "version_guard")):
        value = report.get(key)
        if isinstance(value, Mapping):
            print(f"{label:14} {_human_status_label(value.get('status'))} ({_human_reason(value.get('reason_code'))})")
    rows: list[list[str]] = []
    notes: list[str] = []
    servers = report.get("servers")
    if isinstance(servers, Mapping):
        for name in SERVER_NAMES:
            item = servers.get(name)
            if not isinstance(item, Mapping):
                continue
            local = item.get("local_direct")
            remote = item.get("remote_backend")
            local_status = local.get("status") if isinstance(local, Mapping) else "unknown"
            remote_status = remote.get("status") if isinstance(remote, Mapping) else "unknown"
            rows.append([name, str(item.get("expected_version", "unknown")), _human_status_label(local_status), _human_status_label(remote_status)])
            if local_status != "ready":
                notes.append(f"{name} local/direct: {_human_status_label(local_status)} ({_human_reason(local.get('reason_code') if isinstance(local, Mapping) else None)})")
    print()
    _print_human_table(("СЕРВЕР", "ОЖИДАЕМАЯ ВЕРСИЯ", "ЛОКАЛЬНО", "REMOTE"), rows)
    integrations = report.get("integrations")
    if isinstance(integrations, Mapping):
        print()
        print("Внешние интеграции")
        print("------------------")
        integration_rows = []
        for name in DIRECT_INTEGRATION_NAMES:
            item = integrations.get(name)
            if not isinstance(item, Mapping):
                continue
            state = item.get("state")
            evidence = item.get("evidence")
            configured = evidence.get("configured") if isinstance(evidence, Mapping) else None
            integration_rows.append([name, _human_status_label(state), "ДА" if configured else "НЕТ", _human_reason(item.get("reason_code"))])
            if state != "ready":
                notes.append(f"{name}: {_human_status_label(state)} ({_human_reason(item.get('reason_code'))})")
        _print_human_table(("ИНТЕГРАЦИЯ", "СОСТОЯНИЕ", "НАСТРОЕНО", "ПРИЧИНА"), integration_rows)
    if emission is not None:
        print()
        print(f"METRICS  {_human_status_label('ready' if emission.emitted else 'unavailable')} ({_human_reason(emission.reason_code)}) samples={emission.sample_count}")
    _print_human_notes(notes)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only status и bounded drift check MCP surfaces AzurPilot.")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Вывести bounded JSON.")
    parser.add_argument("--strict", action="store_true", help="Вернуть ненулевой код при неполной проверке.")
    parser.add_argument("--emit-metrics", action="store_true", help="Однократно отправить status metrics через OTel.")
    parser.add_argument("--watch", action="store_true", help="Повторять bounded status probe.")
    parser.add_argument("--interval-seconds", type=float, default=60.0, help="Интервал --watch в секундах (10..3600).")
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if not MCP_STATUS_WATCH_MIN_INTERVAL_SECONDS <= arguments.interval_seconds <= MCP_STATUS_WATCH_MAX_INTERVAL_SECONDS:
        print(f"Интервал --watch должен быть от {MCP_STATUS_WATCH_MIN_INTERVAL_SECONDS:g} до {MCP_STATUS_WATCH_MAX_INTERVAL_SECONDS:g} секунд.")
        return 2

    def run_once() -> tuple[dict[str, object], MetricEmission | None]:
        report = collect_status(arguments.repository_root)
        emission = emit_metrics(report) if arguments.emit_metrics else None
        if emission is not None:
            report = {**report, "metrics": {"emitted": emission.emitted, "reason_code": emission.reason_code, "sample_count": emission.sample_count}}
        return report, emission

    if not arguments.watch:
        report, emission = run_once()
        if arguments.as_json:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        else:
            _print_human(report, emission)
        return 2 if arguments.strict and _strict_failure(report, emission) else 0
    try:
        while True:
            report, emission = run_once()
            if arguments.as_json:
                print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")), flush=True)
            else:
                _print_human(report, emission)
            if arguments.strict and _strict_failure(report, emission):
                return 2
            time.sleep(arguments.interval_seconds)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DIRECT_INTEGRATION_NAMES",
    "SERVER_NAMES",
    "STATUS_SCHEMA_VERSION",
    "MetricEmission",
    "MetricSample",
    "StatusError",
    "_child_environment",
    "_codex_entry_status",
    "_codex_url_entry_status",
    "_print_human",
    "_strict_failure",
    "collect_status",
    "collect_status_async",
    "emit_metrics",
    "first_party_source_registration",
    "main",
    "status_metric_samples",
]
