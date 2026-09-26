"""Проверка repository-level инвариантов прямых внешних интеграций.

Проверка намеренно не хранит второй снимок runtime-конфигурации. Канонические
семейства, vendor endpoints, immutable image refs и tool allowlists берутся
из production integration contracts; здесь остаются только независимые
repository policy-инварианты.
"""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path

import yaml

from azurpilot.integrations import IntegrationRegistry
from azurpilot.integrations.adapters import (
    DOCKER_HUB_BLOCKED_TOOLS,
    DOCKER_HUB_READ_ONLY_TOOLS,
    GRAFANA_BLOCKED_TOOLS,
    GRAFANA_ENABLED_TOOL_CATEGORIES,
    GRAFANA_READ_ONLY_TOOLS,
    GRAFANA_REQUIRED_READ_ONLY_TOOLS,
)
from azurpilot.integrations.config import (
    DEFAULTS,
    REPOSITORY_MCP_ALIASES,
    SHARED_MCP_ROUTE,
)
from azurpilot.integrations.contracts import IntegrationName

EXPECTED_FAMILIES = tuple(name.value for name in IntegrationName)
_CODEX_FAMILIES = frozenset(
    name.value for name in IntegrationName if name is not IntegrationName.CODERABBIT
)

RETIRED_PROFILE_PATHS = (
    Path(".docker/azurpilot-development-profile.json"),
    Path(".docker/azurpilot-observability-profile.json"),
)

_ACTIVE_SOURCE_PATHS = (
    Path(".codex/config.toml"),
    Path("azurpilot/cli.py"),
    Path("azurpilot/tooling/doctor.py"),
    Path("azurpilot/tooling/process.py"),
    Path("dev_tools/mcp_status.py"),
    Path("dev_tools/observability_mcp.py"),
    Path("dev_tools/observability_reliability.py"),
    Path("docs/dev-runtime.md"),
    Path("infrastructure/observability/README.md"),
    Path(".agents/skills/azurpilot-coderabbit-review/SKILL.md"),
    Path(".agents/skills/azurpilot-coderabbit-review/references/review-workflow.md"),
    Path(".agents/skills/azurpilot-repository-development/SKILL.md"),
    Path("plugins/azurpilot/skills/azurpilot-development/SKILL.md"),
    Path("plugins/azurpilot/skills/azurpilot-troubleshooting/SKILL.md"),
)

_LEGACY_MARKERS = (
    "mcp_" + "docker",
    "docker" + " mcp " + "gateway",
    "docker" + " secrets " + "engine",
    "gateway_" + "required",
    "canonical_" + "docker_" + "profile",
)
_MACHINE_PATTERNS = (
    re.compile(r"(?i)(?<![a-z0-9._-])/home/[a-z0-9._-]+(?:/|$)"),
    re.compile(r"(?i)[a-z]:[\\/]+azurpilot(?:[\\/]|$)"),
    re.compile(r"(?i)\\\\wsl(?:\.localhost|\$)[\\/]"),
    re.compile(r"(?i)\$home[\\/][a-z0-9._-]+"),
)
_OPERATOR_LAUNCHER_TOKENS = re.compile(
    r"(?i)(?:\bazur(?:\.exe)?(?![-\w])|\bpython(?:3(?:\.\d+)?)?(?:\.exe)?\s+-m\s+azurpilot(?![-\w]))"
)
_UV_RUN_TOKEN = re.compile(r"(?i)\buv\s+run\b")
_OPERATOR_LAUNCHER_NEGATION = re.compile(
    r"(?i)\b(?:запрещ\w*|forbidden|prohibited|disallowed|not\s+allowed)\b"
)
_CODERABBIT_RETIRED_MARKERS = (
    "direct_wsl_agent",
    "coderabbit-runtime.json",
    "managed review clone",
    "wsl.exe --list",
    "pgrep -x coderabbit",
)


def _contains_prohibited_operator_launcher(text: str) -> bool:
    """Найти положительное описание запрещённого uv/module launcher."""

    for raw_line in text.splitlines():
        normalized = re.sub(r"\s+", " ", raw_line.casefold())
        for match in _UV_RUN_TOKEN.finditer(normalized):
            suffix = normalized[match.end() :]
            if not _OPERATOR_LAUNCHER_TOKENS.search(suffix):
                continue
            if _OPERATOR_LAUNCHER_NEGATION.search(normalized):
                continue
            return True
    return False
