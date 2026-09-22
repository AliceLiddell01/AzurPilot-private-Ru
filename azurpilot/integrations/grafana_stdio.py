"""Repository-owned stdio launcher для canonical Grafana MCP route."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from azurpilot.tooling.errors import ToolingError
from azurpilot.tooling.repository import RepositoryResolver

from .adapters import GrafanaAdapter
from .config import load_integration_config


class GrafanaLauncherError(RuntimeError):
    """Canonical Grafana child command не удалось доказать."""


def _cleanup_child(process: subprocess.Popen[bytes]) -> None:
    """Bounded cleanup child process after launcher interruption."""

    if process.poll() is not None:
        return
    try:
        process.terminate()
    except (OSError, ProcessLookupError):
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def resolve_child_command() -> tuple[Path, tuple[str, ...], dict[str, str]]:
    """Собрать child command только через repository-owned GrafanaAdapter."""

    root = RepositoryResolver().resolve().path
    config = load_integration_config(root)
    command = GrafanaAdapter().build_command(root, config)
    if command is None:
        raise GrafanaLauncherError("GRAFANA_DIRECT_ROUTE_NOT_CONFIGURED")
    executable, args, environment = command
    return root, (executable, *args), environment


def main() -> int:
    """Прозрачно передать stdio реальному pinned Grafana MCP child."""

    try:
        root, command, environment = resolve_child_command()
        process = subprocess.Popen(
            command,
            cwd=root,
            env=environment,
        )
    except (GrafanaLauncherError, OSError, ToolingError, ValueError):
        print(
            "Grafana MCP route не удалось доказать; child process не запущен.",
            file=sys.stderr,
        )
        return 1
    try:
        return process.wait()
    finally:
        _cleanup_child(process)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["GrafanaLauncherError", "main", "resolve_child_command"]
