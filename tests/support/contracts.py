"""Общие helpers для repository contract tests."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

LEGACY_OPERATOR_DIRECTORIES = ("scripts", "tools/acceptance/powershell")
LEGACY_OPERATOR_FILES = (
    "deploy/docker/deploy-image.sh",
    "deploy/docker/Docker-run.sh",
    "dev_tools/alas2.bat",
    "deploy/launcher/Alas.bat",
)
DOCKER_FORBIDDEN_SOURCE_TOKENS = (
    "apt-get",
    "download.docker.com",
    "ifconfig.me",
)


def assert_no_legacy_operator_surfaces(root: Path) -> None:
    """Проверить отсутствие удалённых host/operator поверхностей без case trap."""

    for relative_directory in LEGACY_OPERATOR_DIRECTORIES:
        directory = root / relative_directory
        if not directory.is_dir():
            continue
        assert not any(
            path.is_file() and path.suffix.casefold() in {".ps1", ".psm1"}
            for path in directory.rglob("*")
        ), f"legacy PowerShell surface remains under {relative_directory}"
    for relative_file in LEGACY_OPERATOR_FILES:
        assert not (root / relative_file).exists(), f"legacy surface remains: {relative_file}"


def assert_source_excludes(source: str, tokens: Iterable[str]) -> None:
    """Проверить отрицательные source contracts с единым списком токенов."""

    for token in tokens:
        assert token not in source, f"forbidden source token remains: {token}"


__all__ = [
    "DOCKER_FORBIDDEN_SOURCE_TOKENS",
    "LEGACY_OPERATOR_DIRECTORIES",
    "LEGACY_OPERATOR_FILES",
    "assert_no_legacy_operator_surfaces",
    "assert_source_excludes",
]
