"""Постоянная проверка direct external-integration contract."""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

import yaml

from azurpilot.integrations import IntegrationRegistry

EXPECTED_FAMILIES = (
    "coderabbit",
    "semgrep",
    "grafana",
    "context7",
    "docker-docs",
    "docker-hub",
)
DIRECT_CODEX_REGISTRATIONS = {
    "context7_direct",
    "docker_docs_direct",
    "grafana_direct",
    "dockerhub_direct",
    "semgrep_local_direct",
}
REQUIRED_COMPOSE_SERVICES = {
    "caddy",
    "postgres",
    "pgadmin",
    "grafana",
    "loki",
    "tempo",
    "prometheus",
}
REQUIRED_OBSERVABILITY_VOLUMES = {
    "postgres-data",
    "pgadmin-data",
    "alloy-data",
    "loki-data",
    "prometheus-data",
    "tempo-data",
    "grafana-data",
}
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
RETIRED_PROFILE_PATHS = (
    Path(".docker/azurpilot-development-profile.json"),
    Path(".docker/azurpilot-observability-profile.json"),
)
_IMAGE_RE = re.compile(r"^mcp/(?:grafana|dockerhub)@sha256:[0-9a-f]{64}$")
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
        errors.append("registry: closed family set drift")


def _check_codex_config(root: Path, errors: list[str]) -> None:
    path = root / ".codex" / "config.toml"
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        errors.append(".codex/config.toml: cannot parse source config")
        return
    servers = document.get("mcp_servers")
    if not isinstance(servers, Mapping):
        errors.append(".codex/config.toml: mcp_servers missing")
        return
    if any(str(name).casefold() == "mcp_" + "docker" for name in servers):
        errors.append(".codex/config.toml: retired toolkit registration present")
    if not DIRECT_CODEX_REGISTRATIONS <= set(servers):
        errors.append(".codex/config.toml: direct registration set incomplete")
    for name in DIRECT_CODEX_REGISTRATIONS:
        entry = servers.get(name)
        if not isinstance(entry, Mapping):
            errors.append(f".codex/config.toml: {name} is not a table")
            continue
        if entry.get("enabled") is not True or entry.get("required") is not False:
            errors.append(f".codex/config.toml: {name} must be optional and enabled")
    expected_urls = {
        "context7_direct": "https://mcp.context7.com/mcp",
        "docker_docs_direct": "https://mcp-docs.docker.com/mcp",
    }
    for name, expected_url in expected_urls.items():
        entry = servers.get(name)
        if isinstance(entry, Mapping) and entry.get("url") != expected_url:
            errors.append(f".codex/config.toml: {name} endpoint drift")
    semgrep = servers.get("semgrep_local_direct")
    if isinstance(semgrep, Mapping) and (
        semgrep.get("command") != "semgrep"
        or semgrep.get("args") != ["mcp", "-t", "stdio"]
    ):
        errors.append(".codex/config.toml: Semgrep direct route drift")
    for name, image_name, required_flags in (
        ("grafana_direct", "grafana", {"-transport", "stdio", "-disable-write", "-disable-proxied"}),
        ("dockerhub_direct", "dockerhub", set()),
    ):
        entry = servers.get(name)
        args = entry.get("args") if isinstance(entry, Mapping) else None
        image = (
            next(
                (
                    item
                    for item in args
                    if isinstance(item, str) and item.startswith(f"mcp/{image_name}@sha256:")
                ),
                None,
            )
            if isinstance(args, list)
            else None
        )
        if (
            not isinstance(entry, Mapping)
            or entry.get("command") != "docker"
            or not isinstance(image, str)
            or _IMAGE_RE.fullmatch(image) is None
            or not required_flags <= set(args or ())
        ):
            errors.append(f".codex/config.toml: {name} must use immutable direct image")


def _check_active_text(root: Path, errors: list[str]) -> None:
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
                errors.append(f"{_relative(root, path)}: source cannot be read")
                continue
            for marker in _LEGACY_MARKERS:
                if marker in text:
                    errors.append(f"{_relative(root, path)}: retired route marker present")
            if any(pattern.search(text) for pattern in _MACHINE_PATTERNS):
                errors.append(f"{_relative(root, path)}: machine-specific marker present")


def _check_retired_paths(root: Path, errors: list[str]) -> None:
    for relative in RETIRED_PROFILE_PATHS:
        if (root / relative).exists():
            errors.append(f"{relative.as_posix()}: retired profile must be absent")


def _check_infrastructure(root: Path, errors: list[str]) -> None:
    path = root / "infrastructure" / "observability" / "compose.yaml"
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        errors.append("infrastructure/observability/compose.yaml: cannot parse")
        return
    if not isinstance(document, Mapping):
        errors.append("infrastructure/observability/compose.yaml: root is not a mapping")
        return
    services = document.get("services")
    volumes = document.get("volumes")
    if not isinstance(services, Mapping) or not REQUIRED_COMPOSE_SERVICES <= set(services):
        errors.append("infrastructure/observability/compose.yaml: required services missing")
    if not isinstance(volumes, Mapping) or not REQUIRED_OBSERVABILITY_VOLUMES <= set(volumes):
        errors.append("infrastructure/observability/compose.yaml: observability volumes missing")


def check(root: Path) -> dict[str, object]:
    """Проверить текущий direct contract без base snapshot или live secrets."""

    repository_root = root.resolve()
    errors: list[str] = []
    _check_registry(errors)
    _check_codex_config(repository_root, errors)
    _check_active_text(repository_root, errors)
    _check_retired_paths(repository_root, errors)
    _check_infrastructure(repository_root, errors)
    return {
        "ok": not errors,
        "code": "INTEGRATION_CONTRACT_READY" if not errors else "INTEGRATION_CONTRACT_DRIFT",
        "families": list(EXPECTED_FAMILIES),
        "checks": {
            "registry": "ready" if not any(item.startswith("registry:") for item in errors) else "drift",
            "codex_config": "ready" if not any(item.startswith(".codex/config.toml:") for item in errors) else "drift",
            "active_text": "ready" if not any("marker present" in item for item in errors) else "drift",
            "retired_paths": "ready" if not any("retired profile" in item for item in errors) else "drift",
            "observability_infrastructure": "ready" if not any(item.startswith("infrastructure/") for item in errors) else "drift",
        },
        "errors": errors[:64],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Проверить постоянный direct contract внешних интеграций.")
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
    "check",
    "main",
]
