"""Проверяемая runtime-policy для generated Event-карт.

Файл policy лежит рядом с generated package и содержит только наблюдаемые
runtime-факты, которых нет в ShareCfg. Production-код знает только схему,
а не identity конкретного события.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from module.event_datamine.artifact import canonical_json

RUNTIME_POLICY_SCHEMA_VERSION = 1
GENERATED_EVENT_ROOT = Path(__file__).resolve().parents[2] / "campaign" / "generated_event"
_ALLOWED_UI_LAYOUTS = frozenset({"legacy", "20241219", "20260326"})
_SAFE_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class EventRuntimePolicyError(ValueError):
    """Runtime-policy generated Event-карт не прошла проверку."""


def runtime_policy_digest(data: Mapping[str, Any]) -> str:
    clean = dict(data)
    clean.pop("digest", None)
    return sha256(canonical_json(clean).encode("utf-8")).hexdigest()


def _package_path(parts: tuple[str, ...], root: Path | str) -> Path:
    if not parts or any(not _SAFE_PART.fullmatch(part) for part in parts):
        raise EventRuntimePolicyError("Некорректный generated campaign package")
    base = Path(root).resolve()
    target = base.joinpath(*parts, "runtime.json").resolve()
    if base not in target.parents:
        raise EventRuntimePolicyError("Runtime-policy вышла за пределы generated_event")
    return target


def validate_runtime_policy(data: Any, *, package: str) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise EventRuntimePolicyError("Runtime-policy должна быть JSON object")
    result = dict(data)
    version = result.get("runtime_policy_schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise EventRuntimePolicyError("runtime_policy_schema_version должен быть int")
    if version != RUNTIME_POLICY_SCHEMA_VERSION:
        raise EventRuntimePolicyError("Неподдерживаемая версия runtime-policy")
    if str(result.get("generated_package") or "") != package:
        raise EventRuntimePolicyError("Runtime-policy не соответствует generated package")
    event_id = str(result.get("event_id") or "").strip()
    if not event_id or ":" not in event_id:
        raise EventRuntimePolicyError("Runtime-policy не содержит Event identity")
    campaign_ui = result.get("campaign_ui")
    if not isinstance(campaign_ui, Mapping):
        raise EventRuntimePolicyError("campaign_ui должна быть JSON object")
    layout = str(campaign_ui.get("layout") or "")
    if layout not in _ALLOWED_UI_LAYOUTS:
        raise EventRuntimePolicyError(f"Неподдерживаемый campaign UI layout: {layout!r}")
    evidence = result.get("evidence")
    if not isinstance(evidence, Mapping):
        raise EventRuntimePolicyError("Runtime-policy требует evidence")
    archive_sha256 = str(evidence.get("archive_sha256") or "").lower()
    if not _SHA256.fullmatch(archive_sha256):
        raise EventRuntimePolicyError("Runtime-policy содержит некорректный evidence SHA-256")
    expected = str(result.get("digest") or "")
    if expected != runtime_policy_digest(result):
        raise EventRuntimePolicyError("Digest runtime-policy не совпадает")
    return result


def load_generated_runtime_policy(
    package_parts: tuple[str, ...],
    *,
    root: Path | str = GENERATED_EVENT_ROOT,
) -> dict[str, Any] | None:
    """Загрузить policy для generated package без сетевых запросов."""

    target = _package_path(package_parts, root)
    if not target.is_file():
        return None
    package = ".".join(package_parts)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EventRuntimePolicyError(f"Не удалось прочитать runtime-policy {target}") from exc
    return validate_runtime_policy(data, package=package)
