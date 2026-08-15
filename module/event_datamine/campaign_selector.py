"""Generic bridge from generated Event artifacts to Campaign.Event selectors."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any


_GENERATED_SELECTOR_ROOT = "event_generated"
_SAFE_PACKAGE_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class EventCampaignSelectorError(ValueError):
    """Generated Event artifact cannot be represented as a safe campaign package."""


def generated_campaign_selector(artifact: Mapping[str, Any]) -> str:
    """Build the runtime Campaign.Event package from generated-map metadata.

    ``metadata.generated_maps[*].module`` is authoritative for the generated
    package location.  The returned selector uses the stable ``event_generated``
    namespace alias, so runtime event handling still follows the normal
    ``event*`` path without embedding an activity id in production logic.
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
    parent = next(iter(parents))
    return ".".join((_GENERATED_SELECTOR_ROOT, *parent))