_OPERATOR_POLICY_MARKER_OWNERS = {
    "source_reconciled": Path(".codex/context/11-PYTHON-TOOLING.md"),
    "runtime_ready": Path(".codex/context/11-PYTHON-TOOLING.md"),
    "TOOLING_STACKED_PARENT_UNPUBLISHED": Path(
        ".codex/context/11-PYTHON-TOOLING.md"
    ),
    "codex/base-*": Path(".codex/context/GIT-WORKFLOW.md"),
}


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _check_registry(errors: list[str]) -> None:
    actual = tuple(adapter.name.value for adapter in IntegrationRegistry().adapters)
    if actual != EXPECTED_FAMILIES:
        errors.append("registry: нарушен закрытый набор семейств")


def _direct_entries(
    servers: Mapping[object, object], errors: list[str]
) -> dict[str, tuple[str, Mapping[object, object]]]:
    """Сопоставить регистрации репозитория с каноническими семействами провайдеров."""

    result: dict[str, tuple[str, Mapping[object, object]]] = {}
    for raw_name, raw_entry in servers.items():
        name = str(raw_name)
        family = REPOSITORY_MCP_ALIASES.get(name)
        if family is None:
            continue
        if not isinstance(raw_entry, Mapping):
            errors.append(f".codex/config.toml: {name} не является таблицей")
            continue
        if family in result:
            errors.append(
                f".codex/config.toml: семейство {family} зарегистрировано более одного раза"
            )
            continue
        result[family] = (name, raw_entry)
    return result


def _string_list(entry: Mapping[object, object], key: str) -> tuple[str, ...] | None:
    value = entry.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    return tuple(value)


def _check_codex_config(root: Path, errors: list[str]) -> None:
    """Проверить shape и safety policy без копирования canonical values."""

    path = root / ".codex" / "config.toml"
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        errors.append(".codex/config.toml: не удалось разобрать source config")
        return

    servers = document.get("mcp_servers")
    if not isinstance(servers, Mapping):
        errors.append(".codex/config.toml: отсутствует mcp_servers")
        return
    if any(str(name).casefold() == "mcp_" + "docker" for name in servers):
        errors.append(".codex/config.toml: обнаружена устаревшая toolkit registration")

    entries = _direct_entries(servers, errors)
    missing = _CODEX_FAMILIES.difference(entries)
    if missing:
        errors.append(
            ".codex/config.toml: отсутствуют direct registrations: "
            + ", ".join(sorted(missing))
        )

    for family, (registration, entry) in entries.items():
        if entry.get("enabled") is not True or entry.get("required") is not False:
            errors.append(
                f".codex/config.toml: {registration} должен быть optional и enabled"
            )

        if family in {"context7", "docker-docs"}:
            if entry.get("url") != DEFAULTS[family].get("endpoint"):
                errors.append(
                    f".codex/config.toml: {registration} расходится с canonical endpoint"
                )
            continue

        if family == "semgrep":
            if entry.get("command") != DEFAULTS[family].get("command"):
                errors.append(
                    ".codex/config.toml: semgrep registration расходится с canonical command"
                )
            if _string_list(entry, "args") != ("mcp", "-t", "stdio"):
                errors.append(
                    ".codex/config.toml: semgrep registration имеет неверный MCP transport"
                )
            continue

        if family in _SHARED_MCP_FAMILIES:
            _check_shared_registration(
                root, family, registration, entry, errors
            )
            continue

        errors.append(
            f".codex/config.toml: {registration} использует неизвестный route"
        )

    compose = _load_compose(root, errors)
    if compose is not None:
        for family in _SHARED_MCP_FAMILIES:
            _check_shared_service(root, compose, family, errors)


_SHARED_MCP_FAMILIES = ("grafana", "docker-hub")
_IMMUTABLE_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}@sha256:[0-9a-f]{64}$")
_SOURCE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_COMPOSE_PATH = Path("infrastructure") / "observability" / "compose.yaml"
_REPOSITORY_OWNED_IMAGE_PREFIX = "azurpilot-infrastructure/"
_SHARED_MCP_PROFILE = "external-mcp"
_FORBIDDEN_REGISTRATION_KEYS = (
    "command",
    "args",
    "cwd",
    "image",
    "credential_env_var",
    "env_vars",
)


def _load_compose(root: Path, errors: list[str]) -> Mapping[object, object] | None:
    path = root / _COMPOSE_PATH
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        errors.append(f"{_COMPOSE_PATH.as_posix()}: не удалось разобрать Compose owner")
        return None
    if not isinstance(document, Mapping):
        errors.append(f"{_COMPOSE_PATH.as_posix()}: корень Compose не является отображением")
        return None
    return document


