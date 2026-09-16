"""Загрузка явной конфигурации прямых внешних интеграций."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from azurpilot.tooling.contracts import ResultCode
from azurpilot.tooling.errors import ToolingError
from azurpilot.tooling.filesystem import (
    bounded_read_bytes,
    canonical_path,
    path_has_link,
)
from azurpilot.tooling.process import INTEGRATION_CREDENTIAL_ENVIRONMENT_KEYS

MAX_CONFIG_BYTES = 256 * 1024
_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}@sha256:[0-9a-f]{64}$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")

# Это vendor defaults, а не credentials или machine identity. Image refs
# намеренно immutable; изменять их можно только через явную конфигурацию.
DEFAULTS: dict[str, dict[str, object]] = {
    "semgrep": {
        "command": "semgrep",
        "route": "direct_local_cli",
        "ruleset": "config/semgrep/direct-integrations.yml",
    },
    "context7": {
        "endpoint": "https://mcp.context7.com/mcp",
        "route": "direct_streamable_http",
        "credential_env": "CONTEXT7_API_KEY",
    },
    "docker-docs": {
        "endpoint": "https://mcp-docs.docker.com/mcp",
        "route": "direct_streamable_http",
    },
    "grafana": {
        "command": "docker",
        "image": (
            "mcp/grafana@sha256:"
            "9362bcf6aa0e44e61f645b905cec03fb346a946a34a4dafecd7f3e28d3724014"
        ),
        "route": "direct_container_stdio",
        "credential_env": "GRAFANA_SERVICE_ACCOUNT_TOKEN",
    },
    "docker-hub": {
        "command": "docker",
        "image": (
            "mcp/dockerhub@sha256:"
            "76454af4edfd21571d9740113104d0d9f707220453d1c8f7c9971b21848d4248"
        ),
        "route": "direct_container_stdio",
        "credential_env": "DOCKERHUB_PAT",
    },
    "coderabbit": {"route": "direct_wsl_agent"},
}

_REPO_MCP_ALIASES = {
    "semgrep_local_direct": "semgrep",
    "context7_direct": "context7",
    "docker_docs_direct": "docker-docs",
    "dockerhub_direct": "docker-hub",
    "docker_hub_direct": "docker-hub",
    "grafana_direct": "grafana",
}

_REPOSITORY_FIXED_VALUES: dict[str, dict[str, str]] = {
    "semgrep": {"command": "semgrep"},
    "context7": {
        "endpoint": "https://mcp.context7.com/mcp",
        "credential_env": "CONTEXT7_API_KEY",
    },
    "docker-docs": {"endpoint": "https://mcp-docs.docker.com/mcp"},
    "grafana": {
        "command": "docker",
        "image": str(DEFAULTS["grafana"]["image"]),
        "credential_env": "GRAFANA_SERVICE_ACCOUNT_TOKEN",
    },
    "docker-hub": {
        "command": "docker",
        "image": str(DEFAULTS["docker-hub"]["image"]),
        "credential_env": "DOCKERHUB_PAT",
    },
}

_ENV_OVERRIDES = {
    "coderabbit": {
        "wsl_distribution": "AZURPILOT_CODERABBIT_WSL_DISTRIBUTION",
        "review_clone": "AZURPILOT_CODERABBIT_REVIEW_CLONE",
        "executable": "AZURPILOT_CODERABBIT_EXECUTABLE",
    },
    "grafana": {
        "endpoint": "AZURPILOT_GRAFANA_URL",
        "credential_env": "AZURPILOT_GRAFANA_CREDENTIAL_ENV",
        "credential_file": "GRAFANA_SERVICE_ACCOUNT_TOKEN_FILE",
        "image": "AZURPILOT_GRAFANA_IMAGE",
    },
    "context7": {
        "endpoint": "AZURPILOT_CONTEXT7_ENDPOINT",
        "credential_env": "AZURPILOT_CONTEXT7_CREDENTIAL_ENV",
    },
    "docker-docs": {"endpoint": "AZURPILOT_DOCKER_DOCS_ENDPOINT"},
    "docker-hub": {
        "image": "AZURPILOT_DOCKER_HUB_IMAGE",
        "credential_env": "AZURPILOT_DOCKER_HUB_CREDENTIAL_ENV",
    },
    "semgrep": {"command": "AZURPILOT_SEMGREP_COMMAND"},
}


@dataclass(frozen=True, slots=True)
class IntegrationConfig:
    """Merged settings with provenance labels, never secret values."""

    values: dict[str, dict[str, object]] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)

    def provider(self, name: str) -> dict[str, object]:
        result = dict(DEFAULTS.get(name, {}))
        result.update(self.values.get(name, {}))
        return result

    def source(self, name: str) -> str:
        return self.sources.get(name, "vendor_defaults")


def _raise(message: str) -> None:
    raise ToolingError(ResultCode.TOOLING_PRECONDITION_FAILED, message)


def _read_toml(path: Path) -> dict[str, Any]:
    if path_has_link(path):
        _raise("Файл конфигурации интеграций содержит symlink или reparse point.")
    try:
        raw = bounded_read_bytes(path, max_bytes=MAX_CONFIG_BYTES)
        payload = tomllib.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ToolingError(
            ResultCode.TOOLING_PRECONDITION_FAILED,
            "Файл конфигурации интеграций не удалось разобрать.",
        ) from exc
    if not isinstance(payload, dict):
        _raise("Корень конфигурации интеграций должен быть отображением.")
    return payload


def _candidate_config_paths() -> tuple[tuple[Path, str], ...]:
    machine_candidates: list[tuple[Path, str]] = []
    user_candidates: list[tuple[Path, str]] = []
    explicit_candidates: list[tuple[Path, str]] = []

    def append_environment_path(
        target: list[tuple[Path, str]], variable: str, source: str
    ) -> None:
        raw = os.environ.get(variable, "").strip()
        if not raw:
            return
        path = Path(raw).expanduser()
        if not path.is_absolute():
            _raise(f"{variable} должен быть абсолютным путём.")
        target.append((canonical_path(path), source))

    if os.name == "nt":
        user_base = os.environ.get("APPDATA", "").strip()
        machine_base = os.environ.get("PROGRAMDATA", "").strip()
        if machine_base:
            machine_candidates.append(
                (
                    canonical_path(Path(machine_base) / "AzurPilot" / "config.toml"),
                    "machine_config",
                )
            )
        if user_base:
            user_candidates.append(
                (canonical_path(Path(user_base) / "azurpilot" / "config.toml"), "user_config")
            )
    else:
        user_base = os.environ.get("XDG_CONFIG_HOME", "").strip()
        if user_base:
            user_candidates.append(
                (canonical_path(Path(user_base) / "azurpilot" / "config.toml"), "user_config")
            )

    append_environment_path(machine_candidates, "AZURPILOT_MACHINE_CONFIG", "machine_config")
    append_environment_path(user_candidates, "AZURPILOT_USER_CONFIG", "user_config")
    append_environment_path(explicit_candidates, "AZURPILOT_CONFIG_FILE", "explicit_config")
    candidates = machine_candidates + user_candidates + explicit_candidates
    unique: dict[str, tuple[Path, str]] = {}
    for path, source in candidates:
        unique.setdefault(os.path.normcase(str(path)), (path, source))
    return tuple(unique.values())


def _provider_table(document: dict[str, Any]) -> dict[str, dict[str, object]]:
    raw = document.get("integrations")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        _raise("Секция integrations должна быть отображением.")
    result: dict[str, dict[str, object]] = {}
    for raw_name, raw_values in raw.items():
        if not isinstance(raw_name, str) or not _IDENTIFIER_RE.fullmatch(raw_name):
            _raise("Имя интеграции в конфигурации имеет неверный формат.")
        normalized = raw_name.casefold().replace("_", "-")
        if normalized == "dockerhub":
            normalized = "docker-hub"
        if normalized not in DEFAULTS:
            continue
        if not isinstance(raw_values, dict):
            _raise(f"Секция интеграции {normalized} должна быть отображением.")
        result[normalized] = dict(raw_values)
    return result


def _repo_mcp_table(root: Path) -> dict[str, dict[str, object]]:
    path = root / ".codex" / "config.toml"
    if not path.is_file():
        return {}
    document = _read_toml(path)
    servers = document.get("mcp_servers")
    if not isinstance(servers, dict):
        return {}
    result: dict[str, dict[str, object]] = {}
    for raw_name, raw_values in servers.items():
        name = _REPO_MCP_ALIASES.get(str(raw_name))
        if name is None or not isinstance(raw_values, dict):
            continue
        values: dict[str, object] = {}
        for key in ("command", "args", "url", "image", "credential_env_var", "bearer_token_env_var"):
            if key in raw_values:
                values[key] = raw_values[key]
        if "url" in values:
            values["endpoint"] = values.pop("url")
        if "credential_env_var" in values:
            values["credential_env"] = values.pop("credential_env_var")
        if "bearer_token_env_var" in values:
            values["credential_env"] = values.pop("bearer_token_env_var")
        args = values.get("args")
        if isinstance(args, list):
            for index, item in enumerate(args[:-1]):
                if item in {"--image", "-image"} and isinstance(args[index + 1], str):
                    values["image"] = args[index + 1]
            if name in {"grafana", "docker-hub"}:
                image_candidates = tuple(
                    item
                    for item in args
                    if isinstance(item, str) and _IMAGE_RE.fullmatch(item)
                )
                if len(image_candidates) == 1:
                    values["image"] = image_candidates[0]
        values.pop("args", None)
        expected = _REPOSITORY_FIXED_VALUES.get(name, {})
        for key, value in values.items():
            if expected.get(key) != value:
                _raise(
                    f"Регистрация {name} в repository config не соответствует штатному direct route."
                )
        result[name] = values
    return result


def _validate_value(name: str, key: str, value: object) -> object:
    if key in {
        "endpoint",
        "image",
        "command",
        "route",
        "credential_env",
        "wsl_distribution",
        "review_clone",
        "executable",
    }:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 1024:
            _raise(f"Параметр {name}.{key} имеет неверное значение.")
        value = value.strip()
    if key == "endpoint":
        from .mcp_client import validate_endpoint

        try:
            validate_endpoint(value, allow_http=name == "grafana")
        except ValueError as exc:
            raise ToolingError(
                ResultCode.TOOLING_PRECONDITION_FAILED,
                f"Параметр {name}.endpoint имеет неверный формат.",
            ) from exc
    if key == "image" and (
        not isinstance(value, str) or _IMAGE_RE.fullmatch(value) is None
    ):
        _raise(f"Параметр {name}.image должен быть immutable sha256 image ref.")
    if key == "command" and (
        not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value)
    ):
        _raise(f"Параметр {name}.command имеет неверное имя executable.")
    if key == "executable":
        if not isinstance(value, str):
            _raise(f"Параметр {name}.executable имеет неверный тип.")
        if "\x00" in value or "\\" in value or (
            value.startswith("/") and ".." in Path(value).parts
        ):
            _raise(f"Параметр {name}.executable имеет небезопасный путь.")
        if not value.startswith("/") and _IDENTIFIER_RE.fullmatch(value) is None:
            _raise(f"Параметр {name}.executable имеет неверное имя.")
    if key == "credential_file":
        if not isinstance(value, str):
            _raise(f"Параметр {name}.credential_file имеет неверный тип.")
        path = Path(value)
        if not path.is_absolute() or "\x00" in value or ".." in path.parts:
            _raise(f"Параметр {name}.credential_file имеет небезопасный путь.")
        if path_has_link(path):
            _raise(f"Параметр {name}.credential_file содержит symlink или reparse point.")
        if path.exists() and not path.is_file():
            _raise(f"Параметр {name}.credential_file не является файлом.")
        return str(canonical_path(path))
    if key == "ruleset":
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value.strip()) > 1024
            or "\x00" in value
        ):
            _raise(f"Параметр {name}.ruleset имеет неверный тип.")
        path = Path(value.strip())
        if path.is_absolute() or ".." in path.parts:
            _raise(f"Параметр {name}.ruleset имеет небезопасный путь.")
        return path.as_posix()
    if key == "credential_env" and (
        not isinstance(value, str)
        or _ENV_NAME_RE.fullmatch(value) is None
        or value not in INTEGRATION_CREDENTIAL_ENVIRONMENT_KEYS
    ):
        _raise(f"Параметр {name}.credential_env имеет неверное имя переменной.")
    if key == "args":
        if not isinstance(value, list) or len(value) > 32 or any(
            not isinstance(item, str) or "\x00" in item or len(item) > 1024 for item in value
        ):
            _raise(f"Параметр {name}.args имеет неверную схему.")
        return tuple(value)
    return value


def load_integration_config(root: Path) -> IntegrationConfig:
    """Собрать defaults, repo direct registration и explicit user overrides."""

    values: dict[str, dict[str, object]] = {}
    sources: dict[str, str] = {}
    for name, table in _repo_mcp_table(root).items():
        values.setdefault(name, {}).update(table)
        sources.setdefault(name, "repository_config")

    for path, source in _candidate_config_paths():
        if not path.is_file():
            continue
        document = _read_toml(path)
        for name, table in _provider_table(document).items():
            values.setdefault(name, {}).update(table)
            sources[name] = source

    for name, overrides in _ENV_OVERRIDES.items():
        for key, variable in overrides.items():
            raw = os.environ.get(variable, "").strip()
            if raw:
                values.setdefault(name, {})[key] = raw
                sources[name] = "environment"

    normalized: dict[str, dict[str, object]] = {}
    for name in DEFAULTS:
        merged = values.get(name, {})
        normalized[name] = {
            key: _validate_value(name, key, value) for key, value in merged.items()
        }
    return IntegrationConfig(normalized, sources)


__all__ = ["DEFAULTS", "IntegrationConfig", "load_integration_config"]
