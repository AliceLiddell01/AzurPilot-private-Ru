"""Read-only диагностика MCP surfaces и canonical Docker MCP profile.

Команда намеренно не вызывает mutating MCP tools, не печатает окружение и не
сохраняет ответы внешних endpoint-ов. Docker Toolkit используется только для
``version`` и ``profile list``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
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
METRICS_TIMEOUT_SECONDS = 5.0
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
_MAX_JSON_BYTES = 64 * 1024
_MAX_TOOLS = 256

SERVER_NAMES = ("azurpilot-dev", "azurpilot-game")
SERVER_MODULES = {
    "azurpilot-dev": ("module.dev_mcp", "dev_get_contract"),
    "azurpilot-game": ("module.game_mcp", "game_get_contract"),
}
THIRD_PARTY_SERVERS = (
    "grafana",
    "context7",
    "docker-docs",
    "dockerhub",
    "semgrep",
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


class StatusError(RuntimeError):
    """Безопасная ошибка status collector с machine-readable кодом."""

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


def _child_environment(revision: str) -> dict[str, str]:
    environment = dict(os.environ)
    if revision != UNKNOWN_SOURCE_REVISION:
        environment["AZURPILOT_SOURCE_REVISION"] = revision
    else:
        environment.pop("AZURPILOT_SOURCE_REVISION", None)
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
    server_name: str, *, root: Path, revision: str
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
        env=_child_environment(revision),
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


def _safe_remote_url(value: str) -> tuple[str, str] | None:
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
    metadata = f"{origin}/.well-known/oauth-protected-resource/mcp"
    return origin, metadata


def _remote_metadata_request(url: str) -> tuple[str, str]:
    parsed = _safe_remote_url(url)
    if parsed is None:
        return "unavailable", "REMOTE_URL_INVALID"
    _origin, metadata_url = parsed
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
    return "not_observable", "REMOTE_STATUS_TOKEN_UNAVAILABLE"


async def _probe_remote(server_name: str) -> dict[str, object]:
    env_name = (
        f"AZURPILOT_{server_name.removeprefix('azurpilot-').upper()}_MCP_PUBLIC_URL"
    )
    url = os.environ.get(env_name, "").strip()
    if not url:
        return {
            "status": "not_configured",
            "reason_code": "REMOTE_PUBLIC_URL_NOT_CONFIGURED",
            "endpoint_up": False,
        }
    status, code = await asyncio.to_thread(_remote_metadata_request, url)
    return {
        "status": status,
        "reason_code": code,
        "endpoint_up": status != "unavailable",
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
    ):
        return {"status": "drift", "reason_code": "CODEX_SERVER_CONFIG_DRIFT"}
    return {"status": "configured", "reason_code": "CODEX_SERVER_CONFIGURED"}


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


def _docker_server_status(server: Mapping[str, object]) -> dict[str, object]:
    name = _docker_server_name(server)
    if name is None:
        return {"status": "invalid", "reason_code": "DOCKER_SERVER_ID_INVALID"}
    tools = _bounded_tool_names(server.get("tools"))
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
    server_type = server.get("type")
    tools_observable = bool(tools)
    read_only = not writes and (
        disable_write if name == "grafana" else allowlist_status != "drift"
    )
    if server_type == "remote" and not tools:
        read_only = True
    return {
        "status": "ready"
        if read_only and allowlist_status != "drift" and tools_observable
        else "not_observable"
        if read_only and allowlist_status != "drift"
        else "drift",
        "reason_code": "DOCKER_SERVER_READ_ONLY"
        if read_only and tools_observable
        else "DOCKER_SERVER_TOOLS_NOT_OBSERVABLE"
        if read_only
        else "DOCKER_SERVER_WRITE_OR_ALLOWLIST_DRIFT",
        "tool_count": len(tools),
        "write_tools_exposed": writes,
        "read_only": read_only,
        "allowlist_status": allowlist_status,
        "image_pinned": isinstance(server.get("image"), str)
        and "@sha256:" in str(server.get("image")),
        "disable_write": disable_write,
        "tools_observable": tools_observable,
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
            "reason_code": code,
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
            "third_party": {},
            "secret_engine": secret_engine,
        }
    try:
        validate_development_profile(profile)
    except StatusError as exc:
        return {
            "status": "drift",
            "reason_code": exc.code,
            "version": version,
            "profile_id": CANONICAL_DOCKER_PROFILE_ID,
            "secret_engine": secret_engine,
        }
    raw_servers = profile.get("servers")
    if not isinstance(raw_servers, list):
        return {
            "status": "invalid",
            "reason_code": "DOCKER_PROFILE_SERVERS_INVALID",
            "version": version,
            "profile_id": CANONICAL_DOCKER_PROFILE_ID,
        }
    server_statuses: dict[str, dict[str, object]] = {}
    for item in raw_servers:
        if isinstance(item, Mapping):
            name = _docker_server_name(item)
            if name is not None:
                server_statuses[name] = _docker_server_status(item)
    profile_name = profile.get("name")
    exact_servers = set(server_statuses) == _PROFILE_SERVER_SET
    third_party = {
        name: server_statuses.get(
            name,
            {"status": "missing", "reason_code": "DOCKER_SERVER_NOT_PRESENT"},
        )
        for name in THIRD_PARTY_SERVERS
    }
    secret_store = secret_engine.get("secret_store")
    secret_store_ready = (
        isinstance(secret_store, Mapping)
        and secret_store.get("status") == "ready"
    )
    profile_status = all(
        item.get("status") == "ready" for item in third_party.values()
    ) and secret_store_ready
    profile_partial = exact_servers and all(
        item.get("status") in {"ready", "not_observable"}
        for item in third_party.values()
    )
    profile_read_only = exact_servers and all(
        item.get("read_only") is True for item in third_party.values()
    )
    return {
        "status": "ready"
        if exact_servers and profile_status
        else "partial"
        if profile_partial
        else "drift",
        "reason_code": "DOCKER_PROFILE_READY"
        if exact_servers and profile_status
        else "DOCKER_PROFILE_PARTIAL"
        if profile_partial
        else "DOCKER_PROFILE_DRIFT",
        "version": version,
        "profile_id": CANONICAL_DOCKER_PROFILE_ID,
        "profile_name": profile_name
        if isinstance(profile_name, str) and len(profile_name) <= 128
        else "unknown",
        "server_names": sorted(server_statuses),
        "third_party": third_party,
        "server_count": len(server_statuses),
        "read_only": profile_read_only,
        "secret_engine": secret_engine,
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
    source_state = (
        "modified"
        if working_tree != "clean"
        else "aligned"
        if expected_revision != UNKNOWN_SOURCE_REVISION
        and observed_revision == expected_revision
        else "unknown"
        if expected_revision == UNKNOWN_SOURCE_REVISION
        or observed_revision == UNKNOWN_SOURCE_REVISION
        else "drift"
    )
    result["version_status"] = version_state
    result["source_status"] = source_state
    result["status"] = (
        "ready"
        if version_state == "compatible" and source_state in {"aligned", "unknown"}
        else "drift"
    )
    if result["status"] == "drift":
        result["reason_code"] = "MCP_VERSION_OR_SOURCE_DRIFT"
    return result


def _version_guard(
    root: Path, expected_versions: Mapping[str, str]
) -> dict[str, object]:
    """Проверить identity contracts и plugin compatibility без сети."""

    try:
        import json

        from module.dev_mcp.contract import (
            contract_compatibility_issues,
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
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
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


async def collect_status_async(
    root: Path | str | None = None,
    *,
    local_probe: LocalProbe | None = None,
    remote_probe: RemoteProbe | None = None,
    docker_probe: DockerProbe | None = None,
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
        lambda name, path, current_revision: _probe_local_stdio(
            name, root=path, revision=current_revision
        )
    )
    remote = remote_probe or _probe_remote
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
            remote_result = await remote(name)
        except Exception as exc:  # noqa: BLE001 - injected probes are untrusted.
            remote_result = {
                "status": "unavailable",
                "reason_code": "REMOTE_PROBE_FAILED",
                "error_type": _safe_type_name(exc),
            }
        codex_result: dict[str, object]
        if name == "azurpilot-dev":
            codex_result = _codex_entry_status(
                _load_codex_config(repository_root),
                name,
                expected_command="uv",
                expected_args=(
                    "run",
                    "--locked",
                    "--no-sync",
                    "python",
                    "-m",
                    "module.dev_mcp",
                ),
            )
            if codex_result["status"] == "configured":
                codex_result = {
                    **codex_result,
                    "probe_status": local_result.get("status"),
                }
        else:
            codex_result = {
                "status": "not_configured",
                "reason_code": "CODEX_GAME_SURFACE_EXTERNAL",
            }
        servers[name] = {
            "expected_version": expected,
            "local_direct": local_result,
            "codex": codex_result,
            "remote": remote_result,
        }
    version_guard = _version_guard(repository_root, expected_versions)
    try:
        docker = await asyncio.to_thread(docker_probe or _docker_status)
    except Exception as exc:  # noqa: BLE001 - status boundary hides subprocess details.
        docker = {
            "status": "unavailable",
            "reason_code": "DOCKER_PROBE_FAILED",
            "error_type": _safe_type_name(exc),
        }
    docker_client = _codex_entry_status(
        _load_codex_config(repository_root),
        DOCKER_CLIENT_PROFILE_NAME,
        expected_command=DOCKER_CLIENT_COMMAND,
        expected_args=DOCKER_CLIENT_ARGS,
    )
    docker = {**docker, "codex": docker_client}
    chatgpt = {
        "status": "not_observable",
        "reason_code": "CHATGPT_ACTION_SNAPSHOT_NOT_OBSERVABLE",
    }
    drift = (
        any(
            surface.get("status") == "drift"
            for item in servers.values()
            for surface in (item.get("local_direct"), item.get("codex"))
            if isinstance(surface, Mapping)
        )
        or docker_client.get("status") == "drift"
    )
    unavailable = any(
        surface.get("status") in {"unavailable", "invalid"}
        for item in servers.values()
        for surface in (item.get("local_direct"), item.get("remote"))
        if isinstance(surface, Mapping)
    )
    unknown = any(
        isinstance(item.get("remote"), Mapping)
        and item["remote"].get("status") == "not_observable"
        for item in servers.values()
        if isinstance(item, Mapping)
    )
    status = (
        "drift"
        if drift or version_guard.get("status") == "drift"
        else "partial"
        if unavailable
        or unknown
        or working_tree != "clean"
        or version_guard.get("status") != "ready"
        or docker.get("status") != "ready"
        else "ready"
    )
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "generated_at": now(),
        "status": status,
        "reason_code": "MCP_STATUS_READY"
        if status == "ready"
        else "MCP_STATUS_DRIFT"
        if status == "drift"
        else "MCP_STATUS_PARTIAL",
        "source": {"revision": revision, "working_tree": working_tree},
        "servers": servers,
        "version_guard": version_guard,
        "docker_mcp": docker,
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


_METRIC_ATTRIBUTE_NAMES = frozenset({"server", "surface", "version", "protocol"})
_METRIC_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


def status_metric_samples(report: Mapping[str, object]) -> tuple[MetricSample, ...]:
    """Построить bounded samples без SHA, URL, токенов и диагностических payload."""

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
            for surface_name in ("local_direct", "codex", "remote"):
                surface = value.get(surface_name)
                if not isinstance(surface, Mapping):
                    continue
                status = surface.get("status")
                protocol = surface.get("protocol_version")
                attributes = {
                    "server": server_name,
                    "surface": surface_name,
                    "version": expected_version,
                    "protocol": protocol
                    if isinstance(protocol, str) and _SAFE_TOKEN.fullmatch(protocol)
                    else "unknown",
                }
                surface_ok = status in {"ready", "configured"}
                endpoint_up = (
                    1.0 if surface_ok or surface.get("endpoint_up") is True else 0.0
                )
                samples.append(
                    MetricSample("azurpilot_mcp_endpoint_up", endpoint_up, attributes)
                )
                samples.append(
                    MetricSample(
                        "azurpilot_mcp_version_info",
                        1.0 if surface_ok else 0.0,
                        attributes,
                    )
                )
                samples.append(
                    MetricSample(
                        "azurpilot_mcp_version_drift",
                        1.0 if surface.get("version_status") == "drift" else 0.0,
                        attributes,
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
            ready = isinstance(value, Mapping) and value.get("status") == "ready"
            samples.append(
                MetricSample(
                    "azurpilot_mcp_gateway_server_up",
                    1.0 if ready else 0.0,
                    {
                        "server": server_name,
                        "surface": "docker_gateway",
                        "version": "unknown",
                        "protocol": "unknown",
                    },
                )
            )
    docker_version = docker.get("version") if isinstance(docker, Mapping) else None
    if isinstance(docker_version, str) and _VERSION_RE.fullmatch(docker_version):
        samples.append(
            MetricSample(
                "azurpilot_mcp_version_info",
                1.0
                if isinstance(docker, Mapping) and docker.get("status") == "ready"
                else 0.0,
                {
                    "server": "docker-gateway",
                    "surface": "docker_gateway",
                    "version": docker_version.removeprefix("v"),
                    "protocol": "unknown",
                },
            )
        )
    samples.append(
        MetricSample(
            "azurpilot_mcp_last_probe_timestamp_seconds",
            datetime.now(UTC).timestamp(),
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

    endpoint = _metrics_endpoint()
    samples = status_metric_samples(report)
    if not endpoint:
        return MetricEmission(False, "MCP_METRICS_ENDPOINT_UNCONFIGURED", len(samples))
    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.util.re import parse_env_headers

        headers_raw = os.environ.get(
            "OTEL_EXPORTER_OTLP_METRICS_HEADERS"
        ) or os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
        headers = (
            dict(parse_env_headers(headers_raw, liberal=True)) if headers_raw else None
        )
        exporter = OTLPMetricExporter(
            endpoint=endpoint,
            timeout=METRICS_TIMEOUT_SECONDS,
            headers=headers,
        )
        reader = PeriodicExportingMetricReader(
            exporter,
            export_interval_millis=60_000,
            export_timeout_millis=int(METRICS_TIMEOUT_SECONDS * 1000),
        )
        provider = MeterProvider(
            metric_readers=(reader,),
            resource=Resource.create(
                {
                    "service.name": "azurpilot-mcp-status",
                    "deployment.environment.name": "development",
                }
            ),
            shutdown_on_exit=False,
        )
        instruments: dict[str, Any] = {}
        for sample in samples:
            instrument = instruments.get(sample.name)
            if instrument is None:
                instrument = provider.get_meter("azurpilot.mcp.status").create_gauge(
                    sample.name,
                    unit="1"
                    if sample.name != "azurpilot_mcp_last_probe_timestamp_seconds"
                    else "s",
                )
                instruments[sample.name] = instrument
            instrument.set(sample.value, attributes=sample.attributes)
        flushed = bool(
            provider.force_flush(timeout_millis=int(METRICS_TIMEOUT_SECONDS * 1000))
        )
        provider.shutdown(timeout_millis=int(METRICS_TIMEOUT_SECONDS * 1000))
    except Exception:  # noqa: BLE001 - status command must not expose headers/errors.
        with suppress(Exception):
            provider.shutdown()  # type: ignore[has-type, possibly-undefined]
        return MetricEmission(False, "MCP_METRICS_EXPORT_FAILED", len(samples))
    return MetricEmission(
        flushed,
        "MCP_METRICS_EXPORTED" if flushed else "MCP_METRICS_EXPORT_FAILED",
        len(samples),
    )


def _strict_failure(
    report: Mapping[str, object], emission: MetricEmission | None
) -> bool:
    if report.get("status") in {"drift", "invalid"}:
        return True
    version_guard = report.get("version_guard")
    if isinstance(version_guard, Mapping) and version_guard.get("status") != "ready":
        return True
    source = report.get("source")
    if isinstance(source, Mapping) and source.get("working_tree") != "clean":
        return True
    servers = report.get("servers")
    if isinstance(servers, Mapping):
        for item in servers.values():
            if not isinstance(item, Mapping):
                return True
            for key in ("local_direct", "remote"):
                surface = item.get(key)
                if isinstance(surface, Mapping) and surface.get("status") in {
                    "unavailable",
                    "drift",
                    "invalid",
                    "not_observable",
                }:
                    return True
    docker = report.get("docker_mcp")
    if isinstance(docker, Mapping) and docker.get("status") != "ready":
        return True
    return emission is not None and not emission.emitted


def _print_human(report: Mapping[str, object], emission: MetricEmission | None) -> None:
    print(
        f"MCP status: {report.get('status')} ({report.get('reason_code')}); "
        f"source={report.get('source', {}).get('revision', UNKNOWN_SOURCE_REVISION) if isinstance(report.get('source'), Mapping) else UNKNOWN_SOURCE_REVISION}"
    )
    servers = report.get("servers")
    if isinstance(servers, Mapping):
        for name, item in servers.items():
            if not isinstance(item, Mapping):
                continue
            expected = item.get("expected_version", "unknown")
            print(f"  {name}: expected={expected}")
            for surface_name in ("local_direct", "codex", "remote"):
                surface = item.get(surface_name)
                if isinstance(surface, Mapping):
                    print(
                        f"    {surface_name}: {surface.get('status')} "
                        f"({surface.get('reason_code')})"
                    )
    docker = report.get("docker_mcp")
    if isinstance(docker, Mapping):
        print(f"  docker_mcp: {docker.get('status')} ({docker.get('reason_code')})")
        secret_engine = docker.get("secret_engine")
        if isinstance(secret_engine, Mapping):
            print(
                f"    secret_engine: {secret_engine.get('status')} "
                f"({secret_engine.get('reason_code')}); "
                f"cli={secret_engine.get('cli_status')}, "
                f"keychain={secret_engine.get('keychain_status')}, "
                f"rpc={secret_engine.get('rpc_status')}"
            )
            for surface_name in (
                "secret_store",
                "container_runtime_secret_injection",
                "gateway_secret_injection",
                "host_pass_resolution",
            ):
                surface = secret_engine.get(surface_name)
                if isinstance(surface, Mapping):
                    print(
                        f"    {surface_name}: {surface.get('status')} "
                        f"({surface.get('reason_code')})"
                    )
        third_party = docker.get("third_party")
        if isinstance(third_party, Mapping):
            for name, item in third_party.items():
                if isinstance(item, Mapping):
                    print(
                        f"    {name}: {item.get('status')} ({item.get('reason_code')})"
                    )
    chatgpt = report.get("chatgpt")
    if isinstance(chatgpt, Mapping):
        print(f"  chatgpt: {chatgpt.get('status')} ({chatgpt.get('reason_code')})")
    version_guard = report.get("version_guard")
    if isinstance(version_guard, Mapping):
        print(
            f"  version_guard: {version_guard.get('status')} "
            f"({version_guard.get('reason_code')})"
        )
    if emission is not None:
        print(f"  metrics: {emission.reason_code}; samples={emission.sample_count}")


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
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
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
    if arguments.as_json:
        print(
            json.dumps(
                report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        )
    else:
        _print_human(report, emission)
    return 2 if arguments.strict and _strict_failure(report, emission) else 0


if __name__ == "__main__":
    raise SystemExit(main())