def _environment_names(service: Mapping[object, object]) -> str:
    environment = service.get("environment")
    if isinstance(environment, Mapping):
        return " ".join(str(value) for value in environment.values())
    if isinstance(environment, list):
        return " ".join(str(item) for item in environment)
    return ""


def _loopback_publication(service: Mapping[object, object], endpoint: object) -> bool:
    """Проверить, что общий сервис опубликован только на loopback-порт endpoint-а."""

    if not isinstance(endpoint, str):
        return False
    match = re.search(r":([0-9]{2,5})/", endpoint)
    if match is None:
        return False
    expected = f"127.0.0.1:{match.group(1)}"
    ports = _string_list(service, "ports")
    if ports is None:
        return False
    return any(
        item.replace(" ", "").startswith(f"{expected}:") for item in ports
    )


def _check_shared_service(
    root: Path,
    compose: Mapping[object, object],
    family: str,
    errors: list[str],
) -> None:
    """Проверить Compose-владельца общего HTTP service для одного семейства."""

    services = compose.get("services")
    service_name = str(DEFAULTS[family].get("compose_service", ""))
    service: object = (
        services.get(service_name) if isinstance(services, Mapping) else None
    )
    if not isinstance(service, Mapping):
        errors.append(f"{_COMPOSE_PATH.as_posix()}: отсутствует service {service_name}")
        return
    profiles = _string_list(service, "profiles")
    if profiles is None or _SHARED_MCP_PROFILE not in profiles:
        errors.append(
            f"{_COMPOSE_PATH.as_posix()}: {service_name} должен быть в профиле "
            f"{_SHARED_MCP_PROFILE}"
        )
    if service.get("read_only") is not True:
        errors.append(
            f"{_COMPOSE_PATH.as_posix()}: {service_name} должен иметь read-only rootfs"
        )
    if not _loopback_publication(service, DEFAULTS[family].get("endpoint")):
        errors.append(
            f"{_COMPOSE_PATH.as_posix()}: {service_name} должен публиковаться только "
            "на loopback-порт общего endpoint"
        )
    caller_env = str(DEFAULTS[family].get("caller_token_env", ""))
    if caller_env not in _environment_names(service):
        errors.append(
            f"{_COMPOSE_PATH.as_posix()}: {service_name} обязан получать caller token "
            "общего сервиса из окружения"
        )
    if service.get("healthcheck") is None:
        errors.append(
            f"{_COMPOSE_PATH.as_posix()}: {service_name} должен иметь healthcheck"
        )

    image = service.get("image")
    build = service.get("build")
    if family == "grafana":
        if not isinstance(image, str) or _IMMUTABLE_IMAGE_RE.fullmatch(image) is None:
            errors.append(
                f"{_COMPOSE_PATH.as_posix()}: {service_name} обязан использовать "
                "immutable image digest"
            )
        command = " ".join(_string_list(service, "command") or ())
        if "--transport=streamable-http" not in command:
            errors.append(
                f"{_COMPOSE_PATH.as_posix()}: {service_name} обязан обслуживать "
                "Streamable HTTP transport"
            )
        if "--disable-write" not in command or "--disable-api" not in command:
            errors.append(
                f"{_COMPOSE_PATH.as_posix()}: {service_name} обязан отключать write и API tools"
            )
        if "--disable-query" in command:
            errors.append(
                f"{_COMPOSE_PATH.as_posix()}: {service_name} не должен отключать read-only query tools"
            )
        if GRAFANA_ENABLED_TOOL_CATEGORIES not in command:
            errors.append(
                f"{_COMPOSE_PATH.as_posix()}: {service_name} расходится с read-only "
                "категориями adapter contract"
            )
        entrypoint = _string_list(service, "entrypoint")
        if entrypoint is None or not any(
            str(item).startswith("/opt/azurpilot/") for item in entrypoint
        ):
            errors.append(
                f"{_COMPOSE_PATH.as_posix()}: {service_name} обязан запускаться через "
                "repository-owned entrypoint, который отказывает стартовать без caller token"
            )
        return

    # У Docker Hub MCP нет публичного immutable образа с fail-closed caller auth,
    # поэтому владелец собирает его из закреплённого commit-а исходников.
    repository_owned_image = isinstance(image, str) and image.startswith(
        _REPOSITORY_OWNED_IMAGE_PREFIX
    )
    provider_digest = (
        isinstance(image, str) and _IMMUTABLE_IMAGE_RE.fullmatch(image) is not None
    )
    if (
        not isinstance(image, str)
        or not image
        or (provider_digest and not repository_owned_image)
    ):
        errors.append(
            f"{_COMPOSE_PATH.as_posix()}: {service_name} обязан использовать "
            "repository-owned build вместо provider image"
        )
    arguments = build.get("args") if isinstance(build, Mapping) else None
    commit = (
        str(arguments.get("HUBCP_COMMIT", ""))
        if isinstance(arguments, Mapping)
        else ""
    )
    if _SOURCE_COMMIT_RE.fullmatch(commit) is None:
        errors.append(
            f"{_COMPOSE_PATH.as_posix()}: {service_name} обязан закреплять полный "
            "commit исходников"
        )


