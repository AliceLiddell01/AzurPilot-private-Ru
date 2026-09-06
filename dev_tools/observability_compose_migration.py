"""Безопасная одноразовая миграция старого Compose project observability."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

CANONICAL_PROJECT = "azurpilot-infrastructure"
LEGACY_PROJECT = "azurpilot-observability"
LEGACY_VOLUMES = (
    "azurpilot-observability_alloy-data",
    "azurpilot-observability_grafana-data",
    "azurpilot-observability_loki-data",
    "azurpilot-observability_prometheus-data",
    "azurpilot-observability_tempo-data",
)
VOLUME_SERVICES = {
    "azurpilot-observability_alloy-data": "alloy",
    "azurpilot-observability_grafana-data": "grafana",
    "azurpilot-observability_loki-data": "loki",
    "azurpilot-observability_prometheus-data": "prometheus",
    "azurpilot-observability_tempo-data": "tempo",
}


class ComposeMigrationError(RuntimeError):
    """Операция миграции остановлена до небезопасного изменения состояния."""


def _docker_executable() -> str:
    executable = shutil.which("docker.exe") or shutil.which("docker")
    if executable is None:
        raise ComposeMigrationError("DOCKER_CLI_UNAVAILABLE")
    return executable


def _run(arguments: list[str], *, timeout: int = 120) -> str:
    options: dict[str, object] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            [_docker_executable(), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
            **options,
        )
    except subprocess.TimeoutExpired as exc:
        raise ComposeMigrationError("DOCKER_COMMAND_TIMEOUT") from exc
    if result.returncode != 0:
        raise ComposeMigrationError("DOCKER_COMMAND_FAILED")
    return result.stdout


def _json_lines(raw: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ComposeMigrationError("DOCKER_JSON_INVALID") from exc
        if not isinstance(value, dict):
            raise ComposeMigrationError("DOCKER_JSON_INVALID")
        records.append(value)
    return records


def _project_containers(project: str) -> list[dict[str, Any]]:
    return _json_lines(
        _run(
            [
                "container",
                "ls",
                "--all",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--format",
                "{{json .}}",
            ]
        )
    )


def _project_networks(project: str) -> list[dict[str, Any]]:
    return _json_lines(
        _run(
            [
                "network",
                "ls",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--format",
                "{{json .}}",
            ]
        )
    )


def _volume_exists(name: str) -> bool:
    try:
        _run(["volume", "inspect", name])
    except ComposeMigrationError as exc:
        if str(exc) == "DOCKER_COMMAND_FAILED":
            return False
        raise
    return True


def _volume_metadata(name: str) -> dict[str, Any]:
    raw = _run(["volume", "inspect", name])
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ComposeMigrationError("DOCKER_JSON_INVALID") from exc
    if not isinstance(payload, list) or len(payload) != 1:
        raise ComposeMigrationError("DOCKER_VOLUME_INSPECTION_INVALID")
    value = payload[0]
    if not isinstance(value, dict):
        raise ComposeMigrationError("DOCKER_VOLUME_INSPECTION_INVALID")
    labels = value.get("Labels")
    selected_labels = {}
    if isinstance(labels, dict):
        for key in ("com.docker.compose.project", "com.docker.compose.volume"):
            if isinstance(labels.get(key), str):
                selected_labels[key] = labels[key]
    return {"name": name, "exists": True, "labels": selected_labels}


def inventory() -> dict[str, Any]:
    """Собрать безопасный inventory old project и ожидаемых data volumes."""

    containers = _project_containers(LEGACY_PROJECT)
    networks = _project_networks(LEGACY_PROJECT)
    volumes = [
        _volume_metadata(name) for name in LEGACY_VOLUMES if _volume_exists(name)
    ]
    return {
        "canonical_project": CANONICAL_PROJECT,
        "legacy_project": LEGACY_PROJECT,
        "legacy_containers": [
            {
                "id": item.get("ID"),
                "name": item.get("Names") or item.get("Name"),
                "state": item.get("State"),
                "service": item.get("Service"),
            }
            for item in containers
        ],
        "legacy_networks": [
            {"id": item.get("ID"), "name": item.get("Name")}
            for item in networks
        ],
        "legacy_persistent_volumes": volumes,
        "mode": (
            "migration"
            if containers or networks or volumes
            else "fresh"
        ),
    }


def _compose_arguments(repository_root: Path, *arguments: str) -> list[str]:
    repository_root = repository_root.resolve(strict=True)
    env_file = repository_root / ".env"
    compose_file = repository_root / "infrastructure/observability/compose.yaml"
    if not env_file.is_file() or not compose_file.is_file():
        raise ComposeMigrationError("CANONICAL_COMPOSE_UNAVAILABLE")
    return [
        "compose",
        "--env-file",
        str(env_file),
        "--file",
        str(compose_file),
        *arguments,
    ]


def _run_compose(repository_root: Path, *arguments: str, timeout: int = 300) -> str:
    return _run(_compose_arguments(repository_root, *arguments), timeout=timeout)


def _require_legacy_volumes(state: dict[str, Any]) -> None:
    present = {
        item["name"]
        for item in state["legacy_persistent_volumes"]
        if item.get("exists") is True
    }
    missing = [name for name in LEGACY_VOLUMES if name not in present]
    if missing:
        raise ComposeMigrationError("LEGACY_PERSISTENT_VOLUME_MISSING")


def _create_fresh_legacy_volumes() -> None:
    for name in LEGACY_VOLUMES:
        if not _volume_exists(name):
            created = _run(["volume", "create", name], timeout=60).strip()
            if created != name:
                raise ComposeMigrationError("LEGACY_VOLUME_CREATE_MISMATCH")


def _remove_legacy_project(state: dict[str, Any]) -> None:
    containers = state["legacy_containers"]
    container_ids = [item["id"] for item in containers if item.get("id")]
    running_ids = [
        item["id"]
        for item in containers
        if item.get("id") and str(item.get("state", "")).lower().startswith("running")
    ]
    if running_ids:
        _run(["container", "stop", "--time", "30", *running_ids], timeout=90)
    if container_ids:
        _run(["container", "rm", *container_ids], timeout=90)
    if _project_containers(LEGACY_PROJECT):
        raise ComposeMigrationError("LEGACY_CONTAINERS_REMAIN")

    network_ids = [
        item["id"] for item in state["legacy_networks"] if item.get("id")
    ]
    if network_ids:
        _run(["network", "rm", *network_ids], timeout=60)
    if _project_networks(LEGACY_PROJECT):
        raise ComposeMigrationError("LEGACY_NETWORKS_REMAIN")


def _compose_records(repository_root: Path) -> list[dict[str, Any]]:
    return _json_lines(
        _run_compose(repository_root, "ps", "--all", "--format", "json")
    )


def _service_container_id(repository_root: Path, service: str) -> str:
    output = _run_compose(repository_root, "ps", "-q", service).strip()
    container_id = output.splitlines()[0].strip() if output else ""
    if not container_id:
        raise ComposeMigrationError("CANONICAL_SERVICE_MISSING")
    return container_id


def _verify_canonical_mounts(repository_root: Path) -> None:
    for volume_name, service in VOLUME_SERVICES.items():
        container_id = _service_container_id(repository_root, service)
        try:
            mounts = json.loads(
                _run(
                    [
                        "inspect",
                        "--format",
                        "{{json .Mounts}}",
                        container_id,
                    ]
                )
            )
        except json.JSONDecodeError as exc:
            raise ComposeMigrationError("DOCKER_MOUNT_INSPECTION_INVALID") from exc
        if not isinstance(mounts, list) or volume_name not in {
            mount.get("Name")
            for mount in mounts
            if isinstance(mount, dict)
        }:
            raise ComposeMigrationError("OBSERVABILITY_VOLUME_NOT_REUSED")


def _verify_canonical_project(repository_root: Path) -> list[dict[str, Any]]:
    records = _compose_records(repository_root)
    expected_services = {"postgres", "pgadmin", *VOLUME_SERVICES.values()}
    observed_services = {
        record.get("Service") for record in records if record.get("Service")
    }
    if not expected_services.issubset(observed_services):
        raise ComposeMigrationError("CANONICAL_SERVICE_MISSING")
    for record in records:
        if record.get("Service") not in expected_services:
            continue
        state = str(record.get("State", "")).casefold()
        health = str(record.get("Health", "")).casefold()
        if state not in {"running", "up"} or health in {"unhealthy", "starting"}:
            raise ComposeMigrationError("CANONICAL_SERVICE_UNHEALTHY")
    _verify_canonical_mounts(repository_root)
    return records


def migrate(repository_root: Path) -> dict[str, Any]:
    """Мигрировать old project без удаления persistent volumes и проверить новый."""

    state = inventory()
    if state["mode"] == "fresh":
        _create_fresh_legacy_volumes()
        mode = "fresh"
    else:
        _require_legacy_volumes(state)
        _remove_legacy_project(state)
        mode = "migration"

    _run_compose(repository_root, "config", "--quiet", timeout=60)
    _run_compose(repository_root, "up", "--detach", "--wait", timeout=360)
    records = _verify_canonical_project(repository_root)
    final_state = inventory()
    if (
        mode == "migration"
        and final_state["legacy_persistent_volumes"]
        != state["legacy_persistent_volumes"]
    ):
        raise ComposeMigrationError("LEGACY_VOLUME_STATE_CHANGED")
    return {
        "mode": mode,
        "canonical_project": CANONICAL_PROJECT,
        "legacy_project": LEGACY_PROJECT,
        "services": sorted(
            record.get("Service")
            for record in records
            if record.get("Service")
        ),
        "persistent_volumes": list(LEGACY_VOLUMES),
        "legacy_containers_removed": len(state["legacy_containers"]),
        "legacy_networks_removed": len(state["legacy_networks"]),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Безопасная миграция Compose project observability AzurPilot."
    )
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("inventory", help="Только read-only inventory Docker state.")
    subparsers.add_parser("migrate", help="Мигрировать и проверить canonical project.")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        repository_root = arguments.repository_root.resolve(strict=True)
        payload = (
            inventory()
            if arguments.command == "inventory"
            else migrate(repository_root)
        )
    except (ComposeMigrationError, OSError, ValueError) as exc:
        print(f"ERROR:{exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
