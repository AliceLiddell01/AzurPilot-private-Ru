"""Каноническая bounded-идентичность ресурсов application observability."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_SERVICE_NAME = "azurpilot"
_DEFAULT_DEPLOYMENT_ENVIRONMENT = "local"
_REPOSITORY_ROOT_ENV = "AZURPILOT_REPOSITORY_ROOT"
_MAX_ENV_BYTES = 64 * 1024
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True, slots=True)
class ObservabilityIdentity:
    """Минимальная идентичность, общая для OTel resource и Dev Evidence."""

    service_name: str
    deployment_environment: str


def _is_repository_root(candidate: Path) -> bool:
    return (candidate / "gui.py").is_file() and (candidate / "module").is_dir()


def _resolve_repository_root(
    environment: Mapping[str, str],
    repository_root: Path | None,
    *,
    allow_default_root: bool,
) -> Path | None:
    if repository_root is not None:
        try:
            candidate = repository_root.resolve()
        except (OSError, RuntimeError):
            return None
        return candidate if _is_repository_root(candidate) else None

    configured = environment.get(_REPOSITORY_ROOT_ENV, "").strip()
    if configured:
        try:
            candidate = Path(configured).resolve()
        except (OSError, RuntimeError):
            candidate = None
        if candidate is not None and _is_repository_root(candidate):
            return candidate
    if not allow_default_root:
        return None
    try:
        candidate = Path(__file__).resolve().parents[2]
    except (OSError, RuntimeError):
        return None
    return candidate if _is_repository_root(candidate) else None


def _local_resource_attributes(repository_root: Path | None) -> str | None:
    if repository_root is None:
        return None
    env_path = repository_root / ".env"
    try:
        if not env_path.is_file():
            return None
        with env_path.open("rb") as stream:
            raw = stream.read(_MAX_ENV_BYTES + 1)
        if len(raw) > _MAX_ENV_BYTES:
            return None
        lines = raw.decode("utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = (part.strip() for part in stripped.split("=", 1))
        if name != "OTEL_RESOURCE_ATTRIBUTES":
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value
    return None


def _deployment_environment(raw_attributes: str | None) -> str:
    if not isinstance(raw_attributes, str):
        return _DEFAULT_DEPLOYMENT_ENVIRONMENT
    for attribute in raw_attributes.split(","):
        key, separator, value = attribute.partition("=")
        if separator and key.strip() == "deployment.environment.name":
            candidate = value.strip()
            if _SAFE_VALUE.fullmatch(candidate):
                return candidate
    return _DEFAULT_DEPLOYMENT_ENVIRONMENT


def resolve_observability_identity(
    *,
    repository_root: Path | None = None,
    environment: Mapping[str, str] | None = None,
    allow_default_root: bool = True,
) -> ObservabilityIdentity:
    """Разрешить resource identity из process env с bounded `.env` fallback.

    Читается только ``OTEL_RESOURCE_ATTRIBUTES`` и только из подтверждённого
    корня репозитория. Остальные переменные, headers, credentials и пути в
    identity не попадают.
    """

    process_environment = environment if environment is not None else os.environ
    root = _resolve_repository_root(
        process_environment,
        repository_root,
        allow_default_root=allow_default_root,
    )
    if "OTEL_RESOURCE_ATTRIBUTES" in process_environment:
        raw_attributes = process_environment.get("OTEL_RESOURCE_ATTRIBUTES")
    else:
        raw_attributes = _local_resource_attributes(root)
    return ObservabilityIdentity(
        service_name=_DEFAULT_SERVICE_NAME,
        deployment_environment=_deployment_environment(raw_attributes),
    )


__all__ = ["ObservabilityIdentity", "resolve_observability_identity"]
