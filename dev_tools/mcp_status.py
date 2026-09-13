"""Read-only диагностика MCP surfaces и canonical Docker MCP profile.

Команда намеренно не вызывает mutating MCP tools, не печатает окружение и не
сохраняет ответы внешних endpoint-ов. Docker Toolkit используется только для
bounded version/profile/catalog checks и read-only tool probes.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from module.mcp_shared.versioning import (
    UNKNOWN_SOURCE_REVISION,
    VersioningError,
    load_server_versions,
    version_satisfies,
)

STATUS_SCHEMA_VERSION = 1
STATUS_TIMEOUT_SECONDS = 20.0
REMOTE_TIMEOUT_SECONDS = 5.0
DOCKER_PROBE_TIMEOUT_SECONDS = 90.0
DOCKER_COMMAND_TIMEOUT_SECONDS = 15.0
METRICS_TIMEOUT_SECONDS = 5.0
SEMGREP_PROBE_TIMEOUT_SECONDS = 20.0
SEMGREP_PROBE_TOTAL_TIMEOUT_SECONDS = 3 * SEMGREP_PROBE_TIMEOUT_SECONDS + 5.0
MCP_STATUS_WATCH_MIN_INTERVAL_SECONDS = 10.0
MCP_STATUS_WATCH_MAX_INTERVAL_SECONDS = 3600.0
CANONICAL_DOCKER_PROFILE_ID = "azurpilot-development"
CANONICAL_DOCKER_PROFILE_NAME = "AzurPilot Development"
CANONICAL_GRAFANA_PROFILE_URL = "http://host.docker.internal:3000"
DOCKER_CLIENT_PROFILE_NAME = "MCP_DOCKER"
DOCKER_CLIENT_COMMAND = "docker"
DOCKER_CLIENT_ARGS = ("mcp", "gateway", "run", "--profile", CANONICAL_DOCKER_PROFILE_ID)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_VERSION_RE = re.compile(r"^v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
_URL_SCHEMES = frozenset({"https"})
_MAX_JSON_BYTES = 256 * 1024
_MAX_TOOLS = 256

SERVER_NAMES = ("azurpilot-dev", "azurpilot-game")
SERVER_MODULES = {
    "azurpilot-dev": ("module.dev_mcp", "dev_get_contract"),
    "azurpilot-game": ("module.game_mcp", "game_get_contract"),
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
THIRD_PARTY_SERVERS = (
    "grafana",
    "context7",
    "docker-docs",
    "dockerhub",
    "semgrep",
)
MCP_ROUTE_POLICY: dict[str, dict[str, object]] = {
    "azurpilot-dev": {
        "canonical_route": "direct_local_stdio",
        "gateway_required": False,
        "collector_required": True,
        "required_runtime": True,
    },
    "azurpilot-game": {
        "canonical_route": "direct_local_stdio",
        "gateway_required": False,
        "collector_required": True,
        "required_runtime": True,
    },
    "context7": {
        "canonical_route": "direct_codex",
        "gateway_required": False,
        "collector_required": False,
        "required_runtime": False,
        "live_acceptance_required": True,
        "user_scoped": True,
    },
    "docker-docs": {
        "canonical_route": "direct_mcp",
        "gateway_required": False,
        "collector_required": True,
        "required_runtime": True,
    },
    "semgrep": {
        "canonical_route": "direct_local_stdio",
        "gateway_required": False,
        "collector_required": True,
        "required_runtime": True,
    },
    "grafana": {
        "canonical_route": "docker_gateway",
        "gateway_required": True,
        "collector_required": True,
        "required_runtime": True,
        "credential_evidence": "end_to_end_call",
    },
    "dockerhub": {
        "canonical_route": "docker_gateway",
        "gateway_required": True,
        "collector_required": True,
        "required_runtime": True,
        "credential_evidence": "not_required_for_public_probe",
    },
}
DIRECT_THIRD_PARTY_SERVERS = tuple(
    name
    for name in THIRD_PARTY_SERVERS
    if MCP_ROUTE_POLICY[name].get("gateway_required") is not True
)
GATEWAY_REQUIRED_SERVERS = tuple(
    name
    for name in THIRD_PARTY_SERVERS
    if MCP_ROUTE_POLICY[name].get("gateway_required") is True
)
DOCKERHUB_READ_ONLY_TOOLS = frozenset(
    {
        "checkRepository",
        "checkRepositoryTag",
        "dockerHardenedImages",
        "getPersonalNamespace",
        "getRepositoryInfo",
        "getRepositoryTag",
        "listAllNamespacesMemberOf",
        "listNamespaces",
        "listRepositoriesByNamespace",
        "listRepositoryTags",
        "search",
    }
)
GRAFANA_READ_ONLY_TOOLS = frozenset(
    {
        "check_datasources_health",
        "get_dashboard_panel_queries",
        "get_dashboard_property",
        "get_dashboard_summary",
        "get_datasource",
        "list_datasources",
        "list_loki_label_names",
        "list_loki_label_values",
        "list_prometheus_label_names",
        "list_prometheus_label_values",
        "list_prometheus_metric_metadata",
        "list_prometheus_metric_names",
        "query_loki_logs",
        "query_prometheus",
        "query_prometheus_histogram",
        "search_dashboards",
        "generate_deeplink",
    }
)
SEMGREP_READ_ONLY_TOOLS = frozenset(
    {
        "semgrep_rule_schema",
        "get_supported_languages",
        "semgrep_findings",
        "semgrep_scan_with_custom_rule",
        "semgrep_scan",
        "semgrep_scan_local",
        "security_check",
        "get_abstract_syntax_tree",
    }
)
SEMGREP_LOCAL_SCAN_TOOLS = frozenset(
    {"semgrep_scan", "semgrep_scan_local", "semgrep_scan_with_custom_rule"}
)
SEMGREP_CLOUD_TOOLS = frozenset({"security_check", "semgrep_findings"})
DOCKER_RUNTIME_PROBE_TOOLS = {
    "grafana": ("list_datasources",),
    "dockerhub": ("listNamespaces",),
    "context7": ("resolve-library-id", "resolve_library_id"),
    "docker-docs": ("search", "search_docker_docs"),
    "semgrep": ("get_supported_languages",),
}
DOCKER_RUNTIME_PROBE_ARGUMENTS: dict[str, dict[str, str]] = {
    "context7": {"libraryName": "python"},
    "docker-docs": {"query": "Docker MCP"},
}
_KNOWN_WRITE_TOOLS = frozenset(
    {
        "alerting_manage_routing",
        "alerting_manage_rules",
        "createRepository",
        "create_annotation",
        "create_datasource",
        "create_folder",
        "create_incident",
        "create_snapshot",
        "delete_snapshot",
        "install_plugin",
        "update_annotation",
        "update_dashboard",
        "update_datasource",
        "updateRepositoryInfo",
    }
)
_PROFILE_SERVER_SET = frozenset(THIRD_PARTY_SERVERS)
_EXPECTED_DOCKER_SERVER_TYPES = {
    "grafana": "image",
    "dockerhub": "image",
    "context7": "remote",
    "docker-docs": "remote",
    "semgrep": "remote",
}
_EXPECTED_DOCKER_REMOTE_URLS = {
    "context7": "https://mcp.context7.com/mcp",
    "docker-docs": "https://mcp-docs.docker.com/mcp",
    "semgrep": "https://mcp.semgrep.ai/mcp",
}
_REQUIRED_TOOL_SETS = {
    "grafana": GRAFANA_READ_ONLY_TOOLS,
    "dockerhub": DOCKERHUB_READ_ONLY_TOOLS,
    "semgrep": SEMGREP_READ_ONLY_TOOLS,
}
_FORBIDDEN_PROFILE_KEYS = frozenset(
    {"volumes", "ports", "docker_socket", "host_filesystem", "privileged"}
)


def _route_policy(server_name: str) -> dict[str, object]:
    """Вернуть неизменяемую для вызывающего кода копию route policy."""

    return dict(MCP_ROUTE_POLICY.get(server_name, {}))


_REQUIRED_PROFILE_ERRORS = frozenset(
    {
        "DOCKER_PROFILE_ID_INVALID",
        "DOCKER_PROFILE_SERVER_COUNT_INVALID",
        "DOCKER_PROFILE_SERVER_INVALID",
        "DOCKER_PROFILE_SERVER_ID_INVALID",
        "DOCKER_PROFILE_SERVER_SET_INVALID",
        "DOCKER_PROFILE_SERVER_TYPE_INVALID",
        "DOCKER_PROFILE_IMAGE_NOT_PINNED",
        "DOCKER_PROFILE_HOST_ACCESS_INVALID",
        "DOCKER_PROFILE_SNAPSHOT_HOST_ACCESS_INVALID",
        "DOCKER_PROFILE_WRITE_TOOL_EXPOSED",
        "DOCKER_PROFILE_GRAFANA_CONFIG_INVALID",
        "DOCKER_PROFILE_GRAFANA_WRITE_MODE_INVALID",
        "DOCKER_PROFILE_SECRET_PROVIDER_INVALID",
    }
)


class StatusError(RuntimeError):
    """Безопасная ошибка status collector с machine-readable кодом."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _safe_type_name(value: object) -> str:
    name = type(value).__name__
    return name if _SAFE_IDENTIFIER.fullmatch(name) else "UnknownError"


def _codex_plugin_status(root: Path) -> dict[str, object]:
    """Проверить routing metadata plugin без загрузки Connected App state."""

    plugin_root = root / PLUGIN_RELATIVE_ROOT
    manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
    try:
        raw_manifest = manifest_path.read_bytes()
    except OSError:
        return {
            "status": "unavailable",
            "reason_code": "CODEX_PLUGIN_MANIFEST_UNAVAILABLE",
        }
    if len(raw_manifest) > _MAX_JSON_BYTES:
        return {
            "status": "invalid",
            "reason_code": "CODEX_PLUGIN_MANIFEST_TOO_LARGE",
        }
    try:
        manifest = json.loads(raw_manifest.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {
            "status": "invalid",
            "reason_code": "CODEX_PLUGIN_MANIFEST_INVALID",
        }
    if not isinstance(manifest, Mapping) or manifest.get("name") != "azurpilot":
        return {
            "status": "invalid",
            "reason_code": "CODEX_PLUGIN_MANIFEST_INVALID",
        }
    if manifest.get("skills") != "./skills/":
        return {
            "status": "drift",
            "reason_code": "CODEX_PLUGIN_SKILLS_DRIFT",
        }
    if "apps" in manifest:
        return {
            "status": "drift",
            "reason_code": "CODEX_PLUGIN_LEGACY_APP_DECLARED",
        }
    if "mcpServers" in manifest:
        return {
            "status": "drift",
            "reason_code": "CODEX_PLUGIN_MCP_REGISTRATION_DUPLICATE",
        }
    if (plugin_root / ".app.json").exists():
        return {
            "status": "drift",
            "reason_code": "CODEX_PLUGIN_LEGACY_APP_DISCOVERABLE",
        }
    if (plugin_root / ".mcp.json").exists():
        return {
            "status": "drift",
            "reason_code": "CODEX_PLUGIN_MCP_REGISTRATION_DUPLICATE",
        }
    skills_root = plugin_root / "skills"
    try:
        skill_names = {
            path.name
            for path in skills_root.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        }
    except OSError:
        return {
            "status": "unavailable",
            "reason_code": "CODEX_PLUGIN_SKILLS_UNAVAILABLE",
        }
    if skill_names != PLUGIN_REQUIRED_SKILLS:
        return {
            "status": "drift",
            "reason_code": "CODEX_PLUGIN_SKILLS_DRIFT",
        }
    return {
        "status": "ready",
        "reason_code": "CODEX_PLUGIN_ROUTING_READY",
        "plugin_version": manifest.get("version")
        if isinstance(manifest.get("version"), str)
        else "unknown",
        "mcp_registration": "project_config",
        "legacy_app_manifest": "absent",
        "duplicate_mcp_manifest": "absent",
    }


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
    if len(set(names)) != len(names):
        return ()
    return tuple(names)


def _docker_executable() -> str | None:
    return shutil.which("docker.exe") or shutil.which("docker")


def _run_process(
    arguments: Sequence[str], *, timeout: float, command: str | None = None
) -> subprocess.CompletedProcess[str]:
    executable = command or shutil.which(arguments[0])
    if executable is None:
        raise StatusError("COMMAND_UNAVAILABLE")
    options: dict[str, object] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
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
    """Получить только SHA и dirty/clean state без публикации имён файлов."""

    try:
        revision_result = _run_process(
            ("git", "-C", str(root), "rev-parse", "HEAD"),
            timeout=5,
            command=shutil.which("git.exe") or shutil.which("git"),
        )
        status_result = _run_process(
            ("git", "-C", str(root), "status", "--porcelain"),
            timeout=5,
            command=shutil.which("git.exe") or shutil.which("git"),
        )
    except StatusError:
        return UNKNOWN_SOURCE_REVISION, "unknown"
    revision = _safe_sha(revision_result.stdout.strip())
    if revision == UNKNOWN_SOURCE_REVISION or status_result.returncode != 0:
        return revision, "unknown"
    return revision, "clean" if not status_result.stdout.strip() else "modified"


def _child_environment() -> dict[str, str]:
    """Подготовить bounded окружение без синтетической source provenance."""

    environment = dict(os.environ)
    environment.setdefault("PYTHONUTF8", "1")
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    return environment


def _extract_contract(result: object) -> Mapping[str, object]:
    structured = getattr(result, "structured_content", None)
    if not isinstance(structured, Mapping):
        raise StatusError("LOCAL_CONTRACT_PAYLOAD_INVALID")
    details = structured.get("details")
    contract = details.get("contract") if isinstance(details, Mapping) else None
    if not isinstance(contract, Mapping):
        raise StatusError("LOCAL_CONTRACT_PAYLOAD_INVALID")
    return contract


async def _probe_local_stdio(
    server_name: str, *, root: Path
) -> dict[str, object]:
    """Выполнить initialize, tools/list и ровно один read-only contract call."""

    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    executable = shutil.which("uv.exe") or shutil.which("uv")
    if executable is None:
        return {"status": "unavailable", "reason_code": "LOCAL_COMMAND_UNAVAILABLE"}
    module_name, contract_tool = SERVER_MODULES[server_name]
    parameters = StdioServerParameters(
        command=executable,
        args=["run", "--locked", "--no-sync", "python", "-m", module_name],
        cwd=root,
        env=_child_environment(),
    )
    try:
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialized = await session.initialize()
            listed = await session.list_tools()
            contract_result = await session.call_tool(contract_tool, {})
    except TimeoutError:
        return {"status": "unavailable", "reason_code": "LOCAL_PROBE_TIMEOUT"}
    except Exception as exc:  # noqa: BLE001 - boundary exposes type, not payload.
        return {
            "status": "unavailable",
            "reason_code": "LOCAL_PROBE_FAILED",
            "error_type": _safe_type_name(exc),
        }

    server_info = getattr(initialized, "server_info", None)
    observed_name = getattr(server_info, "name", None)
    observed_version = getattr(server_info, "version", None)
    protocol = getattr(initialized, "protocol_version", None)
    tool_items = getattr(listed, "tools", None)
    tool_names = _bounded_tool_names(
        [getattr(item, "name", None) for item in tool_items]
        if isinstance(tool_items, list)
        else None
    )
    try:
        contract = _extract_contract(contract_result)
    except StatusError as exc:
        return {"status": "unavailable", "reason_code": exc.code}
    if (
        not isinstance(observed_name, str)
        or not isinstance(observed_version, str)
        or not _SAFE_IDENTIFIER.fullmatch(observed_name)
        or not _VERSION_RE.fullmatch(observed_version)
        or not isinstance(protocol, str)
        or not _SAFE_TOKEN.fullmatch(protocol)
        or not tool_names
        or contract.get("server_name") != server_name
        or contract.get("server_version") != observed_version
    ):
        return {
            "status": "unavailable",
            "reason_code": "LOCAL_CONTRACT_IDENTITY_INVALID",
        }
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
        "tool_catalog_sha256": (
            contract.get("tool_catalog_sha256")
            if isinstance(contract.get("tool_catalog_sha256"), str)
            else None
        ),
    }


