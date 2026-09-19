"""Machine-readable inventory and semantic gate for legacy operator surfaces."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LegacySurface:
    path: str
    category: str
    owner: str
    disposition: str


def _tracked(root: Path) -> tuple[str, ...]:
    output = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8", errors="surrogateescape")
    return tuple(item for item in output.split("\0") if item)


def classify(path: str) -> LegacySurface | None:
    normalized = path.replace("\\", "/")
    suffix = Path(normalized).suffix.casefold()
    if suffix not in {".ps1", ".psm1", ".sh", ".bat", ".cmd"}:
        return None
    if normalized.startswith("infrastructure/observability/") and suffix == ".sh":
        return LegacySurface(
            normalized,
            "EXTERNAL_NATIVE_HOOK",
            "external-runtime",
            "RETAIN",
        )
    if normalized.startswith(".github/"):
        return LegacySurface(normalized, "CI_RUNNER_GLUE", "ci", "RETAIN")
    if normalized.startswith("deploy/docker/") and Path(normalized).name in {
        "Dockerfile",
        "requirements_generator.py",
    }:
        return None
    if normalized.startswith("scripts/"):
        return LegacySurface(
            normalized,
            "PROJECT_OPERATIONAL_LEGACY",
            "azurpilot.tooling",
            "REMOVE",
        )
    if normalized.startswith("tools/acceptance/"):
        return LegacySurface(
            normalized,
            "PROJECT_ACCEPTANCE_LEGACY",
            "azurpilot.tooling",
            "REMOVE",
        )
    if normalized in {"dev_tools/alas2.bat", "deploy/launcher/Alas.bat"}:
        return LegacySurface(
            normalized,
            "PROJECT_LAUNCHER_LEGACY",
            "azurpilot.cli",
            "REMOVE_OR_DOCUMENT",
        )
    if normalized.startswith("deploy/docker/"):
        return LegacySurface(
            normalized,
            "PROJECT_DOCKER_ORCHESTRATOR_LEGACY",
            "azurpilot.tooling.docker",
            "REMOVE",
        )
    return LegacySurface(normalized, "UNCLASSIFIED_SHELL", "unknown", "REVIEW")


def inventory(root: Path) -> tuple[LegacySurface, ...]:
    return tuple(
        item
        for path in _tracked(root)
        if (root / path).exists()
        if (item := classify(path)) is not None
    )


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    payload = {
        "schema_version": 1,
        "repository_root": "current-checkout",
        "surfaces": [asdict(item) for item in inventory(root)],
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
