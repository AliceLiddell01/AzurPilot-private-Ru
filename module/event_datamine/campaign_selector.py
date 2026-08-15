"""Validation helpers for generated Event campaign package metadata."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any


_SAFE_PACKAGE_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class EventCampaignSelectorError(ValueError):
    """Generated Event artifact cannot be represented as a safe campaign package."""


def generated_campaign_package_parts(artifact: Mapping[str, Any]) -> tuple[str, ...]:
    """Return one validated generated campaign package from artifact metadata.

    The activity id is never encoded in behavior here.  The package location is
    derived exclusively from ``metadata.generated_maps[*].module`` and must be
    identical for all verified generated maps.
    """

    metadata = artifact.get("metadata")
    if not isinstance(metadata, Mapping):
        raise EventCampaignSelectorError("Event artifact не содержит metadata")
    generated = metadata.get("generated_maps")
    if not isinstance(generated, list):
        raise EventCampaignSelectorError("Event artifact не содержит generated_maps")

    parents: set[tuple[str, ...]] = set()
    for raw in generated:
        if not isinstance(raw, Mapping):
            continue
        if str(raw.get("source_status") or "") != "verified":
            continue
        module = str(raw.get("module") or "").strip()
        if not module:
            continue
        path = PurePosixPath(module)
        if path.is_absolute() or ".." in path.parts or path.suffix != ".py":
            raise EventCampaignSelectorError(
                f"Некорректный generated map module: {module!r}"
            )
        parent = path.parent.parts
        if not parent or any(not _SAFE_PACKAGE_PART.fullmatch(part) for part in parent):
            raise EventCampaignSelectorError(
                f"Некорректный generated campaign package: {module!r}"
            )
        parents.add(tuple(parent))

    if len(parents) != 1:
        raise EventCampaignSelectorError(
            "Generated maps должны принадлежать одному campaign package"
        )
    return next(iter(parents))
