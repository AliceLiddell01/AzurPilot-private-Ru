"""Безопасный bootstrap и bounded acceptance профиля Grafana MCP."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

CANONICAL_PROFILE_ID = "azurpilot-observability"
PROFILE_RELATIVE_PATH = Path(".docker/azurpilot-observability-profile.json")
CANONICAL_SERVICE_ACCOUNT = "azurpilot-observability-mcp"
CANONICAL_SERVICE_ACCOUNT_ROLE = "Viewer"
CANONICAL_SECRET_NAME = "grafana.api_key"
CANONICAL_TOKEN_NAME = "azurpilot-observability-mcp"
DEFAULT_GRAFANA_URL = "http://127.0.0.1:3000"
_DYNAMIC_TOOLS_FEATURE = "dynamic-tools"
_DYNAMIC_TOOL_NAMES = frozenset(
    {
        "code-mode",
        "mcp-activate-profile",
        "mcp-add",
        "mcp-config-set",
        "mcp-create-profile",
        "mcp-discover",
        "mcp-exec",
        "mcp-find",
        "mcp-remove",
    }
)
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_TOOL_ARGUMENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_FEATURE_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Это project contract exported profile. Изменение списка требует одновременной
# проверки profile export и фактического Gateway tools/list.
EXPECTED_PROFILE_TOOLS = (
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
    "alerting_manage_rules",
    "tempo_docs-traceql",
    "tempo_get-attribute-names",
    "tempo_get-attribute-values",
    "tempo_get-trace",
    "tempo_traceql-metrics-instant",
    "tempo_traceql-metrics-range",
    "tempo_traceql-search",
)
EXPECTED_PROFILE_TOOL_SET = frozenset(EXPECTED_PROFILE_TOOLS)


class ObservabilityMcpError(RuntimeError):
    """Безопасная machine-readable ошибка без вывода секретных payload."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ServiceAccountState:
    account_id: int
    created: bool


@dataclass(frozen=True, slots=True)
class GatewayProbe:
    ok: bool
    authentication_failed: bool
    code: str


def _docker_executable() -> str:
    executable = shutil.which("docker.exe") or shutil.which("docker")
    if executable is None:
        raise ObservabilityMcpError("MCP_DOCKER_CLI_UNAVAILABLE")
    return executable