def _semgrep_executable() -> str | None:
    return shutil.which("semgrep.exe") or shutil.which("semgrep")


def _semgrep_result_summary(result: object) -> dict[str, object]:
    """Свести scan result к bounded evidence без публикации исходного payload."""

    is_error = getattr(result, "is_error", None)
    content = getattr(result, "content", None)
    if is_error is True or not isinstance(content, list):
        return {"status": "unavailable", "reason_code": "SEMGREP_SCAN_FAILED"}
    for item in content:
        text = getattr(item, "text", None)
        if not isinstance(text, str) or len(text) > _MAX_JSON_BYTES:
            continue
        try:
            payload = json.loads(text)
        except (UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        results = payload.get("results")
        if isinstance(results, list):
            return {
                "status": "ready",
                "reason_code": "SEMGREP_SCAN_READY",
                "finding_count": min(len(results), _MAX_TOOLS),
            }
    return {
        "status": "not_observable",
        "reason_code": "SEMGREP_SCAN_RESULT_NOT_OBSERVABLE",
        "finding_count": None,
    }


async def _probe_semgrep_local_mcp(root: Path) -> dict[str, object]:
    """Выполнить bounded local Semgrep MCP scan через фактический stdio route."""

    executable = _semgrep_executable()
    if executable is None:
        return {
            "status": "unavailable",
            "reason_code": "SEMGREP_LOCAL_COMMAND_UNAVAILABLE",
            "transport": "stdio",
        }
    source_path = (root / "dev_tools" / "mcp_status.py").resolve()
    if not source_path.is_file():
        return {
            "status": "unavailable",
            "reason_code": "SEMGREP_LOCAL_SOURCE_UNAVAILABLE",
            "transport": "stdio",
        }

    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    parameters = StdioServerParameters(
        command=executable,
        args=["mcp", "-t", "stdio"],
        cwd=root,
        env=_child_environment(),
    )
    try:
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialized = await asyncio.wait_for(
                session.initialize(), timeout=SEMGREP_PROBE_TIMEOUT_SECONDS
            )
            listed = await asyncio.wait_for(
                session.list_tools(), timeout=SEMGREP_PROBE_TIMEOUT_SECONDS
            )
            tool_items = getattr(listed, "tools", None)
            tool_names = _bounded_tool_names(
                [getattr(item, "name", None) for item in tool_items]
                if isinstance(tool_items, list)
                else None
            )
            scan_tool = next(
                (name for name in ("semgrep_scan_local", "semgrep_scan") if name in tool_names),
                None,
            )
            if scan_tool is None:
                return {
                    "status": "not_observable",
                    "reason_code": "SEMGREP_LOCAL_SCAN_TOOL_NOT_OBSERVABLE",
                    "transport": "stdio",
                    "tool_count": len(tool_names),
                    "tool_catalog_sha256": hashlib.sha256(
                        "\n".join(tool_names).encode("utf-8")
                    ).hexdigest(),
                }
            if scan_tool == "semgrep_scan_local":
                arguments = {
                    "code_files": [{"path": str(source_path)}],
                    "config": "auto",
                }
            else:
                arguments = {
                    "code_files": [{"path": str(source_path)}],
                    "config": "auto",
                }
            scan_result = await asyncio.wait_for(
                session.call_tool(scan_tool, arguments),
                timeout=SEMGREP_PROBE_TIMEOUT_SECONDS,
            )
    except TimeoutError:
        return {
            "status": "unavailable",
            "reason_code": "SEMGREP_LOCAL_PROBE_TIMEOUT",
            "transport": "stdio",
        }
    except Exception as exc:  # noqa: BLE001 - boundary exposes type, not payload.
        return {
            "status": "unavailable",
            "reason_code": "SEMGREP_LOCAL_PROBE_FAILED",
            "transport": "stdio",
            "error_type": _safe_type_name(exc),
        }

    server_info = getattr(initialized, "server_info", None)
    observed_name = getattr(server_info, "name", None)
    observed_version = getattr(server_info, "version", None)
    if (
        not isinstance(observed_name, str)
        or not isinstance(observed_version, str)
        or not _SAFE_IDENTIFIER.fullmatch(observed_name)
        or not _VERSION_RE.fullmatch(observed_version)
    ):
        return {
            "status": "unavailable",
            "reason_code": "SEMGREP_LOCAL_IDENTITY_INVALID",
            "transport": "stdio",
        }
    summary = _semgrep_result_summary(scan_result)
    return {
        **summary,
        "transport": "stdio",
        "server_name": observed_name,
        "server_version": observed_version,
        "scan_tool": scan_tool,
        "tool_count": len(tool_names),
        "tool_catalog_sha256": hashlib.sha256(
            "\n".join(tool_names).encode("utf-8")
        ).hexdigest(),
        "runtime_reachable": True,
        "runtime_ready": summary.get("status") == "ready",
    }


def _safe_remote_url(value: str) -> tuple[str, str, str] | None:
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    if parsed.scheme not in _URL_SCHEMES or not parsed.hostname:
        return None
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    if parsed.path.rstrip("/") not in {"", "/mcp"}:
        return None
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    endpoint = urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/mcp", "", ""))
    metadata = f"{origin}/.well-known/oauth-protected-resource/mcp"
    return origin, metadata, endpoint


def _remote_metadata_request(url: str) -> tuple[str, str]:
    parsed = _safe_remote_url(url)
    if parsed is None:
        return "unavailable", "REMOTE_URL_INVALID"
    _origin, metadata_url, _endpoint = parsed
    request = Request(
        metadata_url,
        headers={"Accept": "application/json", "User-Agent": "azurpilot-mcp-status/1"},
        method="GET",
    )
    try:
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected -- URL разрешён только как HTTPS metadata endpoint в _safe_remote_url.
        with urlopen(request, timeout=REMOTE_TIMEOUT_SECONDS) as response:
            raw = response.read(_MAX_JSON_BYTES + 1)
            status = int(getattr(response, "status", 200))
    except HTTPError as exc:
        return "unavailable", f"REMOTE_METADATA_HTTP_{exc.code}"
    except (OSError, URLError, TimeoutError):
        return "unavailable", "REMOTE_METADATA_UNAVAILABLE"
    if status != 200 or len(raw) > _MAX_JSON_BYTES:
        return "unavailable", "REMOTE_METADATA_INVALID"
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return "unavailable", "REMOTE_METADATA_INVALID"
    if not isinstance(payload, dict):
        return "unavailable", "REMOTE_METADATA_INVALID"
    return "ready", "REMOTE_PUBLIC_EDGE_METADATA_READY"


def _remote_access_token(server_name: str) -> str | None:
    """Получить explicit bearer token без публикации его значения."""

    suffix = server_name.removeprefix("azurpilot-").upper()
    direct_name = f"AZURPILOT_{suffix}_MCP_ACCESS_TOKEN"
    file_name = f"{direct_name}_FILE"
    direct = os.environ.get(direct_name, "").strip()
    if direct:
        return direct[:8192]
    token_path = os.environ.get(file_name, "").strip()
    if not token_path:
        return None
    try:
        value = Path(token_path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    value = value.strip()
    return value[:8192] if value else None


async def _probe_remote_backend(
    server_name: str, endpoint: str, token: str
) -> dict[str, object]:
    """Проверить authenticated remote MCP, а не только его public edge."""

    try:
        import httpx2
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError:
        return {
            "status": "unavailable",
            "reason_code": "REMOTE_MCP_CLIENT_UNAVAILABLE",
        }

    module_name, contract_tool = SERVER_MODULES[server_name]
    del module_name
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with (  # noqa: SIM117
            httpx2.AsyncClient(
                headers=headers,
                timeout=REMOTE_TIMEOUT_SECONDS,
                follow_redirects=False,
            ) as http_client,
            streamable_http_client(
                endpoint, http_client=http_client, terminate_on_close=True
            ) as (read_stream, write_stream),
        ):
            # Сессия зависит от уже открытых потоков, поэтому contexts нельзя объединить.
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await asyncio.wait_for(
                    session.initialize(), timeout=REMOTE_TIMEOUT_SECONDS
                )
                listed = await asyncio.wait_for(
                    session.list_tools(), timeout=REMOTE_TIMEOUT_SECONDS
                )
                contract_result = await asyncio.wait_for(
                    session.call_tool(contract_tool, {}),
                    timeout=REMOTE_TIMEOUT_SECONDS,
                )
    except TimeoutError:
        return {
            "status": "unavailable",
            "reason_code": "REMOTE_BACKEND_PROBE_TIMEOUT",
        }
    except Exception as exc:  # noqa: BLE001 - boundary exposes type, not payload.
        return {
            "status": "unavailable",
            "reason_code": "REMOTE_BACKEND_PROBE_FAILED",
            "error_type": _safe_type_name(exc),
        }

    server_info = getattr(initialized, "server_info", None)
    observed_name = getattr(server_info, "name", None)
    observed_version = getattr(server_info, "version", None)
    protocol = getattr(initialized, "protocol_version", None)
    tool_items = getattr(listed, "tools", None)
    tool_names = _bounded_tool_names(
        [getattr(item, "name", None) for item in tool_items]
        if isinstance(tool_items, list)
        else None
    )
    try:
        contract = _extract_contract(contract_result)
    except StatusError:
        return {
            "status": "unavailable",
            "reason_code": "REMOTE_BACKEND_CONTRACT_INVALID",
        }
    if (
        not isinstance(observed_name, str)
        or observed_name != server_name
        or not isinstance(observed_version, str)
        or not _VERSION_RE.fullmatch(observed_version)
        or not isinstance(protocol, str)
        or not _SAFE_TOKEN.fullmatch(protocol)
        or not tool_names
        or contract.get("server_name") != server_name
        or contract.get("server_version") != observed_version
    ):
        return {
            "status": "unavailable",
            "reason_code": "REMOTE_BACKEND_IDENTITY_INVALID",
        }
    return {
        "status": "ready",
        "reason_code": "REMOTE_BACKEND_READY",
        "evidence_kind": "authenticated_remote_probe",
        "runtime_reachable": True,
        "runtime_ready": True,
        "server_name": observed_name,
        "server_version": observed_version,
        "protocol_version": protocol,
        "contract_schema_version": contract.get("contract_schema_version"),
        "source_revision": _safe_sha(contract.get("source_revision")),
        "tool_count": len(tool_names),
        "tool_catalog_sha256": hashlib.sha256(
            "\n".join(tool_names).encode("utf-8")
        ).hexdigest(),
    }


async def _probe_remote(server_name: str) -> dict[str, object]:
    env_name = (
        f"AZURPILOT_{server_name.removeprefix('azurpilot-').upper()}_MCP_PUBLIC_URL"
    )
    url = os.environ.get(env_name, "").strip()
    if not url:
        return {
            "remote_backend": {
                "status": "not_configured",
                "reason_code": "REMOTE_PUBLIC_URL_NOT_CONFIGURED",
                "runtime_reachable": False,
                "runtime_ready": False,
            },
            "public_edge": {
                "status": "not_configured",
                "reason_code": "REMOTE_PUBLIC_URL_NOT_CONFIGURED",
                "edge_reachable": False,
            },
        }
    status, code = await asyncio.to_thread(_remote_metadata_request, url)
    parsed = _safe_remote_url(url)
    public_edge = {
        "status": status,
        "reason_code": code,
        "edge_reachable": status == "ready",
        "evidence_kind": "public_edge_metadata",
    }
    if parsed is None:
        return {
            "remote_backend": {
                "status": "unavailable",
                "reason_code": "REMOTE_URL_INVALID",
                "runtime_reachable": False,
                "runtime_ready": False,
            },
            "public_edge": public_edge,
        }
    token = _remote_access_token(server_name)
    if not token:
        backend = {
            "status": "not_configured",
            "reason_code": "REMOTE_BACKEND_TOKEN_NOT_CONFIGURED",
            "runtime_reachable": False,
            "runtime_ready": False,
        }
    else:
        backend = await _probe_remote_backend(server_name, parsed[2], token)
    return {
        "remote_backend": backend,
        "public_edge": public_edge,
    }


def _load_codex_config(root: Path) -> Mapping[str, object]:
    try:
        import tomllib

        with (root / ".codex" / "config.toml").open("rb") as stream:
            payload = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return payload if isinstance(payload, Mapping) else {}


def _codex_entry_status(
    config: Mapping[str, object],
    name: str,
    *,
    expected_command: str,
    expected_args: Sequence[str],
    expected_cwd: str = ".",
    expected_startup_timeout_sec: int | None = None,
    expected_tool_timeout_sec: int | None = None,
    expected_required: bool | None = None,
) -> dict[str, object]:
    servers = config.get("mcp_servers")
    entry = servers.get(name) if isinstance(servers, Mapping) else None
    if not isinstance(entry, Mapping):
        return {
            "status": "not_configured",
            "reason_code": "CODEX_SERVER_NOT_CONFIGURED",
        }
    command = entry.get("command")
    args = entry.get("args")
    if (
        command != expected_command
        or not isinstance(args, list)
        or tuple(args) != tuple(expected_args)
        or entry.get("enabled") is not True
        or entry.get("cwd") != expected_cwd
        or (
            expected_startup_timeout_sec is not None
            and entry.get("startup_timeout_sec") != expected_startup_timeout_sec
        )
        or (
            expected_tool_timeout_sec is not None
            and entry.get("tool_timeout_sec") != expected_tool_timeout_sec
        )
        or (
            expected_required is not None
            and entry.get("required") is not expected_required
        )
    ):
        return {"status": "drift", "reason_code": "CODEX_SERVER_CONFIG_DRIFT"}
    return {
        "status": "configured",
        "reason_code": "CODEX_SERVER_CONFIGURED",
        "enabled": True,
        "cwd": expected_cwd,
    }


def _codex_url_entry_status(
    config: Mapping[str, object], name: str, *, expected_url: str
) -> dict[str, object]:
    servers = config.get("mcp_servers")
    entry = servers.get(name) if isinstance(servers, Mapping) else None
    if not isinstance(entry, Mapping):
        return {
            "status": "not_configured",
            "reason_code": "CODEX_SERVER_NOT_CONFIGURED",
        }
    if entry.get("url") != expected_url or entry.get("enabled") is not True:
        return {"status": "drift", "reason_code": "CODEX_SERVER_CONFIG_DRIFT"}
    return {
        "status": "configured",
        "reason_code": "CODEX_SERVER_CONFIGURED",
        "enabled": True,
    }


def _tool_result_ready(result: object) -> tuple[bool, str]:
    """Проверить bounded MCP tool result без публикации payload."""

    if getattr(result, "is_error", False) is True:
        return False, "MCP_TOOL_RESULT_ERROR"
    structured = getattr(result, "structured_content", None)
    content = getattr(result, "content", None)
    if isinstance(structured, Mapping) and structured:
        return True, "MCP_TOOL_RESULT_READY"
    if isinstance(content, list) and content:
        return True, "MCP_TOOL_RESULT_READY"
    return False, "MCP_TOOL_RESULT_NOT_OBSERVABLE"


async def _probe_direct_route(
    server_name: str, codex_config: Mapping[str, object]
) -> dict[str, object]:
    """Проверить direct third-party route, не подменяя user-scoped evidence."""

    policy = MCP_ROUTE_POLICY.get(server_name, {})
    canonical_route = policy.get("canonical_route", "unknown")
    if server_name == "context7":
        return {
            "status": "not_observable",
            "reason_code": "DIRECT_USER_SCOPED_ACCEPTANCE_EXTERNAL",
            "evidence_kind": "user_scoped_codex_session",
            "canonical_route": canonical_route,
            "gateway_required": False,
            "runtime_reachable": False,
            "runtime_ready": False,
        }
    if server_name != "docker-docs":
        return {
            "status": "invalid",
            "reason_code": "DIRECT_ROUTE_NOT_SUPPORTED",
            "canonical_route": canonical_route,
            "gateway_required": False,
        }
    config_status = _codex_url_entry_status(
        codex_config,
        "docker_docs_direct",
        expected_url=_EXPECTED_DOCKER_REMOTE_URLS["docker-docs"],
    )
    if config_status.get("status") != "configured":
        return {
            **config_status,
            "canonical_route": canonical_route,
            "gateway_required": False,
            "runtime_reachable": False,
            "runtime_ready": False,
        }
    try:
        import httpx2
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with httpx2.AsyncClient(
            timeout=REMOTE_TIMEOUT_SECONDS, follow_redirects=False
        ) as http_client:
            async with streamable_http_client(
                _EXPECTED_DOCKER_REMOTE_URLS["docker-docs"],
                http_client=http_client,
                terminate_on_close=True,
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    initialized = await asyncio.wait_for(
                        session.initialize(), timeout=REMOTE_TIMEOUT_SECONDS
                    )
                    listed = await asyncio.wait_for(
                        session.list_tools(), timeout=REMOTE_TIMEOUT_SECONDS
                    )
                    tool_items = getattr(listed, "tools", None)
                    tool_names = _bounded_tool_names(
                        [getattr(item, "name", None) for item in tool_items]
                        if isinstance(tool_items, list)
                        else None
                    )
                    if "fetch_docker_docs" not in tool_names:
                        return {
                            "status": "not_observable",
                            "reason_code": "DIRECT_DOCKER_DOCS_TOOL_NOT_OBSERVABLE",
                            "canonical_route": canonical_route,
                            "gateway_required": False,
                            "runtime_reachable": True,
                            "runtime_ready": False,
                        }
                    result = await asyncio.wait_for(
                        session.call_tool("fetch_docker_docs", {}),
                        timeout=REMOTE_TIMEOUT_SECONDS,
                    )
    except TimeoutError:
        return {
            "status": "unavailable",
            "reason_code": "DIRECT_DOCKER_DOCS_PROBE_TIMEOUT",
            "canonical_route": canonical_route,
            "gateway_required": False,
            "runtime_reachable": False,
            "runtime_ready": False,
        }
    except Exception as exc:  # noqa: BLE001 - bounded direct route boundary.
        return {
            "status": "unavailable",
            "reason_code": "DIRECT_DOCKER_DOCS_PROBE_FAILED",
            "error_type": _safe_type_name(exc),
            "canonical_route": canonical_route,
            "gateway_required": False,
            "runtime_reachable": False,
            "runtime_ready": False,
        }
    result_ready, result_code = _tool_result_ready(result)
    if not result_ready:
        return {
            "status": "unavailable",
            "reason_code": f"DIRECT_DOCKER_DOCS_{result_code}",
            "canonical_route": canonical_route,
            "gateway_required": False,
            "runtime_reachable": True,
            "runtime_ready": False,
        }
    server_info = getattr(initialized, "server_info", None)
    observed_version = getattr(server_info, "version", None)
    return {
        **config_status,
        "status": "ready",
        "reason_code": "DIRECT_DOCKER_DOCS_READY",
        "evidence_kind": "direct_streamable_http_probe",
        "canonical_route": canonical_route,
        "gateway_required": False,
        "runtime_reachable": True,
        "runtime_ready": True,
        "server_version": observed_version
        if isinstance(observed_version, str) and _VERSION_RE.fullmatch(observed_version)
        else "unknown",
        "tool_name": "fetch_docker_docs",
        "tool_count": len(tool_names),
        "tool_catalog_sha256": hashlib.sha256(
            "\n".join(tool_names).encode("utf-8")
        ).hexdigest(),
    }


def _docker_json(arguments: Sequence[str]) -> tuple[object | None, str]:
    executable = _docker_executable()
    if executable is None:
        return None, "DOCKER_CLI_UNAVAILABLE"
    try:
        result = _run_process((executable, *arguments), timeout=10, command=executable)
    except StatusError as exc:
        return None, exc.code
    if result.returncode != 0:
        return None, "DOCKER_COMMAND_FAILED"
    try:
        value = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError):
        return None, "DOCKER_JSON_INVALID"
    return value, "OK"


def _docker_secret_engine_status(executable: str) -> dict[str, object]:
    """Разделить secret store, host-side RPC и runtime injection.

    Read-only probe выполняет только ``docker pass --help``, ``docker pass ls``
    и ``docker pass plugins ls``. Эти команды проверяют доступность локального
    keychain и Secrets Engine RPC, но не доказывают, что текущий процесс может
    выполнить ``se://`` injection в контейнер или Gateway. Поэтому эти
    поверхности намеренно не сворачиваются в один флаг.
    """

    def result(
        *,
        status: str,
        reason_code: str,
        cli_status: str,
        keychain_status: str,
        rpc_status: str,
        host_status: str,
        host_reason_code: str,
    ) -> dict[str, object]:
        return {
            "status": status,
            "reason_code": reason_code,
            "cli_status": cli_status,
            "keychain_status": keychain_status,
            "rpc_status": rpc_status,
            "secret_store": {
                "status": keychain_status,
                "reason_code": "DOCKER_SECRET_STORE_READY"
                if keychain_status == "ready"
                else "DOCKER_SECRET_STORE_UNAVAILABLE"
                if keychain_status == "unavailable"
                else "DOCKER_SECRET_STORE_NOT_OBSERVABLE",
            },
            "container_runtime_secret_injection": {
                "status": "not_observable",
                "reason_code": "CONTAINER_RUNTIME_SECRET_INJECTION_NOT_PROBED",
            },
            "gateway_secret_injection": {
                "status": "not_observable",
                "reason_code": "GATEWAY_SECRET_INJECTION_NOT_PROBED",
            },
            "host_pass_resolution": {
                "status": host_status,
                "reason_code": host_reason_code,
                "scope": "current_process",
            },
        }

    try:
        help_result = _run_process(
            (executable, "pass", "--help"), timeout=10, command=executable
        )
    except StatusError:
        return result(
            status="unavailable",
            reason_code="DOCKER_PASS_CLI_UNAVAILABLE",
            cli_status="unavailable",
            keychain_status="unknown",
            rpc_status="unknown",
            host_status="unavailable",
            host_reason_code="DOCKER_PASS_CLI_UNAVAILABLE",
        )
    if help_result.returncode != 0:
        return result(
            status="unavailable",
            reason_code="DOCKER_PASS_CLI_UNAVAILABLE",
            cli_status="unavailable",
            keychain_status="unknown",
            rpc_status="unknown",
            host_status="unavailable",
            host_reason_code="DOCKER_PASS_CLI_UNAVAILABLE",
        )
    try:
        keychain_result = _run_process(
            (executable, "pass", "ls"), timeout=10, command=executable
        )
        plugin_result = _run_process(
            (executable, "pass", "plugins", "ls"), timeout=10, command=executable
        )
    except StatusError as exc:
        return result(
            status="partial",
            reason_code="DOCKER_SECRET_ENGINE_RPC_UNAVAILABLE",
            cli_status="ready",
            keychain_status="unknown",
            rpc_status=exc.code,
            host_status="degraded",
            host_reason_code="DOCKER_SECRET_ENGINE_RPC_UNAVAILABLE",
        )
    keychain_status = "ready" if keychain_result.returncode == 0 else "unavailable"
    rpc_status = "ready" if plugin_result.returncode == 0 else "unavailable"
    if keychain_status == "ready" and rpc_status == "ready":
        return result(
            status="ready",
            reason_code="DOCKER_SECRET_ENGINE_READY",
            cli_status="ready",
            keychain_status=keychain_status,
            rpc_status=rpc_status,
            host_status="ready",
            host_reason_code="DOCKER_PASS_RESOLUTION_READY",
        )
    if keychain_status == "ready":
        return result(
            status="partial",
            reason_code="DOCKER_SECRET_ENGINE_RPC_UNAVAILABLE",
            cli_status="ready",
            keychain_status=keychain_status,
            rpc_status=rpc_status,
            host_status="degraded",
            host_reason_code="DOCKER_SECRET_ENGINE_RPC_UNAVAILABLE",
        )
    return result(
        status="unavailable",
        reason_code="DOCKER_PASS_KEYCHAIN_UNAVAILABLE",
        cli_status="ready",
        keychain_status=keychain_status,
        rpc_status=rpc_status,
        host_status="unavailable",
        host_reason_code="DOCKER_PASS_KEYCHAIN_UNAVAILABLE",
    )


def _docker_server_name(server: Mapping[str, object]) -> str | None:
    snapshot = server.get("snapshot")
    snapshot_server = snapshot.get("server") if isinstance(snapshot, Mapping) else None
    candidates = (
        snapshot_server.get("name") if isinstance(snapshot_server, Mapping) else None,
        server.get("name"),
    )
    return next(
        (
            value
            for value in candidates
            if isinstance(value, str) and _SAFE_IDENTIFIER.fullmatch(value)
        ),
        None,
    )


def _snapshot_tool_names(server: Mapping[str, object]) -> tuple[str, ...]:
    snapshot = server.get("snapshot")
    snapshot_server = snapshot.get("server") if isinstance(snapshot, Mapping) else None
    snapshot_tools = (
        snapshot_server.get("tools") if isinstance(snapshot_server, Mapping) else None
    )
    if not isinstance(snapshot_tools, list) or len(snapshot_tools) > _MAX_TOOLS:
        return ()
    names = [
        item.get("name")
        for item in snapshot_tools
        if isinstance(item, Mapping)
    ]
    return _bounded_tool_names(names)


def _docker_server_status(server: Mapping[str, object]) -> dict[str, object]:
    name = _docker_server_name(server)
    if name is None:
        return {"status": "invalid", "reason_code": "DOCKER_SERVER_ID_INVALID"}
    route_policy = _route_policy(name)
    tools = _bounded_tool_names(server.get("tools"))
    snapshot_tools = _snapshot_tool_names(server)
    snapshot = server.get("snapshot")
    snapshot_server = snapshot.get("server") if isinstance(snapshot, Mapping) else None
    command = (
        snapshot_server.get("command") if isinstance(snapshot_server, Mapping) else None
    )
    command_names = (
        tuple(item for item in command if isinstance(item, str))
        if isinstance(command, list)
        else ()
    )
    writes = sorted(set(tools).intersection(_KNOWN_WRITE_TOOLS))
    expected_tools = _REQUIRED_TOOL_SETS.get(name)
    allowlist_status = "not_applicable"
    if expected_tools is not None:
        allowlist_status = "ready" if set(tools) == expected_tools else "drift"
    disable_write = "--disable-write" in command_names
    tools_observable = bool(tools)
    read_only = not writes and (
        disable_write if name == "grafana" else allowlist_status != "drift"
    )
    policy_status = "ready" if read_only else "drift"
    policy_reason = (
        "DOCKER_SERVER_READ_ONLY_POLICY_READY"
        if read_only
        else "DOCKER_SERVER_WRITE_OR_ALLOWLIST_DRIFT"
    )
    return {
        "status": "ready" if read_only and allowlist_status != "drift" else "drift",
        "reason_code": "DOCKER_SERVER_PROFILE_CONFIGURED"
        if read_only and allowlist_status != "drift"
        else "DOCKER_SERVER_WRITE_OR_ALLOWLIST_DRIFT",
        "configured": True,
        "tool_count": len(tools),
        "snapshot_tool_count": len(snapshot_tools),
        "write_tools_exposed": writes,
        "read_only": read_only,
        "allowlist_status": allowlist_status,
        "image_pinned": isinstance(server.get("image"), str)
        and "@sha256:" in str(server.get("image")),
        "disable_write": disable_write,
        "tools_observable": tools_observable,
        "profile_tool_names": list(tools),
        "snapshot_tool_names": list(snapshot_tools),
        "runtime_reachable": False,
        "runtime_ready": False,
        "canonical_route": route_policy.get("canonical_route", "unknown"),
        "gateway_required": route_policy.get("gateway_required") is True,
        "collector_required": route_policy.get("collector_required") is True,
        "required_runtime": route_policy.get("required_runtime") is True,
        "credential_evidence": route_policy.get("credential_evidence"),
        "read_only_policy": {
            "status": policy_status,
            "reason_code": policy_reason,
        },
    }


def _decode_bounded_json(text: object) -> tuple[object | None, str]:
    """Извлечь первый bounded JSON value из CLI stdout с возможным префиксом."""

    if not isinstance(text, str):
        return None, "DOCKER_GATEWAY_TOOL_RESULT_INVALID"
    try:
        if len(text.encode("utf-8", errors="replace")) > _MAX_JSON_BYTES:
            return None, "DOCKER_GATEWAY_TOOL_RESULT_TOO_LARGE"
    except UnicodeError:
        return None, "DOCKER_GATEWAY_TOOL_RESULT_INVALID"
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character not in "[{":
            continue
        try:
            value, _end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        return value, "OK"
    return None, "DOCKER_GATEWAY_TOOL_RESULT_INVALID"


def _docker_tool_payload_ready(payload: object) -> tuple[bool, str]:
    """Проверить содержимое read-only CLI tool result без публикации payload."""

    if isinstance(payload, Mapping) and payload.get("isError") is True:
        return False, "DOCKER_GATEWAY_TOOL_RESULT_ERROR"
    if isinstance(payload, Mapping):
        if not payload:
            return False, "DOCKER_GATEWAY_TOOL_RESULT_NOT_OBSERVABLE"
        if payload.get("isError") is False:
            structured = payload.get("structuredContent")
            content = payload.get("content")
            if not (
                isinstance(structured, Mapping)
                and structured
                or isinstance(content, list)
                and content
            ):
                return False, "DOCKER_GATEWAY_TOOL_RESULT_NOT_OBSERVABLE"
        return True, "DOCKER_GATEWAY_TOOL_RESULT_READY"
    if isinstance(payload, list) and payload:
        return True, "DOCKER_GATEWAY_TOOL_RESULT_READY"
    return False, "DOCKER_GATEWAY_TOOL_RESULT_NOT_OBSERVABLE"


def _docker_gateway_tools(executable: str) -> dict[str, object]:
    """Получить реальный Gateway catalog через тот же CLI, что использует клиент."""

    arguments = (
        executable,
        "mcp",
        "tools",
        "ls",
        "--format=json",
        "--gateway-arg=--profile",
        f"--gateway-arg={CANONICAL_DOCKER_PROFILE_ID}",
        "--gateway-arg=--watch=false",
    )
    try:
        result = _run_process(
            arguments, timeout=DOCKER_COMMAND_TIMEOUT_SECONDS, command=executable
        )
    except StatusError as exc:
        return {
            "status": "unavailable",
            "reason_code": f"DOCKER_GATEWAY_TOOLS_{exc.code}",
            "runtime_reachable": False,
            "runtime_ready": False,
        }
    if result.returncode != 0:
        return {
            "status": "unavailable",
            "reason_code": "DOCKER_GATEWAY_TOOLS_LIST_FAILED",
            "runtime_reachable": False,
            "runtime_ready": False,
        }
    payload, payload_code = _decode_bounded_json(result.stdout)
    if payload_code != "OK":
        return {
            "status": "unavailable",
            "reason_code": payload_code.replace(
                "DOCKER_GATEWAY_TOOL_RESULT", "DOCKER_GATEWAY_TOOLS_CATALOG"
            ),
            "runtime_reachable": False,
            "runtime_ready": False,
        }
    if not isinstance(payload, list):
        return {
            "status": "unavailable",
            "reason_code": "DOCKER_GATEWAY_TOOLS_CATALOG_INVALID",
            "runtime_reachable": False,
            "runtime_ready": False,
        }
    names = _bounded_tool_names(
        [item.get("name") for item in payload if isinstance(item, Mapping)]
    )
    if not names:
        return {
            "status": "not_observable",
            "reason_code": "DOCKER_GATEWAY_TOOLS_NOT_OBSERVABLE",
            "runtime_reachable": True,
            "runtime_ready": False,
        }
    return {
        "status": "ready",
        "reason_code": "DOCKER_GATEWAY_TOOLS_READY",
        "runtime_reachable": True,
        "runtime_ready": False,
        "tool_count": len(names),
        "tool_names": list(names),
        "tool_catalog_sha256": hashlib.sha256(
            "\n".join(names).encode("utf-8")
        ).hexdigest(),
    }


def _docker_gateway_tool_call(
    executable: str, tool_name: str, arguments: Mapping[str, str]
) -> dict[str, object]:
    if not _SAFE_IDENTIFIER.fullmatch(tool_name):
        return {"status": "invalid", "reason_code": "DOCKER_TOOL_NAME_INVALID"}
    call_arguments = tuple(
        f"{key}={value}"
        for key, value in arguments.items()
        if _SAFE_IDENTIFIER.fullmatch(key) and isinstance(value, str) and len(value) <= 256
    )
    if len(call_arguments) != len(arguments):
        return {"status": "invalid", "reason_code": "DOCKER_TOOL_ARGUMENT_INVALID"}
    command = (
        executable,
        "mcp",
        "tools",
        "call",
        tool_name,
        *call_arguments,
        "--format=json",
        "--gateway-arg=--profile",
        f"--gateway-arg={CANONICAL_DOCKER_PROFILE_ID}",
        "--gateway-arg=--watch=false",
    )
    try:
        result = _run_process(
            command, timeout=DOCKER_COMMAND_TIMEOUT_SECONDS, command=executable
        )
    except StatusError as exc:
        return {
            "status": "unavailable",
            "reason_code": f"DOCKER_GATEWAY_TOOL_CALL_{exc.code}",
        }
    if result.returncode != 0:
        return {
            "status": "unavailable",
            "reason_code": "DOCKER_GATEWAY_TOOL_CALL_FAILED",
        }
    payload, payload_code = _decode_bounded_json(result.stdout)
    if payload_code != "OK":
        return {"status": "unavailable", "reason_code": payload_code}
    payload_ready, result_code = _docker_tool_payload_ready(payload)
    if not payload_ready:
        return {"status": "unavailable", "reason_code": result_code}
    return {
        "status": "ready",
        "reason_code": "DOCKER_GATEWAY_READ_ONLY_CALL_READY",
        "tool_name": tool_name,
    }


def _docker_gateway_runtime(
    executable: str,
    profile_config_servers: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    catalog = _docker_gateway_tools(executable)
    if catalog.get("status") != "ready":
        runtime_servers = {
            name: {
                "status": "not_observable",
                "reason_code": "DOCKER_GATEWAY_CATALOG_NOT_READY",
                "runtime_reachable": catalog.get("runtime_reachable") is True,
                "runtime_ready": False,
                "tools_observable": False,
                "canonical_route": _route_policy(name).get(
                    "canonical_route", "unknown"
                ),
                "gateway_required": _route_policy(name).get("gateway_required")
                is True,
                "required_runtime": _route_policy(name).get("required_runtime")
                is True,
            }
            for name in THIRD_PARTY_SERVERS
        }
        return {
            **catalog,
            "runtime_ready": False,
            "required_runtime_ready": False,
            "servers": runtime_servers,
        }
    catalog_names = set(catalog.get("tool_names", ()))
    declared_names: dict[str, set[str]] = {
        name: set(
            profile_server.get("profile_tool_names", ())
            if isinstance(profile_server, Mapping)
            else ()
        )
        | set(
            profile_server.get("snapshot_tool_names", ())
            if isinstance(profile_server, Mapping)
            else ()
        )
        for name, profile_server in profile_config_servers.items()
    }
    runtime_servers: dict[str, dict[str, object]] = {}
    candidates: dict[str, tuple[str, Mapping[str, object], bool]] = {}
    for name in THIRD_PARTY_SERVERS:
        route_policy = _route_policy(name)
        gateway_required = route_policy.get("gateway_required") is True
        if not gateway_required:
            runtime_servers[name] = {
                "status": "not_observable",
                "reason_code": "DOCKER_GATEWAY_OPTIONAL_ROUTE_NOT_PROBED",
                "runtime_reachable": True,
                "runtime_ready": False,
                "tools_observable": False,
                "canonical_route": route_policy.get("canonical_route", "unknown"),
                "gateway_required": False,
                "required_runtime": route_policy.get("required_runtime") is True,
                "read_only_policy": {"status": "not_applicable"},
            }
            continue
        configured = profile_config_servers.get(name, {})
        policy = (
            configured.get("read_only_policy")
            if isinstance(configured, Mapping)
            else None
        )
        policy_ready = isinstance(policy, Mapping) and policy.get("status") == "ready"
        candidate = next(
            (
                tool
                for tool in DOCKER_RUNTIME_PROBE_TOOLS[name]
                if tool in catalog_names
                and (
                    tool in declared_names.get(name, set())
                    or not any(
                        tool in other_names
                        for other_name, other_names in declared_names.items()
                        if other_name != name
                    )
                )
            ),
            None,
        )
        if candidate is None:
            runtime_servers[name] = {
                "status": "not_observable",
                "reason_code": (
                    "DOCKER_SERVER_RUNTIME_TOOL_NOT_OBSERVABLE"
                    if gateway_required
                    else "DOCKER_GATEWAY_OPTIONAL_ROUTE_NOT_OBSERVABLE"
                ),
                "runtime_reachable": True,
                "runtime_ready": False,
                "tools_observable": False,
                "canonical_route": route_policy.get("canonical_route", "unknown"),
                "gateway_required": gateway_required,
                "required_runtime": route_policy.get("required_runtime") is True,
                "read_only_policy": policy
                if isinstance(policy, Mapping)
                else {"status": "unknown"},
            }
            continue
        candidates[name] = (
            candidate,
            policy if isinstance(policy, Mapping) else {},
            policy_ready,
        )

    # Docker MCP Gateway не гарантирует параллельную обработку нескольких
    # `tools/call` для одного profile. Последовательные bounded probes дают
    # воспроизводимое evidence и не создают конкурентный retry storm.
    calls = {
        name: _docker_gateway_tool_call(
            executable,
            candidate,
            DOCKER_RUNTIME_PROBE_ARGUMENTS.get(name, {}),
        )
        for name, (candidate, _, _) in candidates.items()
    }

    for name, (_, policy, policy_ready) in candidates.items():
        call = calls[name]
        ready = policy_ready and call.get("status") == "ready"
        route_policy = _route_policy(name)
        gateway_required = route_policy.get("gateway_required") is True
        call_status = call.get("status", "unavailable")
        if not policy_ready:
            item_status = "drift"
            item_reason = "DOCKER_SERVER_WRITE_OR_ALLOWLIST_DRIFT"
        else:
            item_status = call_status
            item_reason = call.get("reason_code", "DOCKER_SERVER_NOT_OBSERVABLE")
        runtime_servers[name] = {
            **call,
            "status": "ready" if ready else item_status,
            "reason_code": "DOCKER_GATEWAY_READ_ONLY_CALL_READY"
            if ready
            else item_reason,
            "runtime_reachable": True,
            "runtime_ready": ready,
            "tools_observable": True,
            "canonical_route": route_policy.get("canonical_route", "unknown"),
            "gateway_required": gateway_required,
            "required_runtime": route_policy.get("required_runtime") is True,
            "credential_evidence": {
                "status": "ready"
                if ready and name == "grafana"
                else "not_required"
                if ready and name == "dockerhub"
                else "not_observable",
                "reason_code": "DOCKER_GATEWAY_READ_ONLY_CALL_READY"
                if ready and name == "grafana"
                else "DOCKERHUB_PUBLIC_PROBE_NO_CREDENTIAL_ASSERTION"
                if ready and name == "dockerhub"
                else "DOCKER_GATEWAY_CREDENTIAL_EVIDENCE_NOT_OBSERVABLE",
            },
            "read_only_policy": policy,
        }
    required_runtime_ready = all(
        isinstance(runtime_servers.get(name), Mapping)
        and runtime_servers[name].get("runtime_ready") is True
        for name in GATEWAY_REQUIRED_SERVERS
    )
    return {
        **catalog,
        "status": "ready" if required_runtime_ready else "partial",
        "reason_code": "DOCKER_GATEWAY_REQUIRED_RUNTIME_READY"
        if required_runtime_ready
        else "DOCKER_GATEWAY_REQUIRED_RUNTIME_PARTIAL",
        "runtime_ready": required_runtime_ready,
        "required_runtime_ready": required_runtime_ready,
        "servers": runtime_servers,
    }


def validate_development_profile(profile: Mapping[str, object]) -> None:
    """Проверить экспортированный profile без доверия к его описаниям."""

    if (
        profile.get("version") != 1
        or profile.get("id") != CANONICAL_DOCKER_PROFILE_ID
        or profile.get("name") != CANONICAL_DOCKER_PROFILE_NAME
    ):
        raise StatusError("DOCKER_PROFILE_ID_INVALID")
    servers = profile.get("servers")
    if not isinstance(servers, list) or len(servers) != len(THIRD_PARTY_SERVERS):
        raise StatusError("DOCKER_PROFILE_SERVER_COUNT_INVALID")
    names: list[str] = []
    for server in servers:
        if not isinstance(server, Mapping):
            raise StatusError("DOCKER_PROFILE_SERVER_INVALID")
        if _FORBIDDEN_PROFILE_KEYS.intersection(server):
            raise StatusError("DOCKER_PROFILE_HOST_ACCESS_INVALID")
        name = _docker_server_name(server)
        if name is None or name in names:
            raise StatusError("DOCKER_PROFILE_SERVER_ID_INVALID")
        names.append(name)
        if name not in _PROFILE_SERVER_SET:
            raise StatusError("DOCKER_PROFILE_SERVER_SET_INVALID")
        if server.get("type") != _EXPECTED_DOCKER_SERVER_TYPES[name]:
            raise StatusError("DOCKER_PROFILE_SERVER_TYPE_INVALID")
        if server.get("type") == "image" and not (
            isinstance(server.get("image"), str) and "@sha256:" in server["image"]
        ):
            raise StatusError("DOCKER_PROFILE_IMAGE_NOT_PINNED")
        snapshot = server.get("snapshot")
        snapshot_server = (
            snapshot.get("server") if isinstance(snapshot, Mapping) else None
        )
        if server.get("type") == "remote":
            remote = (
                snapshot_server.get("remote")
                if isinstance(snapshot_server, Mapping)
                else None
            )
            if (
                not isinstance(remote, Mapping)
                or remote.get("url") != _EXPECTED_DOCKER_REMOTE_URLS[name]
                or remote.get("transport_type") != "streamable-http"
            ):
                raise StatusError("DOCKER_PROFILE_REMOTE_ENDPOINT_INVALID")
        tools = _bounded_tool_names(server.get("tools"))
        if name in _REQUIRED_TOOL_SETS and set(tools) != _REQUIRED_TOOL_SETS[name]:
            raise StatusError("DOCKER_PROFILE_ALLOWLIST_INVALID")
        if set(tools).intersection(_KNOWN_WRITE_TOOLS):
            raise StatusError("DOCKER_PROFILE_WRITE_TOOL_EXPOSED")
        if isinstance(
            snapshot_server, Mapping
        ) and _FORBIDDEN_PROFILE_KEYS.intersection(snapshot_server):
            raise StatusError("DOCKER_PROFILE_SNAPSHOT_HOST_ACCESS_INVALID")
        if name == "grafana":
            config = server.get("config")
            if (
                not isinstance(config, Mapping)
                or config.get("url") != CANONICAL_GRAFANA_PROFILE_URL
            ):
                raise StatusError("DOCKER_PROFILE_GRAFANA_CONFIG_INVALID")
            command = (
                snapshot_server.get("command")
                if isinstance(snapshot_server, Mapping)
                else None
            )
            if not isinstance(command, list) or "--disable-write" not in command:
                raise StatusError("DOCKER_PROFILE_GRAFANA_WRITE_MODE_INVALID")
    if set(names) != _PROFILE_SERVER_SET:
        raise StatusError("DOCKER_PROFILE_SERVER_SET_INVALID")
    secrets = profile.get("secrets")
    default = secrets.get("default") if isinstance(secrets, Mapping) else None
    if (
        not isinstance(default, Mapping)
        or default.get("provider") != "docker-desktop-store"
    ):
        raise StatusError("DOCKER_PROFILE_SECRET_PROVIDER_INVALID")


def _docker_status() -> dict[str, object]:
    executable = _docker_executable()
    if executable is None:
        return {"status": "unavailable", "reason_code": "DOCKER_CLI_UNAVAILABLE"}
    try:
        version_result = _run_process(
            (executable, "mcp", "version"), timeout=10, command=executable
        )
    except StatusError as exc:
        return {"status": "unavailable", "reason_code": exc.code}
    version = next(
        (
            line.strip()
            for line in version_result.stdout.splitlines()
            if _VERSION_RE.fullmatch(line.strip())
        ),
        None,
    )
    if version_result.returncode != 0 or version is None:
        return {"status": "unavailable", "reason_code": "DOCKER_VERSION_UNAVAILABLE"}
    secret_engine = _docker_secret_engine_status(executable)
    payload, code = _docker_json(("mcp", "profile", "list", "--format", "json"))
    if code != "OK" or not isinstance(payload, list):
        return {
            "status": "unavailable",
            "reason_code": code if code != "OK" else "DOCKER_PROFILE_LIST_INVALID",
            "version": version,
            "secret_engine": secret_engine,
        }
    matching_profiles = [
        item
        for item in payload
        if isinstance(item, Mapping)
        and item.get("id") == CANONICAL_DOCKER_PROFILE_ID
    ]
    if len(matching_profiles) > 1:
        return {
            "status": "drift",
            "reason_code": "DOCKER_PROFILE_DUPLICATE",
            "version": version,
            "profile_id": CANONICAL_DOCKER_PROFILE_ID,
            "secret_engine": secret_engine,
        }
    profile = matching_profiles[0] if matching_profiles else None
    if not isinstance(profile, Mapping):
        return {
            "status": "not_configured",
            "reason_code": "DOCKER_PROFILE_NOT_CONFIGURED",
            "version": version,
            "profile_id": CANONICAL_DOCKER_PROFILE_ID,
            "server_names": [],
            "profile_config": {
                "status": "not_configured",
                "reason_code": "DOCKER_PROFILE_NOT_CONFIGURED",
            },
            "gateway_runtime": {
                "status": "not_configured",
                "reason_code": "DOCKER_PROFILE_NOT_CONFIGURED",
                "runtime_reachable": False,
                "runtime_ready": False,
            },
            "third_party": {},
            "secret_engine": secret_engine,
        }
    raw_servers = profile.get("servers")
    if not isinstance(raw_servers, list):
        return {
            "status": "invalid",
            "reason_code": "DOCKER_PROFILE_SERVERS_INVALID",
            "version": version,
            "profile_id": CANONICAL_DOCKER_PROFILE_ID,
            "profile_config": {
                "status": "invalid",
                "reason_code": "DOCKER_PROFILE_SERVERS_INVALID",
            },
            "gateway_runtime": {
                "status": "unavailable",
                "reason_code": "DOCKER_PROFILE_SERVERS_INVALID",
                "runtime_reachable": False,
                "runtime_ready": False,
            },
            "secret_engine": secret_engine,
        }
    profile_config_error: str | None = None
    try:
        validate_development_profile(profile)
    except StatusError as exc:
        profile_config_error = exc.code
    server_statuses: dict[str, dict[str, object]] = {}
    for item in raw_servers:
        if isinstance(item, Mapping):
            name = _docker_server_name(item)
            if name is not None:
                server_statuses[name] = _docker_server_status(item)
    profile_name = profile.get("name")
    exact_servers = set(server_statuses) == _PROFILE_SERVER_SET
    profile_third_party = {
        name: server_statuses.get(
            name,
            {"status": "missing", "reason_code": "DOCKER_SERVER_NOT_PRESENT"},
        )
        for name in THIRD_PARTY_SERVERS
    }
    profile_config_status = (
        "ready"
        if profile_config_error is None
        and exact_servers
        and all(item.get("status") == "ready" for item in profile_third_party.values())
        else "drift"
    )
    required_profile_ready = (
        exact_servers
        and all(
            profile_third_party.get(name, {}).get("status") == "ready"
            for name in GATEWAY_REQUIRED_SERVERS
        )
        and profile_config_error not in _REQUIRED_PROFILE_ERRORS
    )
    required_profile_read_only = exact_servers and all(
        profile_third_party.get(name, {}).get("read_only") is True
        for name in GATEWAY_REQUIRED_SERVERS
    )
    profile_config = {
        "status": profile_config_status,
        "reason_code": "DOCKER_PROFILE_READY"
        if profile_config_status == "ready"
        else profile_config_error or "DOCKER_PROFILE_DRIFT",
        "profile_id": CANONICAL_DOCKER_PROFILE_ID,
        "server_names": sorted(server_statuses),
        "server_count": len(server_statuses),
        "third_party": profile_third_party,
        "read_only": exact_servers
        and all(item.get("read_only") is True for item in profile_third_party.values()),
        "canonical_status": "ready" if required_profile_ready else "drift",
        "canonical_read_only": required_profile_read_only,
        "canonical_reason_code": "DOCKER_REQUIRED_PROFILE_READY"
        if required_profile_ready
        else profile_config_error or "DOCKER_REQUIRED_PROFILE_DRIFT",
    }
    gateway_runtime = _docker_gateway_runtime(executable, profile_third_party)
    runtime_third_party = gateway_runtime.get("servers")
    if not isinstance(runtime_third_party, Mapping):
        runtime_third_party = {}
    third_party: dict[str, dict[str, object]] = {}
    for name in THIRD_PARTY_SERVERS:
        configured = profile_third_party.get(name, {})
        runtime = runtime_third_party.get(name, {})
        if not isinstance(configured, Mapping):
            configured = {}
        if not isinstance(runtime, Mapping):
            runtime = {}
        item = {
            **configured,
            "profile_config": configured,
            "gateway_runtime": runtime,
            "configured": configured.get("configured") is True,
            "tools_observable": runtime.get("tools_observable") is True,
            "runtime_reachable": runtime.get("runtime_reachable") is True,
            "runtime_ready": runtime.get("runtime_ready") is True,
            "read_only_policy": configured.get(
                "read_only_policy", {"status": "unknown"}
            ),
            "canonical_route": _route_policy(name).get("canonical_route", "unknown"),
            "gateway_required": _route_policy(name).get("gateway_required") is True,
            "required_runtime": _route_policy(name).get("required_runtime") is True,
        }
        item["status"] = (
            "ready"
            if item["runtime_ready"] is True
            else runtime.get("status", "not_observable")
        )
        item["reason_code"] = runtime.get(
            "reason_code", configured.get("reason_code", "DOCKER_SERVER_NOT_OBSERVABLE")
        )
        third_party[name] = item
    gateway_ready = gateway_runtime.get("required_runtime_ready") is True or (
        gateway_runtime.get("runtime_ready") is True
        and all(
            isinstance(third_party.get(name), Mapping)
            and third_party[name].get("runtime_ready") is True
            for name in GATEWAY_REQUIRED_SERVERS
        )
    )
    if not required_profile_ready:
        overall_status = "drift"
        overall_reason = "DOCKER_REQUIRED_PROFILE_DRIFT"
    elif not gateway_ready:
        overall_status = "partial"
        overall_reason = "DOCKER_GATEWAY_REQUIRED_RUNTIME_PARTIAL"
    else:
        overall_status = "ready"
        overall_reason = "DOCKER_REQUIRED_ROUTES_READY"
    return {
        "status": overall_status,
        "reason_code": overall_reason,
        "version": version,
        "profile_id": CANONICAL_DOCKER_PROFILE_ID,
        "profile_name": profile_name
        if isinstance(profile_name, str) and len(profile_name) <= 128
        else "unknown",
        "server_names": sorted(server_statuses),
        "third_party": third_party,
        "server_count": len(server_statuses),
        "profile_config": profile_config,
        "gateway_runtime": gateway_runtime,
        "canonical_status": overall_status,
        "canonical_reason_code": overall_reason,
        "canonical_read_only": required_profile_read_only and gateway_ready,
        "read_only": profile_config.get("read_only") is True
        and gateway_ready,
        "secret_engine": secret_engine,
    }


def _surface_status(
    probe: Mapping[str, object],
    *,
    expected_version: str,
    expected_revision: str,
    working_tree: str,
    source_mode: str = "local",
) -> dict[str, object]:
    result = dict(probe)
    if probe.get("status") != "ready":
        return result
    observed_version = probe.get("server_version")
    observed_revision = probe.get("source_revision")
    try:
        version_state = (
            "compatible"
            if isinstance(observed_version, str)
            and version_satisfies(observed_version, f"={expected_version}")
            else "drift"
        )
    except ValueError:
        version_state = "drift"
    if source_mode == "local" or probe.get("evidence_kind") == "representative_local_probe":
        if probe.get("evidence_kind") == "representative_local_probe":
            source_state = "not_applicable"
        elif (
            expected_revision != UNKNOWN_SOURCE_REVISION
            and isinstance(observed_revision, str)
            and _SHA_RE.fullmatch(observed_revision) is not None
        ):
            source_state = (
                "aligned" if observed_revision == expected_revision else "drift"
            )
        else:
            source_state = "unknown"
        if working_tree != "clean" and source_state in {"aligned", "not_applicable"}:
            source_state = "modified"
        runtime_ready = (
            version_state == "compatible"
            and source_state in {"aligned", "not_applicable"}
            and working_tree == "clean"
        )
        result_status = (
            "ready"
            if runtime_ready
            else "partial"
            if version_state == "compatible" and source_state != "drift"
            else "drift"
        )
    else:
        source_state = (
            "aligned"
            if expected_revision != UNKNOWN_SOURCE_REVISION
            and isinstance(observed_revision, str)
            and _SHA_RE.fullmatch(observed_revision) is not None
            and observed_revision == expected_revision
            else "unknown"
            if expected_revision == UNKNOWN_SOURCE_REVISION
            or not isinstance(observed_revision, str)
            or _SHA_RE.fullmatch(observed_revision) is None
            else "drift"
        )
        runtime_ready = version_state == "compatible" and source_state == "aligned"
        result_status = (
            "ready"
            if runtime_ready
            else "partial"
            if version_state == "compatible" and source_state == "unknown"
            else "drift"
        )
    result["version_status"] = version_state
    result["source_status"] = source_state
    result["runtime_reachable"] = True
    result["runtime_ready"] = runtime_ready
    result["status"] = result_status
    if result["status"] == "drift":
        result["reason_code"] = "MCP_VERSION_OR_SOURCE_DRIFT"
    elif result["status"] == "partial" and source_state == "unknown":
        result["reason_code"] = "MCP_SOURCE_PROVENANCE_UNKNOWN"
    return result


def _version_guard(
    root: Path, expected_versions: Mapping[str, str]
) -> dict[str, object]:
    """Проверить identity contracts и plugin compatibility без сети."""

    try:
        from module.dev_mcp.contract import (
            contract_compatibility_issues,
            server_compatibility_issues,
        )
        from module.dev_mcp.contract import (
            contract_payload as dev_contract_payload,
        )
        from module.game_mcp.contract import (
            contract_payload as game_contract_payload,
        )

        contracts = {
            "azurpilot-dev": dev_contract_payload(),
            "azurpilot-game": game_contract_payload(),
        }
        issues: list[str] = []
        for name, expected in expected_versions.items():
            actual = contracts.get(name)
            if not isinstance(actual, Mapping):
                issues.append(f"{name}.contract")
                continue
            if actual.get("server_name") != name:
                issues.append(f"{name}.server_name")
            if actual.get("server_version") != expected:
                issues.append(f"{name}.server_version")

        compatibility_path = root / "plugins" / "azurpilot" / "compatibility.json"
        with compatibility_path.open(encoding="utf-8") as stream:
            compatibility = json.load(stream)
        dev_issues = contract_compatibility_issues(
            compatibility, contracts["azurpilot-dev"]
        )
        issues.extend(f"plugin.{issue}" for issue in dev_issues)
        game_issues = server_compatibility_issues(
            compatibility, contracts["azurpilot-game"]
        )
        issues.extend(f"plugin.game.{issue}" for issue in game_issues)
    except (
        OSError,
        TypeError,
        ValueError,
        KeyError,
        AttributeError,
        ImportError,
        json.JSONDecodeError,
    ) as exc:
        return {
            "status": "unavailable",
            "reason_code": "MCP_VERSION_GUARD_FAILED",
            "error_type": _safe_type_name(exc),
        }
    if issues:
        return {
            "status": "drift",
            "reason_code": "MCP_VERSION_GUARD_DRIFT",
            "issues": sorted(set(issues)),
        }
    return {"status": "ready", "reason_code": "MCP_VERSION_GUARD_READY"}


LocalProbe = Callable[[str, Path, str], Awaitable[dict[str, object]]]
RemoteProbe = Callable[[str], Awaitable[dict[str, object]]]
DockerProbe = Callable[[], dict[str, object]]
SemgrepProbe = Callable[[Path], Awaitable[dict[str, object]]]
DirectProbe = Callable[[str, Mapping[str, object]], Awaitable[dict[str, object]]]


def _split_remote_result(result: object) -> dict[str, object]:
    """Нормализовать legacy injected probe, не смешивая edge и backend."""

    if isinstance(result, Mapping) and (
        isinstance(result.get("remote_backend"), Mapping)
        or isinstance(result.get("public_edge"), Mapping)
    ):
        return dict(result)
    if isinstance(result, Mapping):
        legacy = dict(result)
        edge_status = "ready" if legacy.get("endpoint_up") is True else legacy.get("status")
        return {
            "remote_backend": legacy,
            "public_edge": {
                "status": edge_status,
                "reason_code": legacy.get(
                    "reason_code", "REMOTE_PUBLIC_EDGE_NOT_OBSERVABLE"
                ),
                "edge_reachable": edge_status == "ready",
                "evidence_kind": "public_edge_metadata",
            },
        }
    return {
        "remote_backend": {
            "status": "unavailable",
            "reason_code": "REMOTE_PROBE_PAYLOAD_INVALID",
        },
        "public_edge": {
            "status": "unavailable",
            "reason_code": "REMOTE_PROBE_PAYLOAD_INVALID",
            "edge_reachable": False,
        },
    }


def _timestamp_from_iso(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    timestamp = parsed.timestamp()
    return timestamp if timestamp >= 0 else None


def _record_failure(status_value: object, *, failures: list[str]) -> None:
    if status_value in {"drift", "invalid"}:
        failures.append("drift")
    elif status_value not in {"ready", "configured", "not_configured"}:
        failures.append("partial")


def _record_required_failure(
    status_value: object, *, expected: str, failures: list[str]
) -> None:
    if status_value == expected:
        return
    failures.append("drift" if status_value in {"drift", "invalid"} else "partial")


def _canonical_status(
    *,
    servers: Mapping[str, object],
    direct_routes: Mapping[str, object],
    docker: Mapping[str, object],
    version_guard: Mapping[str, object],
    working_tree: str,
    plugin: Mapping[str, object],
) -> tuple[str, str, bool]:
    """Оценить только canonical routes, отделяя внешнее Context7 evidence."""

    failures: list[str] = []
    external_evidence_pending = False
    if version_guard.get("status") != "ready":
        _record_failure(version_guard.get("status"), failures=failures)
    if working_tree != "clean":
        failures.append("partial")
    _record_required_failure(
        plugin.get("status"), expected="ready", failures=failures
    )

    for name in SERVER_NAMES:
        item = servers.get(name)
        if not isinstance(item, Mapping):
            failures.append("partial")
            continue
        local = item.get("local_direct")
        if not isinstance(local, Mapping) or local.get("status") != "ready":
            _record_required_failure(
                local.get("status") if isinstance(local, Mapping) else None,
                expected="ready",
                failures=failures,
            )
        codex = item.get("codex")
        _record_required_failure(
            codex.get("status") if isinstance(codex, Mapping) else None,
            expected="configured",
            failures=failures,
        )
        remote_backend = item.get("remote_backend")
        if isinstance(remote_backend, Mapping) and remote_backend.get(
            "status"
        ) not in {"ready", "not_configured"}:
            _record_failure(remote_backend.get("status"), failures=failures)

    for name, policy in MCP_ROUTE_POLICY.items():
        if name not in DIRECT_THIRD_PARTY_SERVERS:
            continue
        route = direct_routes.get(name)
        route_status = route.get("status") if isinstance(route, Mapping) else None
        if name == "context7":
            if (
                route_status == "not_observable"
                and route.get("reason_code")
                == "DIRECT_USER_SCOPED_ACCEPTANCE_EXTERNAL"
            ):
                external_evidence_pending = True
            elif route_status != "ready":
                _record_failure(route_status, failures=failures)
            continue
        if policy.get("collector_required") is True and route_status != "ready":
            _record_failure(route_status, failures=failures)

    docker_status = docker.get("canonical_status", docker.get("status"))
    if docker_status != "ready":
        _record_failure(docker_status, failures=failures)
    else:
        gateway_runtime = docker.get("gateway_runtime")
        required_gateway_ready = (
            gateway_runtime.get("required_runtime_ready") is True
            if isinstance(gateway_runtime, Mapping)
            else False
        )
        if not required_gateway_ready and isinstance(gateway_runtime, Mapping):
            runtime_servers = gateway_runtime.get("servers")
            required_gateway_ready = all(
                isinstance(runtime_servers, Mapping)
                and isinstance(runtime_servers.get(name), Mapping)
                and runtime_servers[name].get("runtime_ready") is True
                for name in GATEWAY_REQUIRED_SERVERS
            )
        if not required_gateway_ready:
            failures.append("partial")
        third_party = docker.get("third_party")
        if isinstance(third_party, Mapping):
            for name in GATEWAY_REQUIRED_SERVERS:
                item = third_party.get(name)
                if not isinstance(item, Mapping) or item.get("runtime_ready") is not True:
                    failures.append("partial")
    client = docker.get("client_connection")
    if not isinstance(client, Mapping) or client.get("status") != "configured":
        _record_failure(
            client.get("status") if isinstance(client, Mapping) else None,
            failures=failures,
        )

    if "drift" in failures:
        return "drift", "MCP_CANONICAL_ROUTE_DRIFT", external_evidence_pending
    if failures:
        return "partial", "MCP_CANONICAL_ROUTE_PARTIAL", external_evidence_pending
    return "ready", "MCP_CANONICAL_ROUTES_READY", external_evidence_pending


async def collect_status_async(
    root: Path | str | None = None,
    *,
    local_probe: LocalProbe | None = None,
    remote_probe: RemoteProbe | None = None,
    docker_probe: DockerProbe | None = None,
    semgrep_probe: SemgrepProbe | None = None,
    direct_probe: DirectProbe | None = None,
    now: Callable[[], str] = _utc_now,
) -> dict[str, object]:
    repository_root = Path(root or Path(__file__).resolve().parents[1]).resolve()
    try:
        expected_versions = load_server_versions(repository_root)
    except VersioningError:
        return {
            "schema_version": STATUS_SCHEMA_VERSION,
            "generated_at": now(),
            "status": "invalid",
            "reason_code": "MCP_VERSION_MANIFEST_INVALID",
        }
    revision, working_tree = _git_source_snapshot(repository_root)
    local = local_probe or (
        lambda name, path, _current_revision: _probe_local_stdio(name, root=path)
    )
    remote = remote_probe or _probe_remote
    direct = direct_probe or _probe_direct_route
    codex_config = _load_codex_config(repository_root)
    servers: dict[str, dict[str, object]] = {}
    for name in SERVER_NAMES:
        expected = expected_versions.get(name)
        if expected is None:
            servers[name] = {
                "status": "invalid",
                "reason_code": "MCP_SERVER_NOT_IN_MANIFEST",
            }
            continue
        try:
            local_result = await asyncio.wait_for(
                local(name, repository_root, revision), timeout=STATUS_TIMEOUT_SECONDS
            )
        except TimeoutError:
            local_result = {
                "status": "unavailable",
                "reason_code": "LOCAL_PROBE_TIMEOUT",
            }
        except Exception as exc:  # noqa: BLE001 - injected probes are untrusted.
            local_result = {
                "status": "unavailable",
                "reason_code": "LOCAL_PROBE_FAILED",
                "error_type": _safe_type_name(exc),
            }
        local_result = _surface_status(
            local_result,
            expected_version=expected,
            expected_revision=revision,
            working_tree=working_tree,
        )
        try:
            remote_result = await asyncio.wait_for(
                remote(name), timeout=STATUS_TIMEOUT_SECONDS
            )
        except TimeoutError:
            remote_result = {
                "status": "unavailable",
                "reason_code": "REMOTE_PROBE_TIMEOUT",
            }
        except Exception as exc:  # noqa: BLE001 - injected probes are untrusted.
            remote_result = {
                "status": "unavailable",
                "reason_code": "REMOTE_PROBE_FAILED",
                "error_type": _safe_type_name(exc),
            }
        remote_result = _split_remote_result(remote_result)
        remote_backend = remote_result.get("remote_backend")
        if isinstance(remote_backend, Mapping):
            remote_result = {
                **remote_result,
                "remote_backend": _surface_status(
                    remote_backend,
                    expected_version=expected,
                    expected_revision=revision,
                    working_tree=working_tree,
                    source_mode="remote",
                ),
            }
        codex_result = _codex_entry_status(
            codex_config,
            name,
            expected_command="uv",
            expected_args=CODEX_SERVER_ARGS[name],
            expected_startup_timeout_sec=CODEX_SERVER_TIMEOUTS[name][0],
            expected_tool_timeout_sec=CODEX_SERVER_TIMEOUTS[name][1],
            expected_required=False,
        )
        if codex_result["status"] == "configured":
            codex_result = {
                **codex_result,
                "runtime_reachable": False,
                "runtime_ready": False,
                "evidence_kind": "configuration_only",
            }
        servers[name] = {
            "expected_version": expected,
            "expected": {
                "server_version": expected,
                "source_revision": revision,
                "working_tree": working_tree,
            },
            "local_direct": local_result,
            "codex": codex_result,
            "remote_backend": remote_result.get("remote_backend", {}),
            "public_edge": remote_result.get("public_edge", {}),
        }
    semgrep = semgrep_probe or _probe_semgrep_local_mcp
    try:
        semgrep_result = await asyncio.wait_for(
            semgrep(repository_root), timeout=SEMGREP_PROBE_TOTAL_TIMEOUT_SECONDS
        )
    except TimeoutError:
        semgrep_result = {
            "status": "unavailable",
            "reason_code": "SEMGREP_LOCAL_PROBE_TIMEOUT",
        }
    except Exception as exc:  # noqa: BLE001 - status boundary hides subprocess details.
        semgrep_result = {
            "status": "unavailable",
            "reason_code": "SEMGREP_LOCAL_PROBE_FAILED",
            "error_type": _safe_type_name(exc),
        }
    direct_routes: dict[str, dict[str, object]] = {}
    for name in DIRECT_THIRD_PARTY_SERVERS:
        route_policy = _route_policy(name)
        if name == "semgrep":
            direct_routes[name] = {
                **semgrep_result,
                "canonical_route": route_policy.get("canonical_route", "unknown"),
                "gateway_required": route_policy.get("gateway_required") is True,
                "collector_required": route_policy.get("collector_required") is True,
                "required_runtime": route_policy.get("required_runtime") is True,
            }
            continue
        try:
            direct_routes[name] = await asyncio.wait_for(
                direct(name, codex_config), timeout=STATUS_TIMEOUT_SECONDS
            )
        except TimeoutError:
            direct_routes[name] = {
                "status": "unavailable",
                "reason_code": "DIRECT_ROUTE_PROBE_TIMEOUT",
            }
        except Exception as exc:  # noqa: BLE001 - injected probes are untrusted.
            direct_routes[name] = {
                "status": "unavailable",
                "reason_code": "DIRECT_ROUTE_PROBE_FAILED",
                "error_type": _safe_type_name(exc),
            }
        direct_routes[name] = {
            **direct_routes[name],
            "canonical_route": route_policy.get("canonical_route", "unknown"),
            "gateway_required": route_policy.get("gateway_required") is True,
            "collector_required": route_policy.get("collector_required") is True,
            "required_runtime": route_policy.get("required_runtime") is True,
            "live_acceptance_required": route_policy.get("live_acceptance_required")
            is True,
        }
    version_guard = _version_guard(repository_root, expected_versions)
    plugin = _codex_plugin_status(repository_root)
    try:
        docker = await asyncio.wait_for(
            asyncio.to_thread(docker_probe or _docker_status),
            timeout=DOCKER_PROBE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        docker = {
            "status": "unavailable",
            "reason_code": "DOCKER_PROBE_TIMEOUT",
        }
    except Exception as exc:  # noqa: BLE001 - status boundary hides subprocess details.
        docker = {
            "status": "unavailable",
            "reason_code": "DOCKER_PROBE_FAILED",
            "error_type": _safe_type_name(exc),
        }
    docker_client = _codex_entry_status(
        codex_config,
        DOCKER_CLIENT_PROFILE_NAME,
        expected_command=DOCKER_CLIENT_COMMAND,
        expected_args=DOCKER_CLIENT_ARGS,
    )
    docker = {
        **docker,
        "codex": docker_client,
        "client_connection": docker_client,
    }
    chatgpt = {
        "status": "not_observable",
        "reason_code": "CHATGPT_ACTION_SNAPSHOT_NOT_OBSERVABLE",
    }
    canonical_status, canonical_reason, external_evidence_pending = _canonical_status(
        servers=servers,
        direct_routes=direct_routes,
        docker=docker,
        version_guard=version_guard,
        working_tree=working_tree,
        plugin=plugin,
    )
    status = (
        "drift"
        if canonical_status == "drift"
        else "partial"
        if canonical_status != "ready" or external_evidence_pending
        else "ready"
    )
    generated_at = now()
    probe = {
        "attempted_at": generated_at,
        "last_successful_probe_timestamp_seconds": _timestamp_from_iso(generated_at)
        if canonical_status == "ready"
        else None,
    }
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": status,
        "reason_code": "MCP_STATUS_READY"
        if status == "ready"
        else "MCP_STATUS_DRIFT"
        if status == "drift"
        else "MCP_STATUS_PARTIAL",
        "canonical_status": canonical_status,
        "canonical_reason_code": canonical_reason,
        "external_evidence_pending": external_evidence_pending,
        "source": {"revision": revision, "working_tree": working_tree},
        "plugin": plugin,
        "servers": servers,
        "route_policy": {
            name: _route_policy(name) for name in MCP_ROUTE_POLICY
        },
        "direct_routes": direct_routes,
        "version_guard": version_guard,
        "docker_mcp": docker,
        "semgrep_mcp": semgrep_result,
        "probe": probe,
        "chatgpt": chatgpt,
    }


def collect_status(
    root: Path | str | None = None, **kwargs: object
) -> dict[str, object]:
    """Синхронный bounded entrypoint для CLI и тестов."""

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


def status_metric_samples(report: Mapping[str, object]) -> tuple[MetricSample, ...]:
    """Построить bounded samples без SHA, URL, токенов и payload."""

    samples: list[MetricSample] = []
    servers = report.get("servers")
    if isinstance(servers, Mapping):
        for server_name, value in servers.items():
            if not isinstance(server_name, str) or not _SAFE_IDENTIFIER.fullmatch(
                server_name
            ):
                continue
            if not isinstance(value, Mapping):
                continue
            expected_version = value.get("expected_version")
            if not isinstance(expected_version, str) or not _VERSION_RE.fullmatch(
                expected_version
            ):
                continue
            for surface_name in (
                "local_direct",
                "codex",
                "remote_backend",
                "public_edge",
            ):
                surface = value.get(surface_name)
                if not isinstance(surface, Mapping):
                    continue
                status = surface.get("status")
                protocol = surface.get("protocol_version")
                observed_version = surface.get("server_version")
                attributes = {
                    "server": server_name,
                    "surface": surface_name,
                    "version": expected_version,
                    "protocol": protocol
                    if isinstance(protocol, str) and _SAFE_TOKEN.fullmatch(protocol)
                    else "unknown",
                    "required_runtime": "1"
                    if surface_name == "local_direct"
                    else "0",
                }
                configured = status not in {"not_configured", None}
                reachable = surface.get("runtime_reachable") is True or (
                    surface_name == "public_edge"
                    and surface.get("edge_reachable") is True
                )
                runtime_ready = surface.get("runtime_ready") is True or (
                    surface_name == "local_direct" and status == "ready"
                )
                endpoint_up = reachable
                samples.append(
                    MetricSample(
                        "azurpilot_mcp_surface_configured",
                        1.0 if configured else 0.0,
                        attributes,
                    )
                )
                samples.append(
                    MetricSample(
                        "azurpilot_mcp_endpoint_up",
                        1.0 if endpoint_up else 0.0,
                        attributes,
                    )
                )
                samples.append(
                    MetricSample(
                        "azurpilot_mcp_surface_reachable",
                        1.0 if reachable else 0.0,
                        attributes,
                    )
                )
                samples.append(
                    MetricSample(
                        "azurpilot_mcp_surface_runtime_ready",
                        1.0 if runtime_ready else 0.0,
                        attributes,
                    )
                )
                if isinstance(observed_version, str) and _VERSION_RE.fullmatch(
                    observed_version
                ):
                    observed_attributes = {
                        **attributes,
                        "version": observed_version.removeprefix("v"),
                    }
                    samples.append(
                        MetricSample(
                            "azurpilot_mcp_observed_version_info",
                            1.0,
                            observed_attributes,
                        )
                    )
                    samples.append(
                        MetricSample(
                            "azurpilot_mcp_version_info",
                            1.0,
                            observed_attributes,
                        )
                    )
                samples.append(
                    MetricSample(
                        "azurpilot_mcp_expected_version_info",
                        1.0,
                        attributes,
                    )
                )
                samples.append(
                    MetricSample(
                        "azurpilot_mcp_version_drift",
                        1.0
                        if surface.get("version_status") == "drift"
                        or surface.get("source_status") == "drift"
                        else 0.0,
                        attributes,
                    )
                )
    direct_routes = report.get("direct_routes")
    if isinstance(direct_routes, Mapping):
        for server_name, route in direct_routes.items():
            if (
                not isinstance(server_name, str)
                or server_name not in DIRECT_THIRD_PARTY_SERVERS
                or not isinstance(route, Mapping)
            ):
                continue
            status = route.get("status")
            attributes = {
                "server": server_name,
                "surface": "canonical_direct",
                "version": "unknown",
                "protocol": "unknown",
                "required_runtime": "1"
                if route.get("required_runtime") is True
                else "0",
            }
            reachable = route.get("runtime_reachable") is True
            ready = route.get("runtime_ready") is True or status == "ready"
            samples.extend(
                (
                    MetricSample(
                        "azurpilot_mcp_surface_configured",
                        1.0 if status not in {None, "not_configured"} else 0.0,
                        attributes,
                    ),
                    MetricSample(
                        "azurpilot_mcp_endpoint_up",
                        1.0 if reachable else 0.0,
                        attributes,
                    ),
                    MetricSample(
                        "azurpilot_mcp_surface_reachable",
                        1.0 if reachable else 0.0,
                        attributes,
                    ),
                    MetricSample(
                        "azurpilot_mcp_surface_runtime_ready",
                        1.0 if ready else 0.0,
                        attributes,
                    ),
                )
            )
    docker = report.get("docker_mcp")
    third_party = docker.get("third_party") if isinstance(docker, Mapping) else None
    if isinstance(third_party, Mapping):
        for server_name, value in third_party.items():
            if (
                not isinstance(server_name, str)
                or server_name not in THIRD_PARTY_SERVERS
            ):
                continue
            configured = isinstance(value, Mapping) and value.get("configured") is True
            reachable = isinstance(value, Mapping) and value.get(
                "runtime_reachable"
            ) is True
            ready = isinstance(value, Mapping) and value.get("runtime_ready") is True
            attributes = {
                "server": server_name,
                "surface": "docker_gateway",
                "version": "unknown",
                "protocol": "unknown",
                "required_runtime": "1"
                if isinstance(value, Mapping)
                and value.get("gateway_required") is True
                else "0",
            }
            samples.append(
                MetricSample(
                    "azurpilot_mcp_surface_configured",
                    1.0 if configured else 0.0,
                    attributes,
                )
            )
            samples.append(
                MetricSample(
                    "azurpilot_mcp_surface_reachable",
                    1.0 if reachable else 0.0,
                    attributes,
                )
            )
            samples.append(
                MetricSample(
                    "azurpilot_mcp_surface_runtime_ready",
                    1.0 if ready else 0.0,
                    attributes,
                )
            )
            samples.append(
                MetricSample(
                    "azurpilot_mcp_gateway_server_up",
                    1.0 if ready else 0.0,
                    attributes,
                )
            )
    docker_version = docker.get("version") if isinstance(docker, Mapping) else None
    if isinstance(docker_version, str) and _VERSION_RE.fullmatch(docker_version):
        samples.append(
            MetricSample(
                "azurpilot_mcp_observed_version_info",
                1.0,
                {
                    "server": "docker-gateway",
                    "surface": "docker_gateway",
                    "version": docker_version.removeprefix("v"),
                    "protocol": "unknown",
                },
            )
        )
    probe = report.get("probe")
    successful_timestamp = (
        probe.get("last_successful_probe_timestamp_seconds")
        if isinstance(probe, Mapping)
        else None
    )
    if isinstance(successful_timestamp, (int, float)) and successful_timestamp >= 0:
        samples.append(
            MetricSample(
                "azurpilot_mcp_last_successful_probe_timestamp_seconds",
                float(successful_timestamp),
                {},
            )
        )
    for sample in samples:
        if (
            not _METRIC_NAME_RE.fullmatch(sample.name)
            or set(sample.attributes) - _METRIC_ATTRIBUTE_NAMES
        ):
            raise StatusError("MCP_METRIC_ATTRIBUTES_INVALID")
    return tuple(samples)


def _metrics_endpoint() -> str | None:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "").strip()
    if endpoint:
        return endpoint
    endpoint = os.environ.get("AZURPILOT_OBSERVABILITY_OTLP_ENDPOINT", "").strip()
    if endpoint:
        return endpoint.rstrip("/") + "/v1/metrics"
    generic = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    return generic.rstrip("/") + "/v1/metrics" if generic else None


@dataclass(frozen=True, slots=True)
class MetricEmission:
    emitted: bool
    reason_code: str
    sample_count: int


def emit_metrics(report: Mapping[str, object]) -> MetricEmission:
    """Однократно отправить status samples через существующий OTel/Alloy path."""

    try:
        samples = status_metric_samples(report)
    except StatusError as exc:
        return MetricEmission(False, exc.code, 0)
    endpoint = _metrics_endpoint()
    if not endpoint:
        return MetricEmission(False, "MCP_METRICS_ENDPOINT_UNCONFIGURED", len(samples))
    try:
        from module.observability.metrics import emit_metric_samples_once

        flushed = emit_metric_samples_once(
            samples,
            endpoint=endpoint,
            timeout_millis=int(METRICS_TIMEOUT_SECONDS * 1000),
            repository_root=Path(__file__).resolve().parents[1],
        )
    except Exception:  # noqa: BLE001 - status command must not expose headers/errors.
        return MetricEmission(False, "MCP_METRICS_EXPORT_FAILED", len(samples))
    return MetricEmission(
        flushed,
        "MCP_METRICS_EXPORTED" if flushed else "MCP_METRICS_EXPORT_FAILED",
        len(samples),
    )


def _strict_failure(
    report: Mapping[str, object], emission: MetricEmission | None
) -> bool:
    canonical_status = report.get("canonical_status", report.get("status"))
    if canonical_status in {"drift", "invalid"}:
        return True
    version_guard = report.get("version_guard")
    if not isinstance(version_guard, Mapping) or version_guard.get("status") != "ready":
        return True
    source = report.get("source")
    if not isinstance(source, Mapping) or source.get("working_tree") != "clean":
        return True
    plugin = report.get("plugin")
    if not isinstance(plugin, Mapping) or plugin.get("status") != "ready":
        return True
    servers = report.get("servers")
    if isinstance(servers, Mapping):
        for name in SERVER_NAMES:
            item = servers.get(name)
            if not isinstance(item, Mapping):
                return True
            codex = item.get("codex")
            if not isinstance(codex, Mapping) or codex.get("status") != "configured":
                return True
            local = item.get("local_direct")
            if not isinstance(local, Mapping) or local.get("status") != "ready":
                return True
            remote_backend = item.get("remote_backend")
            if not isinstance(remote_backend, Mapping):
                return True
            if remote_backend.get("status") not in {"ready", "not_configured"}:
                return True
    else:
        return True
    direct_routes = report.get("direct_routes")
    if isinstance(direct_routes, Mapping):
        for name in ("docker-docs", "semgrep"):
            route = direct_routes.get(name)
            if not isinstance(route, Mapping) or route.get("status") != "ready":
                return True
        context7 = direct_routes.get("context7")
        if not isinstance(context7, Mapping):
            return True
        if context7.get("status") != "ready" and not (
            context7.get("status") == "not_observable"
            and context7.get("reason_code") == "DIRECT_USER_SCOPED_ACCEPTANCE_EXTERNAL"
        ):
            return True
    else:
        semgrep = report.get("semgrep_mcp")
        if not isinstance(semgrep, Mapping) or semgrep.get("status") != "ready":
            return True
        if semgrep.get("scan_tool") not in SEMGREP_LOCAL_SCAN_TOOLS:
            return True
    docker = report.get("docker_mcp")
    if isinstance(docker, Mapping):
        docker_status = docker.get("canonical_status", docker.get("status"))
        if docker_status != "ready":
            return True
        profile_config = docker.get("profile_config")
        profile_status = (
            profile_config.get("canonical_status")
            if isinstance(profile_config, Mapping)
            else None
        )
        if profile_status is None:
            profile_status = (
                profile_config.get("status")
                if isinstance(profile_config, Mapping)
                else None
            )
        if profile_status != "ready":
            return True
        gateway_runtime = docker.get("gateway_runtime")
        required_runtime_ready = (
            gateway_runtime.get("required_runtime_ready") is True
            if isinstance(gateway_runtime, Mapping)
            else False
        )
        if not required_runtime_ready and isinstance(gateway_runtime, Mapping):
            required_runtime_ready = all(
                isinstance(gateway_runtime.get("servers"), Mapping)
                and isinstance(gateway_runtime["servers"].get(name), Mapping)
                and gateway_runtime["servers"][name].get("runtime_ready") is True
                for name in GATEWAY_REQUIRED_SERVERS
            )
        if not required_runtime_ready:
            return True
        client_connection = docker.get("client_connection")
        if not isinstance(client_connection, Mapping) or client_connection.get(
            "status"
        ) != "configured":
            return True
        third_party = docker.get("third_party")
        if not isinstance(third_party, Mapping):
            return True
        for name in GATEWAY_REQUIRED_SERVERS:
            item = third_party.get(name)
            if not isinstance(item, Mapping):
                return True
            if item.get("configured") is not True:
                return True
            if item.get("runtime_reachable") is not True:
                return True
            if item.get("runtime_ready") is not True or item.get("status") != "ready":
                return True
            policy = item.get("read_only_policy")
            if not isinstance(policy, Mapping) or policy.get("status") != "ready":
                return True
    else:
        return True
    return emission is not None and not emission.emitted


_HUMAN_STATUS_LABELS = {
    "ready": "OK",
    "configured": "OK",
    "partial": "PARTIAL",
    "degraded": "DEGRADED",
    "drift": "DRIFT",
    "invalid": "INVALID",
    "missing": "MISSING",
    "not_configured": "NOT CONFIGURED",
    "not_observable": "UNKNOWN",
    "unavailable": "UNAVAILABLE",
}


def _human_status_label(value: object) -> str:
    if not isinstance(value, str):
        return "UNKNOWN"
    return _HUMAN_STATUS_LABELS.get(value, value.upper().replace("_", " "))


def _human_reason(value: object) -> str:
    return (
        value if isinstance(value, str) and _SAFE_TOKEN.fullmatch(value) else "UNKNOWN"
    )


def _human_surface_cell(surface: object, *, expected_version: object = None) -> str:
    if not isinstance(surface, Mapping):
        return "UNKNOWN"
    surface_status = surface.get("status")
    if surface_status in {"ready", "configured"}:
        version = surface.get("server_version")
        if isinstance(version, str) and _VERSION_RE.fullmatch(version):
            return f"{version} OK"
        return "OK"
    if (
        surface_status in {"drift", "partial"}
        and surface.get("source_status") == "modified"
    ):
        version = surface.get("server_version") or expected_version
        if isinstance(version, str) and _VERSION_RE.fullmatch(version):
            return f"{version} MODIFIED"
    return _human_status_label(surface_status)


def _human_protocol(item: Mapping[str, object]) -> str:
    surface = item.get("local_direct")
    protocol = surface.get("protocol_version") if isinstance(surface, Mapping) else None
    return (
        protocol
        if isinstance(protocol, str) and _SAFE_TOKEN.fullmatch(protocol)
        else "UNKNOWN"
    )


def _print_human_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    print(
        "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    )
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def _print_human_notes(notes: Sequence[str]) -> None:
    if not notes:
        return
    print()
    print("Notes")
    print("-----")
    for note in notes:
        print(f"- {note}")


def _print_human(report: Mapping[str, object], emission: MetricEmission | None) -> None:
    print("AzurPilot MCP Status")
    print("====================")

    source = report.get("source")
    source_revision = (
        source.get("revision", UNKNOWN_SOURCE_REVISION)
        if isinstance(source, Mapping)
        else UNKNOWN_SOURCE_REVISION
    )
    source_revision = (
        source_revision[:12]
        if isinstance(source_revision, str)
        and source_revision != UNKNOWN_SOURCE_REVISION
        else UNKNOWN_SOURCE_REVISION
    )
    working_tree = (
        source.get("working_tree", "unknown")
        if isinstance(source, Mapping)
        else "unknown"
    )
    version_guard = report.get("version_guard")
    print(
        f"OVERALL   {_human_status_label(report.get('status'))} "
        f"({_human_reason(report.get('reason_code'))})"
    )
    canonical_status = report.get("canonical_status")
    if canonical_status is not None:
        print(
            f"CANONICAL {_human_status_label(canonical_status)} "
            f"({_human_reason(report.get('canonical_reason_code'))})"
        )
    print(f"SOURCE    {source_revision} ({working_tree})")
    if isinstance(version_guard, Mapping):
        print(
            f"VERSION   {_human_status_label(version_guard.get('status'))} "
            f"({_human_reason(version_guard.get('reason_code'))})"
        )
    plugin = report.get("plugin")
    if isinstance(plugin, Mapping):
        print(
            f"PLUGIN    {_human_status_label(plugin.get('status'))} "
            f"({_human_reason(plugin.get('reason_code'))})"
        )

    rows: list[list[str]] = []
    notes: list[str] = []
    servers = report.get("servers")
    if isinstance(servers, Mapping):
        for name, item in servers.items():
            if not isinstance(name, str) or not isinstance(item, Mapping):
                continue
            expected_version = item.get("expected_version", "unknown")
            expected = item.get("expected")
            expected_source = (
                expected.get("source_revision")
                if isinstance(expected, Mapping)
                else source_revision
            )
            if not isinstance(expected_source, str):
                expected_source = UNKNOWN_SOURCE_REVISION
            expected_source = (
                expected_source[:12]
                if expected_source != UNKNOWN_SOURCE_REVISION
                else UNKNOWN_SOURCE_REVISION
            )
            local = item.get("local_direct")
            expected_cell = f"{expected_version} / {expected_source}"
            local_cell = _human_surface_cell(local, expected_version=expected_version)
            codex_cell = _human_surface_cell(
                item.get("codex"), expected_version=expected_version
            )
            backend_cell = _human_surface_cell(
                item.get("remote_backend"), expected_version=expected_version
            )
            edge_cell = _human_surface_cell(
                item.get("public_edge"), expected_version=expected_version
            )
            rows.append(
                [
                    name,
                    expected_cell,
                    local_cell,
                    codex_cell,
                    backend_cell,
                    edge_cell,
                    _human_protocol(item),
                ]
            )
            for surface_name, label in (
                ("local_direct", "Local/direct"),
                ("codex", "Codex path"),
                ("remote_backend", "Remote backend"),
                ("public_edge", "Public edge"),
            ):
                surface = item.get(surface_name)
                if isinstance(surface, Mapping) and surface.get("status") not in {
                    "ready",
                    "configured",
                }:
                    surface_cell = _human_surface_cell(
                        surface, expected_version=expected_version
                    )
                    notes.append(
                        f"{name} / {label}: "
                        f"{surface_cell} "
                        f"({_human_reason(surface.get('reason_code'))})"
                    )

    print()
    _print_human_table(
        (
            "SERVER",
            "EXPECTED/SOURCE",
            "LOCAL/DIRECT",
            "CODEX PATH",
            "REMOTE BACKEND",
            "PUBLIC EDGE",
            "PROTOCOL",
        ),
        rows,
    )

    direct_routes = report.get("direct_routes")
    if isinstance(direct_routes, Mapping):
        route_rows = []
        for name in DIRECT_THIRD_PARTY_SERVERS:
            route = direct_routes.get(name)
            if not isinstance(route, Mapping):
                continue
            route_rows.append(
                [
                    name,
                    str(route.get("canonical_route", "unknown")),
                    "YES" if route.get("required_runtime") is True else "NO",
                    _human_status_label(route.get("status")),
                ]
            )
        if route_rows:
            print()
            print("Canonical direct routes")
            print("-----------------------")
            _print_human_table(("SERVER", "ROUTE", "REQUIRED", "STATUS"), route_rows)

    docker = report.get("docker_mcp")
    if isinstance(docker, Mapping):
        print()
        print("Docker MCP Gateway")
        print("------------------")
        profile_id = docker.get("profile_id", CANONICAL_DOCKER_PROFILE_ID)
        profile_name = docker.get("profile_name", CANONICAL_DOCKER_PROFILE_NAME)
        print(f"Version: {docker.get('version', 'UNKNOWN')}")
        print(f"Profile: {profile_id} ({profile_name})")
        print(f"Status: {_human_status_label(docker.get('status'))}")
        profile_config = docker.get("profile_config")
        if isinstance(profile_config, Mapping):
            print(
                "Profile config: "
                f"{_human_status_label(profile_config.get('status'))} "
                f"({_human_reason(profile_config.get('reason_code'))})"
            )
        gateway_runtime = docker.get("gateway_runtime")
        if isinstance(gateway_runtime, Mapping):
            print(
                "Gateway runtime: "
                f"{_human_status_label(gateway_runtime.get('status'))} "
                f"({_human_reason(gateway_runtime.get('reason_code'))})"
            )
        client_connection = docker.get("client_connection")
        if isinstance(client_connection, Mapping):
            print(
                "Client connection: "
                f"{_human_status_label(client_connection.get('status'))} "
                f"({_human_reason(client_connection.get('reason_code'))})"
            )
        server_count = docker.get("server_count")
        if isinstance(server_count, int):
            print(f"Servers: {server_count}/{len(THIRD_PARTY_SERVERS)}")

        third_party = docker.get("third_party")
        if isinstance(third_party, Mapping):
            docker_rows: list[list[str]] = []
            for name in THIRD_PARTY_SERVERS:
                item = third_party.get(name)
                if not isinstance(item, Mapping):
                    continue
                profile_item = item.get("profile_config")
                runtime_item = item.get("gateway_runtime")
                policy_item = item.get("read_only_policy")
                policy = (
                    _human_status_label(policy_item.get("status"))
                    if isinstance(policy_item, Mapping)
                    else "UNKNOWN"
                )
                tool_count = (
                    runtime_item.get("tool_count")
                    if isinstance(runtime_item, Mapping)
                    else None
                )
                tools = (
                    f"{tool_count} tools"
                    if isinstance(tool_count, int)
                    and isinstance(runtime_item, Mapping)
                    and runtime_item.get("tools_observable") is not False
                    else "catalog unknown"
                )
                docker_rows.append(
                    [
                        name,
                        str(item.get("canonical_route", "unknown")),
                        "YES" if item.get("gateway_required") is True else "NO",
                        _human_status_label(
                            profile_item.get("status")
                            if isinstance(profile_item, Mapping)
                            else "unknown"
                        ),
                        _human_status_label(
                            runtime_item.get("status")
                            if isinstance(runtime_item, Mapping)
                            else "unknown"
                        ),
                        policy,
                        tools,
                    ]
                )
                if item.get("runtime_ready") is not True:
                    notes.append(
                        f"Docker {name}: "
                        f"{_human_status_label(item.get('status'))} "
                        f"({_human_reason(item.get('reason_code'))})"
                    )
            if docker_rows:
                print()
                _print_human_table(
                    (
                        "SERVER",
                        "ROUTE",
                        "REQUIRED",
                        "PROFILE CONFIG",
                        "GATEWAY RUNTIME",
                        "POLICY",
                        "TOOLS",
                    ),
                    docker_rows,
                )

        secret_engine = docker.get("secret_engine")
        if isinstance(secret_engine, Mapping):
            print()
            print("Secrets (auxiliary, not a canonical readiness gate)")
            print("-------------------------------------------------")
            for label, key in (
                ("Secret store", "secret_store"),
                ("CLI", "cli_status"),
                ("Keychain", "keychain_status"),
                ("Engine RPC", "rpc_status"),
                ("Runtime injection", "container_runtime_secret_injection"),
                ("Gateway injection", "gateway_secret_injection"),
                ("Host pass", "host_pass_resolution"),
            ):
                value = secret_engine.get(key)
                status_value = (
                    value.get("status") if isinstance(value, Mapping) else value
                )
                print(f"{label + ':':20} {_human_status_label(status_value)}")
                if status_value not in {"ready", "configured"}:
                    reason = (
                        value.get("reason_code")
                        if isinstance(value, Mapping)
                        else secret_engine.get("reason_code")
                    )
                    notes.append(
                        f"Secrets {label}: {_human_status_label(status_value)} "
                        f"({_human_reason(reason)})"
                    )

    chatgpt = report.get("chatgpt")
    if isinstance(chatgpt, Mapping):
        chatgpt_status = _human_status_label(chatgpt.get("status"))
        print()
        print("Кэш действий ChatGPT (только удалённый маршрут)")
        print("-----------------------------------------------")
        print(f"{'AzurPilot Development (удалённый):':36} {chatgpt_status}")
        print(f"{'AzurPilot Game (удалённый):':36} {chatgpt_status}")
        if chatgpt_status != "OK":
            notes.append(
                f"Удалённый кэш действий ChatGPT: {chatgpt_status} "
                f"({_human_reason(chatgpt.get('reason_code'))})"
            )

    if emission is not None:
        print()
        print("Metrics")
        print("-------")
        print(
            f"Status: {_human_status_label('ready' if emission.emitted else 'unavailable')}"
        )
        print(f"Samples: {emission.sample_count}")
        if not emission.emitted:
            notes.append(f"Metrics: {_human_reason(emission.reason_code)}")

    _print_human_notes(notes)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only status и drift check MCP surfaces AzurPilot."
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="Вывести bounded JSON."
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Вернуть ненулевой код при drift/unavailable.",
    )
    parser.add_argument(
        "--emit-metrics",
        action="store_true",
        help="Однократно отправить status metrics через OTel.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Повторять bounded status probe до остановки процесса.",
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=60.0,
        help="Интервал --watch в секундах (10..3600).",
    )
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if not (
        MCP_STATUS_WATCH_MIN_INTERVAL_SECONDS
        <= arguments.interval_seconds
        <= MCP_STATUS_WATCH_MAX_INTERVAL_SECONDS
    ):
        print(
            "Интервал --watch должен быть от "
            f"{MCP_STATUS_WATCH_MIN_INTERVAL_SECONDS:g} до "
            f"{MCP_STATUS_WATCH_MAX_INTERVAL_SECONDS:g} секунд."
        )
        return 2

    def run_once() -> tuple[dict[str, object], MetricEmission | None]:
        report = collect_status(arguments.repository_root)
        emission = emit_metrics(report) if arguments.emit_metrics else None
        if emission is not None:
            report = {
                **report,
                "metrics": {
                    "emitted": emission.emitted,
                    "reason_code": emission.reason_code,
                    "sample_count": emission.sample_count,
                },
            }
        return report, emission

    if not arguments.watch:
        report, emission = run_once()
        if arguments.as_json:
            print(
                json.dumps(
                    report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
            )
        else:
            _print_human(report, emission)
        return 2 if arguments.strict and _strict_failure(report, emission) else 0

    last_failure = False
    try:
        while True:
            report, emission = run_once()
            last_failure = _strict_failure(report, emission) if arguments.strict else False
            if arguments.as_json:
                print(
                    json.dumps(
                        report,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
            else:
                _print_human(report, emission)
                print()
            time.sleep(arguments.interval_seconds)
    except KeyboardInterrupt:
        return 2 if arguments.strict and last_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