def _check_shared_registration(
    root: Path,
    family: str,
    registration: str,
    entry: Mapping[object, object],
    errors: list[str],
) -> None:
    """Проверить, что регистрация подключается к общему HTTP service, а не запускает provider."""

    settings = DEFAULTS[family]
    if settings.get("route") != SHARED_MCP_ROUTE:
        errors.append(f"config: {family} обязан использовать общий MCP HTTP route")
    for key in _FORBIDDEN_REGISTRATION_KEYS:
        if key in entry:
            errors.append(
                f".codex/config.toml: {registration} не должен владеть provider "
                f"process-ом ({key})"
            )
    if entry.get("url") != settings.get("endpoint"):
        errors.append(
            f".codex/config.toml: {registration} расходится с общим HTTP endpoint"
        )
    if entry.get("bearer_token_env_var") != settings.get("caller_token_env"):
        errors.append(
            f".codex/config.toml: {registration} обязан предъявлять caller token "
            "общего сервиса"
        )

    enabled_tools = _string_list(entry, "enabled_tools")
    disabled_tools = _string_list(entry, "disabled_tools")
    if family == "grafana":
        if enabled_tools is None or set(enabled_tools) != set(GRAFANA_READ_ONLY_TOOLS):
            errors.append(
                ".codex/config.toml: grafana_direct allowlist расходится с adapter contract"
            )
        elif not set(GRAFANA_REQUIRED_READ_ONLY_TOOLS).issubset(enabled_tools):
            errors.append(
                ".codex/config.toml: grafana_direct allowlist не содержит required query/Tempo reads"
            )
        if disabled_tools is None or set(disabled_tools) != set(GRAFANA_BLOCKED_TOOLS):
            errors.append(
                ".codex/config.toml: grafana_direct denylist расходится с adapter contract"
            )
        return
    if enabled_tools is None or set(enabled_tools) != set(DOCKER_HUB_READ_ONLY_TOOLS):
        errors.append(
            ".codex/config.toml: dockerhub_direct allowlist расходится с adapter contract"
        )
    if disabled_tools is None or set(disabled_tools) != set(DOCKER_HUB_BLOCKED_TOOLS):
        errors.append(
            ".codex/config.toml: dockerhub_direct denylist расходится с adapter contract"
        )


def _check_active_text(root: Path, errors: list[str]) -> None:
    """Защита миграции: активные области не должны вернуть устаревший gateway route."""

    paths = list(_ACTIVE_SOURCE_PATHS) + [Path("azurpilot/integrations")]
    for relative in paths:
        candidates = (
            sorted((root / relative).rglob("*.py"))
            if (root / relative).is_dir()
            else [root / relative]
        )
        for path in candidates:
            try:
                text = path.read_text(encoding="utf-8").casefold()
            except (OSError, UnicodeError):
                errors.append(
                    f"{_relative(root, path)}: исходный файл не удалось прочитать"
                )
                continue
            for marker in _LEGACY_MARKERS:
                if marker in text:
                    errors.append(
                        f"{_relative(root, path)}: обнаружен marker устаревшего маршрута"
                    )
            if any(pattern.search(text) for pattern in _MACHINE_PATTERNS):
                errors.append(
                    f"{_relative(root, path)}: обнаружен machine-specific marker"
                )


def _check_retired_paths(root: Path, errors: list[str]) -> None:
    for relative in RETIRED_PROFILE_PATHS:
        if (root / relative).exists():
            errors.append(
                f"{relative.as_posix()}: устаревший profile должен отсутствовать"
            )