def _run_docker(
    arguments: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    options: dict[str, object] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
    }
    if input_text is None:
        options["stdin"] = subprocess.DEVNULL
    else:
        options["input"] = input_text
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.run(
            [_docker_executable(), *arguments], check=False, **options
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ObservabilityMcpError("MCP_DOCKER_COMMAND_FAILED") from exc


def _checked_docker(
    arguments: list[str], *, input_text: str | None = None, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    result = _run_docker(arguments, input_text=input_text, timeout=timeout)
    if result.returncode != 0:
        raise ObservabilityMcpError("MCP_DOCKER_COMMAND_FAILED")
    return result


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ObservabilityMcpError("MCP_PROFILE_INVALID") from exc
    if not isinstance(value, dict):
        raise ObservabilityMcpError("MCP_PROFILE_INVALID")
    return value


def validate_profile(profile: dict[str, Any]) -> None:
    """Проверить exported profile и его единственную canonical server запись."""

    if profile.get("version") != 1 or profile.get("id") != CANONICAL_PROFILE_ID:
        raise ObservabilityMcpError("MCP_PROFILE_ID_INVALID")
    servers = profile.get("servers")
    if not isinstance(servers, list) or len(servers) != 1:
        raise ObservabilityMcpError("MCP_PROFILE_SERVER_COUNT_INVALID")
    server = servers[0]
    if not isinstance(server, dict) or server.get("type") != "image":
        raise ObservabilityMcpError("MCP_PROFILE_SERVER_INVALID")
    tools = server.get("tools")
    if not isinstance(tools, list) or any(not isinstance(item, str) for item in tools):
        raise ObservabilityMcpError("MCP_PROFILE_TOOLS_INVALID")
    if len(tools) != len(EXPECTED_PROFILE_TOOLS):
        raise ObservabilityMcpError("MCP_PROFILE_TOOL_COUNT_INVALID")
    if len(set(tools)) != len(EXPECTED_PROFILE_TOOLS):
        raise ObservabilityMcpError("MCP_PROFILE_TOOL_DUPLICATE")
    if set(tools) != EXPECTED_PROFILE_TOOL_SET:
        raise ObservabilityMcpError("MCP_PROFILE_ALLOWLIST_INVALID")
    if server.get("secrets") != "default":
        raise ObservabilityMcpError("MCP_PROFILE_SECRET_SCOPE_INVALID")
    image = server.get("image")
    if not isinstance(image, str) or not image.startswith("mcp/grafana@sha256:"):
        raise ObservabilityMcpError("MCP_PROFILE_IMAGE_NOT_PINNED")
    config = server.get("config")
    if not isinstance(config, dict) or config.get("url") != "http://host.docker.internal:3000":
        raise ObservabilityMcpError("MCP_PROFILE_GRAFANA_URL_INVALID")

    secrets = profile.get("secrets")
    if not isinstance(secrets, dict):
        raise ObservabilityMcpError("MCP_PROFILE_SECRET_CONFIG_INVALID")
    default_secret = secrets.get("default")
    if not isinstance(default_secret, dict) or default_secret.get("provider") != "docker-desktop-store":
        raise ObservabilityMcpError("MCP_PROFILE_SECRET_PROVIDER_INVALID")

    snapshot = server.get("snapshot")
    snapshot_server = snapshot.get("server") if isinstance(snapshot, dict) else None
    if not isinstance(snapshot_server, dict):
        raise ObservabilityMcpError("MCP_PROFILE_SNAPSHOT_INVALID")
    if snapshot_server.get("image") != image:
        raise ObservabilityMcpError("MCP_PROFILE_SNAPSHOT_DRIFT")
    if snapshot_server.get("secrets") != [
        {"name": CANONICAL_SECRET_NAME, "env": "GRAFANA_SERVICE_ACCOUNT_TOKEN"}
    ]:
        raise ObservabilityMcpError("MCP_PROFILE_SNAPSHOT_SECRET_INVALID")
    if snapshot_server.get("command") != [
        "--transport=stdio",
        "--disable-write",
        "--max-loki-log-limit=50",
    ]:
        raise ObservabilityMcpError("MCP_PROFILE_SERVER_FLAGS_INVALID")
    if snapshot_server.get("env") != [
        {"name": "GRAFANA_URL", "value": "{{grafana.url}}"}
    ]:
        raise ObservabilityMcpError("MCP_PROFILE_ENV_INVALID")

    forbidden = {"volumes", "ports", "docker_socket", "host_filesystem", "privileged"}
    if forbidden.intersection(server) or forbidden.intersection(snapshot_server):
        raise ObservabilityMcpError("MCP_PROFILE_HOST_ACCESS_INVALID")


def load_and_validate_profile(path: Path) -> dict[str, Any]:
    """Загрузить единственный canonical profile artifact."""

    profile = _load_json_file(path)
    validate_profile(profile)
    return profile


def _parse_feature_state(raw: str) -> str:
    for line in _FEATURE_ANSI_RE.sub("", raw).splitlines():
        fields = line.strip().split()
        if len(fields) >= 2 and fields[0] == _DYNAMIC_TOOLS_FEATURE:
            state = fields[1].casefold()
            if state in {"enabled", "disabled"}:
                return state
    raise ObservabilityMcpError("MCP_DYNAMIC_TOOLS_STATE_UNKNOWN")


def dynamic_tools_state() -> str:
    """Прочитать global Docker MCP feature без изменения других поверхностей."""

    result = _checked_docker(["mcp", "feature", "ls"])
    return _parse_feature_state(result.stdout)


def ensure_dynamic_tools_disabled(*, apply: bool) -> str:
    """Проверить или применить static boundary Docker MCP Toolkit."""

    state = dynamic_tools_state()
    if state == "enabled":
        if not apply:
            raise ObservabilityMcpError("MCP_DYNAMIC_TOOLS_ENABLED")
        _checked_docker(["mcp", "feature", "disable", _DYNAMIC_TOOLS_FEATURE])
        state = dynamic_tools_state()
    if state != "disabled":
        raise ObservabilityMcpError("MCP_DYNAMIC_TOOLS_NOT_DISABLED")
    return state


def _runtime_tool_names_from_payload(payload: object) -> tuple[str, ...]:
    if not isinstance(payload, list):
        raise ObservabilityMcpError("MCP_RUNTIME_TOOLS_INVALID")
    names: list[str] = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ObservabilityMcpError("MCP_RUNTIME_TOOLS_INVALID")
        names.append(item["name"])
    if len(names) != len(set(names)):
        raise ObservabilityMcpError("MCP_RUNTIME_TOOL_DUPLICATE")
    actual = set(names)
    if actual.intersection(_DYNAMIC_TOOL_NAMES) or any(
        name.startswith("mcp-") or name == "code-mode" for name in actual
    ):
        raise ObservabilityMcpError("MCP_RUNTIME_DYNAMIC_TOOL_PRESENT")
    if actual != EXPECTED_PROFILE_TOOL_SET:
        raise ObservabilityMcpError("MCP_RUNTIME_ALLOWLIST_MISMATCH")
    return tuple(sorted(names))


def _decode_json_output(raw: str) -> object:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        for line in reversed(raw.splitlines()):
            if not line.strip():
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    raise ObservabilityMcpError("MCP_GATEWAY_JSON_INVALID")


def runtime_tool_names() -> tuple[str, ...]:
    """Получить и проверить фактический Gateway tools/list через Docker CLI."""

    result = _checked_docker(
        [
            "mcp",
            "tools",
            "ls",
            "--format=json",
            "--gateway-arg=--profile",
            f"--gateway-arg={CANONICAL_PROFILE_ID}",
            "--gateway-arg=--watch=false",
        ],
        timeout=180,
    )
    return _runtime_tool_names_from_payload(_decode_json_output(result.stdout))


def _gateway_tool_call(
    tool_name: str, arguments: dict[str, str] | None = None
) -> object:
    command = [
        "mcp",
        "tools",
        "--gateway-arg=--profile",
        f"--gateway-arg={CANONICAL_PROFILE_ID}",
        "--gateway-arg=--watch=false",
        "call",
        tool_name,
    ]
    for name, value in (arguments or {}).items():
        if not _TOOL_ARGUMENT_RE.fullmatch(name):
            raise ObservabilityMcpError("MCP_TOOL_ARGUMENT_INVALID")
        command.append(f"{name}={value}")
    result = _run_docker(command, timeout=180)
    if result.returncode != 0:
        raise ObservabilityMcpError("MCP_GATEWAY_TOOL_FAILED")
    return _decode_json_output(result.stdout)


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    except (OSError, UnicodeError) as exc:
        raise ObservabilityMcpError("MCP_ENV_FILE_UNAVAILABLE") from exc
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if not _ENV_KEY_RE.fullmatch(name):
            continue
        if name in values:
            raise ObservabilityMcpError("MCP_ENV_DUPLICATE_KEY")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[name] = value
    return values


def _setting(name: str, env_values: dict[str, str]) -> str | None:
    value = os.environ.get(name)
    if value:
        return value
    return env_values.get(name)


def _grafana_url(value: str | None) -> str:
    url = value or DEFAULT_GRAFANA_URL
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError as exc:
        raise ObservabilityMcpError("MCP_GRAFANA_URL_INVALID") from exc
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ObservabilityMcpError("MCP_GRAFANA_URL_INVALID")
    if hostname.casefold() not in {"127.0.0.1", "::1", "localhost"}:
        raise ObservabilityMcpError("MCP_GRAFANA_URL_INVALID")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ObservabilityMcpError("MCP_GRAFANA_URL_INVALID")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


class _GrafanaApi:
    """Минимальный JSON client для pinned Grafana service-account API."""

    def __init__(
        self,
        base_url: str,
        *,
        admin_user: str,
        admin_password: str,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self._base_url = base_url
        self._admin_user = admin_user
        self._admin_password = admin_password
        self._opener = opener

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        bearer: str | None = None,
        error_code: str = "MCP_GRAFANA_REQUEST_FAILED",
    ) -> Any:
        headers = {"Accept": "application/json"}
        if bearer is not None:
            headers["Authorization"] = f"Bearer {bearer}"
        else:
            basic = base64.b64encode(
                f"{self._admin_user}:{self._admin_password}".encode()
            ).decode("ascii")
            headers["Authorization"] = f"Basic {basic}"
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self._base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self._opener(request, timeout=30) as response:
                raw = response.read()
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise ObservabilityMcpError(f"{error_code}_UNAUTHORIZED") from exc
            if exc.code == 404:
                raise ObservabilityMcpError(f"{error_code}_NOT_FOUND") from exc
            raise ObservabilityMcpError(f"{error_code}_HTTP") from exc
        except (OSError, URLError, TimeoutError) as exc:
            raise ObservabilityMcpError(f"{error_code}_UNAVAILABLE") from exc
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ObservabilityMcpError(f"{error_code}_JSON_INVALID") from exc

    def search_service_account(self) -> list[dict[str, Any]]:
        query = urlencode(
            {"perpage": "100", "page": "1", "query": CANONICAL_SERVICE_ACCOUNT}
        )
        payload = self._request(
            "GET",
            f"/api/serviceaccounts/search?{query}",
            error_code="MCP_GRAFANA_SERVICE_ACCOUNT_SEARCH",
        )
        accounts = payload.get("serviceAccounts") if isinstance(payload, dict) else None
        if not isinstance(accounts, list) or any(not isinstance(item, dict) for item in accounts):
            raise ObservabilityMcpError("MCP_GRAFANA_SERVICE_ACCOUNT_RESPONSE_INVALID")
        return [
            item for item in accounts if item.get("name") == CANONICAL_SERVICE_ACCOUNT
        ]

    def create_service_account(self) -> dict[str, Any]:
        payload = self._request(
            "POST",
            "/api/serviceaccounts",
            payload={
                "name": CANONICAL_SERVICE_ACCOUNT,
                "role": CANONICAL_SERVICE_ACCOUNT_ROLE,
                "isDisabled": False,
            },
            error_code="MCP_GRAFANA_SERVICE_ACCOUNT_CREATE",
        )
        if not isinstance(payload, dict):
            raise ObservabilityMcpError("MCP_GRAFANA_SERVICE_ACCOUNT_RESPONSE_INVALID")
        return payload

    def update_service_account(self, account_id: int) -> dict[str, Any]:
        payload = self._request(
            "PATCH",
            f"/api/serviceaccounts/{account_id}",
            payload={
                "name": CANONICAL_SERVICE_ACCOUNT,
                "role": CANONICAL_SERVICE_ACCOUNT_ROLE,
                "isDisabled": False,
            },
            error_code="MCP_GRAFANA_SERVICE_ACCOUNT_UPDATE",
        )
        if not isinstance(payload, dict):
            raise ObservabilityMcpError("MCP_GRAFANA_SERVICE_ACCOUNT_RESPONSE_INVALID")
        return payload

    def list_tokens(self, account_id: int) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            f"/api/serviceaccounts/{account_id}/tokens",
            error_code="MCP_GRAFANA_TOKEN_LIST",
        )
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise ObservabilityMcpError("MCP_GRAFANA_TOKEN_RESPONSE_INVALID")
        return payload

    def create_token(self, account_id: int) -> tuple[int, str]:
        payload = self._request(
            "POST",
            f"/api/serviceaccounts/{account_id}/tokens",
            payload={"name": CANONICAL_TOKEN_NAME, "secondsToLive": 0},
            error_code="MCP_GRAFANA_TOKEN_CREATE",
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("key"), str):
            raise ObservabilityMcpError("MCP_GRAFANA_TOKEN_RESPONSE_INVALID")
        token_id = payload.get("id")
        if not isinstance(token_id, int) or token_id <= 0:
            raise ObservabilityMcpError("MCP_GRAFANA_TOKEN_RESPONSE_INVALID")
        return token_id, payload["key"]

    def verify_token(self, token: str) -> None:
        payload = self._request(
            "GET",
            "/api/user",
            bearer=token,
            error_code="MCP_GRAFANA_TOKEN_VERIFY",
        )
        if not isinstance(payload, dict) or not payload.get("login"):
            raise ObservabilityMcpError("MCP_GRAFANA_TOKEN_VERIFY_INVALID")

    def delete_token(self, account_id: int, token_id: int) -> None:
        self._request(
            "DELETE",
            f"/api/serviceaccounts/{account_id}/tokens/{token_id}",
            error_code="MCP_GRAFANA_TOKEN_DELETE",
        )


def _ensure_service_account(api: _GrafanaApi) -> ServiceAccountState:
    accounts = api.search_service_account()
    if len(accounts) > 1:
        raise ObservabilityMcpError("MCP_GRAFANA_SERVICE_ACCOUNT_DUPLICATE")
    if not accounts:
        account = api.create_service_account()
        created = True
    else:
        account = accounts[0]
        created = False
        account_id = account.get("id")
        if not isinstance(account_id, int) or account_id <= 0:
            raise ObservabilityMcpError("MCP_GRAFANA_SERVICE_ACCOUNT_RESPONSE_INVALID")
        if account.get("role") != CANONICAL_SERVICE_ACCOUNT_ROLE or account.get("isDisabled"):
            account = api.update_service_account(account_id)
    account_id = account.get("id")
    if not isinstance(account_id, int) or account_id <= 0:
        raise ObservabilityMcpError("MCP_GRAFANA_SERVICE_ACCOUNT_RESPONSE_INVALID")
    if account.get("name") != CANONICAL_SERVICE_ACCOUNT:
        raise ObservabilityMcpError("MCP_GRAFANA_SERVICE_ACCOUNT_RESPONSE_INVALID")
    if account.get("role") != CANONICAL_SERVICE_ACCOUNT_ROLE or account.get("isDisabled"):
        raise ObservabilityMcpError("MCP_GRAFANA_SERVICE_ACCOUNT_ROLE_INVALID")
    return ServiceAccountState(account_id=account_id, created=created)


def _gateway_probe() -> GatewayProbe:
    result = _run_docker(
        [
            "mcp",
            "tools",
            "--gateway-arg=--profile",
            f"--gateway-arg={CANONICAL_PROFILE_ID}",
            "--gateway-arg=--watch=false",
            "call",
            "list_datasources",
        ],
        timeout=180,
    )
    if result.returncode != 0:
        return GatewayProbe(False, False, "MCP_GATEWAY_UNAVAILABLE")
    try:
        payload = _decode_json_output(result.stdout)
    except ObservabilityMcpError:
        return GatewayProbe(False, False, "MCP_GATEWAY_JSON_INVALID")
    if isinstance(payload, dict) and payload.get("isError") is True:
        text_parts: list[str] = []
        for key in ("error", "message", "detail", "reason"):
            value = payload.get(key)
            if isinstance(value, str):
                text_parts.append(value)
        content = payload.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                for key in ("text", "message"):
                    value = item.get(key)
                    if isinstance(value, str):
                        text_parts.append(value)
        text = " ".join(text_parts).casefold()
        authentication_failed = any(
            marker in text
            for marker in (
                "401",
                "403",
                "unauthorized",
                "forbidden",
                "authentication",
                "invalid token",
            )
        )
        return GatewayProbe(
            False,
            authentication_failed,
            "MCP_GATEWAY_AUTH_INVALID"
            if authentication_failed
            else "MCP_GATEWAY_TOOL_ERROR",
        )
    if isinstance(payload, dict) and isinstance(payload.get("datasources"), list):
        return GatewayProbe(True, False, "MCP_GATEWAY_READY")
    return GatewayProbe(False, False, "MCP_GATEWAY_RESPONSE_INVALID")


def _store_secret(token: str) -> None:
    _checked_docker(
        ["mcp", "secret", "set", CANONICAL_SECRET_NAME],
        input_text=f"{token}\n",
        timeout=60,
    )


def ensure_identity(
    *,
    repository_root: Path,
    env_file: Path,
    grafana_url: str | None = None,
    import_profile: bool = True,
) -> dict[str, object]:
    """Идемпотентно обеспечить Viewer account, token и Docker secret."""

    profile_path = repository_root / PROFILE_RELATIVE_PATH
    load_and_validate_profile(profile_path)
    ensure_dynamic_tools_disabled(apply=True)
    if import_profile:
        _checked_docker(["mcp", "profile", "import", str(profile_path)], timeout=60)

    env_values = _load_env_file(env_file)
    admin_user = _setting("AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_USER", env_values)
    admin_password = _setting(
        "AZURPILOT_OBSERVABILITY_GRAFANA_ADMIN_PASSWORD", env_values
    )
    if not admin_user or not admin_password:
        raise ObservabilityMcpError("MCP_GRAFANA_ADMIN_CREDENTIALS_UNAVAILABLE")
    api = _GrafanaApi(
        _grafana_url(
            grafana_url
            or _setting("AZURPILOT_OBSERVABILITY_GRAFANA_URL", env_values)
        ),
        admin_user=admin_user,
        admin_password=admin_password,
    )
    account = _ensure_service_account(api)
    tokens = api.list_tokens(account.account_id)

    if not account.created:
        probe = _gateway_probe()
        if probe.ok:
            return {
                "ok": True,
                "account": CANONICAL_SERVICE_ACCOUNT,
                "role": CANONICAL_SERVICE_ACCOUNT_ROLE,
                "token": "reused",
                "gateway": probe.code,
            }
        if not probe.authentication_failed:
            raise ObservabilityMcpError(probe.code)

    token_id, token = api.create_token(account.account_id)
    try:
        api.verify_token(token)
        _store_secret(token)
        probe = _gateway_probe()
        if not probe.ok:
            raise ObservabilityMcpError("MCP_GATEWAY_AUTH_AFTER_TOKEN_STORE_FAILED")
    except BaseException:
        try:
            api.delete_token(account.account_id, token_id)
        except BaseException:
            pass
        raise
    finally:
        del token

    old_tokens = [
        item
        for item in tokens
        if item.get("id") != token_id and item.get("name") == CANONICAL_TOKEN_NAME
    ]
    for old_token in old_tokens:
        old_id = old_token.get("id")
        if isinstance(old_id, int) and old_id > 0:
            api.delete_token(account.account_id, old_id)
    return {
        "ok": True,
        "account": CANONICAL_SERVICE_ACCOUNT,
        "role": CANONICAL_SERVICE_ACCOUNT_ROLE,
        "token": "created",
        "gateway": probe.code,
        "obsolete_tokens_revoked": len(old_tokens),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bootstrap и bounded acceptance локального Grafana MCP profile."
    )
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "preflight",
        help="Проверить, что global Dynamic MCP уже отключён.",
    )
    subparsers.add_parser(
        "ensure-boundary",
        help="Отключить Dynamic MCP и проверить static boundary.",
    )
    subparsers.add_parser(
        "runtime-tools",
        help="Проверить фактический Gateway tools/list против allowlist.",
    )
    profile = subparsers.add_parser(
        "profile",
        help="Проверить canonical profile и импортировать его в Toolkit.",
    )
    profile.add_argument(
        "--import",
        action="store_true",
        dest="import_profile",
        help="Импортировать profile после проверки.",
    )
    identity = subparsers.add_parser(
        "ensure-identity",
        help="Обеспечить Grafana Viewer service account, token и Docker secret.",
    )
    identity.add_argument("--env-file", type=Path, default=Path(".env"))
    identity.add_argument("--grafana-url", default=None)
    identity.add_argument(
        "--no-import",
        action="store_true",
        help="Не импортировать profile перед bootstrap.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    repository_root = arguments.repository_root.resolve()
    try:
        if arguments.command == "preflight":
            result: object = {
                "ok": True,
                "dynamic_tools": ensure_dynamic_tools_disabled(apply=False),
            }
        elif arguments.command == "ensure-boundary":
            result = {
                "ok": True,
                "dynamic_tools": ensure_dynamic_tools_disabled(apply=True),
            }
        elif arguments.command == "runtime-tools":
            load_and_validate_profile(repository_root / PROFILE_RELATIVE_PATH)
            ensure_dynamic_tools_disabled(apply=False)
            names = runtime_tool_names()
            result = {"ok": True, "tool_count": len(names), "tools": names}
        elif arguments.command == "profile":
            profile_path = repository_root / PROFILE_RELATIVE_PATH
            profile = load_and_validate_profile(profile_path)
            if arguments.import_profile:
                _checked_docker(["mcp", "profile", "import", str(profile_path)], timeout=60)
            result = {
                "ok": True,
                "profile": profile["id"],
                "imported": bool(arguments.import_profile),
            }
        else:
            result = ensure_identity(
                repository_root=repository_root,
                env_file=arguments.env_file.resolve(),
                grafana_url=arguments.grafana_url,
                import_profile=not arguments.no_import,
            )
    except ObservabilityMcpError as exc:
        print(f"ERROR:{exc.code}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
