"""Проверка repository-level инвариантов прямых внешних интеграций.

Gate намеренно не хранит второй снимок runtime-конфигурации. Канонические
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

from azurpilot.integrations import IntegrationRegistry
from azurpilot.integrations.adapters import (
    DOCKER_HUB_BLOCKED_TOOLS,
    DOCKER_HUB_READ_ONLY_TOOLS,
)
from azurpilot.integrations.config import DEFAULTS, _REPO_MCP_ALIASES
from azurpilot.integrations.contracts import IntegrationName

EXPECTED_FAMILIES = tuple(name.value for name in IntegrationName)
_CODEX_FAMILIES = frozenset(
    name.value for name in IntegrationName if name is not IntegrationName.CODERABBIT
)

RETIRED_PROFILE_PATHS = (
    Path(".docker/azurpilot-development-profile.json"),
    Path(".docker/azurpilot-observability-profile.json"),
)

ACTIVE_SOURCE_PATHS = (
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
    Path(".agents/skills/azurpilot-repository-development/references/engineering-contract.md"),
    Path(".agents/skills/azurpilot-repository-development/references/pr-merge-cleanup.md"),
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
    re.compile(r"(?i)/home/[a-z0-9._-]+(?:/|$)"),
    re.compile(r"(?i)[a-z]:[\\/]+azurpilot(?:[\\/]|$)"),
    re.compile(r"(?i)\\\\wsl(?:\.localhost|\$)[\\/]"),
    re.compile(r"(?i)\$home[\\/][a-z0-9._-]+"),
)


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _check_registry(errors: list[str]) -> None:
    actual = tuple(adapter.name.value for adapter in IntegrationRegistry().adapters)
    if actual != EXPECTED_FAMILIES:
        errors.append("registry: нарушен закрытый набор семейств")


def _direct_entries(
    servers: Mapping[object, object], errors: list[str]
) -> dict[str, tuple[str, Mapping[object, object]]]:
    """Сопоставить repository registrations с canonical provider families."""

    result: dict[str, tuple[str, Mapping[object, object]]] = {}
    for raw_name, raw_entry in servers.items():
        name = str(raw_name)
        family = _REPO_MCP_ALIASES.get(name)
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

        args = _string_list(entry, "args")
        canonical_image = DEFAULTS[family].get("image")
        if (
            entry.get("command") != DEFAULTS[family].get("command")
            or args is None
            or not isinstance(canonical_image, str)
            or canonical_image not in args
        ):
            errors.append(
                f".codex/config.toml: {registration} расходится с canonical container route"
            )
            continue

        if family == "grafana":
            # Единственный repository-level safety invariant для server args:
            # Codex route обязан запрещать mutating Grafana tools. Наличие
            # proxied/Tempo surface определяется production adapter contract,
            # а не дублируется здесь отдельным флагом.
            if "-disable-write" not in args:
                errors.append(
                    ".codex/config.toml: grafana_direct обязан быть read-only"
                )
            env_vars = _string_list(entry, "env_vars")
            expected_env = {
                "GRAFANA_URL",
                str(DEFAULTS[family].get("credential_env")),
            }
            if env_vars is None or not expected_env.issubset(env_vars):
                errors.append(
                    ".codex/config.toml: grafana_direct не наследует canonical env names"
                )
            continue

        if family == "docker-hub":
            enabled_tools = _string_list(entry, "enabled_tools")
            disabled_tools = _string_list(entry, "disabled_tools")
            if enabled_tools is None or set(enabled_tools) != set(
                DOCKER_HUB_READ_ONLY_TOOLS
            ):
                errors.append(
                    ".codex/config.toml: dockerhub_direct allowlist расходится с adapter contract"
                )
            if disabled_tools is None or not set(disabled_tools).issubset(
                DOCKER_HUB_BLOCKED_TOOLS
            ):
                errors.append(
                    ".codex/config.toml: dockerhub_direct denylist содержит неизвестный write tool"
                )


def _check_active_text(root: Path, errors: list[str]) -> None:
    """Migration guard: активные surfaces не должны вернуть retired gateway route."""

    paths = list(ACTIVE_SOURCE_PATHS) + [Path("azurpilot/integrations")]
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
                        f"{_relative(root, path)}: обнаружен marker устаревшего route"
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


def _run_check(
    check_id: str,
    checker: Callable[[list[str]], None],
) -> tuple[str, tuple[str, ...]]:
    errors: list[str] = []
    checker(errors)
    return check_id, tuple(errors)


def check(root: Path) -> dict[str, object]:
    """Проверить direct integration policy без live secrets и второго snapshot."""

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
