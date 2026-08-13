"""Детерминированное и атомарное хранение валидированных EventSpec artifacts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from deploy.atomic import file_remove, file_write, replace_tmp, to_tmp_file

EVENT_ARTIFACT_SCHEMA_VERSION = 2
BUILTIN_ARTIFACT_ROOT = Path(__file__).with_name("data")


def _normalize_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = str(key)
            if normalized_key in result:
                raise ValueError(
                    f"Дублирующийся JSON key после нормализации: {normalized_key}"
                )
            result[normalized_key] = _normalize_json(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Неподдерживаемый тип Event artifact: {type(value).__name__}")


def canonical_json(data: Mapping[str, Any]) -> str:
    return json.dumps(
        _normalize_json(data),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def artifact_digest(data: Mapping[str, Any]) -> str:
    clean = dict(data)
    clean.pop("digest", None)
    return sha256(canonical_json(clean).encode("utf-8")).hexdigest()


def validate_artifact(data: Any) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise TypeError("Event artifact должен быть JSON object")
    result = _normalize_json(data)
    if (
        int(result.get("artifact_schema_version", 0) or 0)
        != EVENT_ARTIFACT_SCHEMA_VERSION
    ):
        raise ValueError("Неподдерживаемая версия Event artifact")
    spec = result.get("event_spec")
    if not isinstance(spec, Mapping) or not spec.get("id"):
        raise ValueError("Event artifact не содержит EventSpec identity")
    expected = str(result.get("digest") or "")
    actual = artifact_digest(result)
    if expected != actual:
        raise ValueError("Digest Event artifact не совпадает")
    return result


def build_artifact(
    spec: Mapping[str, Any],
    compiler_version: str = "1",
    *,
    role: str = "production",
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if role not in {"production", "demo"}:
        raise ValueError(f"Неподдерживаемая роль Event artifact: {role}")
    result = {
        "artifact_schema_version": EVENT_ARTIFACT_SCHEMA_VERSION,
        "compiler_version": str(compiler_version),
        "role": role,
        "event_spec": _normalize_json(spec),
    }
    if metadata:
        result["metadata"] = _normalize_json(metadata)
    result["digest"] = artifact_digest(result)
    return result


def write_artifact(path: Path | str, artifact: Mapping[str, Any]) -> Path:
    target = Path(path)
    checked = validate_artifact(artifact)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = to_tmp_file(str(target))
    try:
        file_write(
            temp,
            json.dumps(checked, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        replace_tmp(temp, str(target))
    except BaseException:
        try:
            file_remove(temp)
        except OSError:
            pass
        raise
    return target


def load_artifact(path: Path | str) -> dict[str, Any]:
    return validate_artifact(json.loads(Path(path).read_text(encoding="utf-8")))


def load_builtin_artifact(name: str) -> dict[str, Any]:
    if "/" in name or "\\" in name:
        raise ValueError("Имя builtin artifact не может содержать путь")
    return load_artifact(BUILTIN_ARTIFACT_ROOT / name)