def _check_coderabbit_native_boundary(root: Path, errors: list[str]) -> None:
    """Проверить, что CodeRabbit policy не возвращается к retired topology."""

    if DEFAULTS.get("coderabbit", {}).get("route") != "direct_native_agent":
        errors.append("coderabbit: canonical route должен быть direct_native_agent")
    config_source = root / "azurpilot" / "integrations" / "config.py"
    adapter_source = root / "azurpilot" / "integrations" / "coderabbit.py"
    for path in (config_source, adapter_source):
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            errors.append(f"{_relative(root, path)}: native boundary source не прочитан")
            continue
        content = raw.casefold()
        for marker in _CODERABBIT_RETIRED_MARKERS:
            if marker in content:
                errors.append(f"{_relative(root, path)}: найден retired CodeRabbit marker {marker}")
        if "coderabbit_native_windows_required" in content:
            errors.append(
                f"{_relative(root, path)}: host-native CodeRabbit boundary ошибочно ограничен Windows"
            )
        if "azurpilot_coderabbit_wsl_distribution" in content or (
            "azurpilot_coderabbit_review_clone" in content
        ):
            errors.append("coderabbit: retired host environment overrides остаются активными")


def _check_operator_workflow_boundary(root: Path, errors: list[str]) -> None:
    """Проверить literal azur path, MCP readiness split и topology policy."""

    contents: dict[Path, str] = {}
    owner_paths = tuple(dict.fromkeys(_OPERATOR_POLICY_MARKER_OWNERS.values()))
    for relative in owner_paths:
        path = root / relative
        try:
            contents[relative] = path.read_text(encoding="utf-8").casefold()
        except (OSError, UnicodeError):
            errors.append(f"{relative.as_posix()}: operator policy source не прочитан")
    for marker, owner in _OPERATOR_POLICY_MARKER_OWNERS.items():
        policy = contents.get(owner)
        if policy is not None and marker.casefold() not in policy:
            errors.append(f"operator workflow: отсутствует policy marker {marker}")
    development_skill = (
        root / "plugins" / "azurpilot" / "skills" / "azurpilot-development" / "SKILL.md"
    )
    try:
        development_text = development_skill.read_text(encoding="utf-8").casefold()
    except (OSError, UnicodeError):
        errors.append(
            "plugins/azurpilot/skills/azurpilot-development/SKILL.md: "
            "development skill source не прочитан"
        )
        return
    if _contains_prohibited_operator_launcher(development_text):
        errors.append(
            "operator workflow: plugin development skill возвращает uv/module launcher"
        )


def _run_check(
    check_id: str,
    checker: Callable[[list[str]], None],
) -> tuple[str, tuple[str, ...]]:
    errors: list[str] = []
    checker(errors)
    return check_id, tuple(errors)


def check(root: Path) -> dict[str, object]:
    """Проверить политику прямых интеграций без чтения секретов и второго снимка."""

    repository_root = root.resolve()
    results = (
        _run_check("registry", _check_registry),
        _run_check(
            "codex_config",
            lambda errors: _check_codex_config(repository_root, errors),
        ),
        _run_check(
            "active_text",
            lambda errors: _check_active_text(repository_root, errors),
        ),
        _run_check(
            "retired_paths",
            lambda errors: _check_retired_paths(repository_root, errors),
        ),
        _run_check(
            "coderabbit_native_boundary",
            lambda errors: _check_coderabbit_native_boundary(repository_root, errors),
        ),
        _run_check(
            "operator_workflow_boundary",
            lambda errors: _check_operator_workflow_boundary(repository_root, errors),
        ),
    )
    errors = [error for _check_id, check_errors in results for error in check_errors]
    return {
        "ok": not errors,
        "code": "INTEGRATION_CONTRACT_READY"
        if not errors
        else "INTEGRATION_CONTRACT_DRIFT",
        "families": list(EXPECTED_FAMILIES),
        "checks": {
            check_id: "ready" if not check_errors else "drift"
            for check_id, check_errors in results
        },
        "errors": errors[:64],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Проверить repository policy прямых внешних интеграций."
    )
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        payload = check(arguments.repository_root)
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        payload = {
            "ok": False,
            "code": "INTEGRATION_CONTRACT_UNKNOWN",
            "families": list(EXPECTED_FAMILIES),
            "errors": [type(error).__name__],
        }
    if arguments.as_json:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        status = "OK" if payload["ok"] else "ERROR"
        print(f"{status}: {payload['code']}")
        for error in payload.get("errors", ()):
            print(f"- {error}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXPECTED_FAMILIES",
    "RETIRED_PROFILE_PATHS",
    "check",
    "main",
]
