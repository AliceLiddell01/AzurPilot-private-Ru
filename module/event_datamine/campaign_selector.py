"""Проверка и разрешение generated-карт текущего события.

Модуль не читает registry и не импортирует campaign-файлы при собственном
импорте. Разрешение текущего события выполняется только по явному вызову.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from module.event_datamine.artifact import BUILTIN_ARTIFACT_ROOT
from module.event_datamine.runtime_policy import load_generated_runtime_policy


_SAFE_PACKAGE_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DEFAULT_ARGS_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "argument" / "args.json"
)
_STAGE_COMPATIBILITY = {
    "t1": "a1",
    "t2": "a2",
    "t3": "a3",
    "t4": "b1",
    "t5": "b2",
    "t6": "b3",
    "ht1": "c1",
    "ht2": "c2",
    "ht3": "c3",
    "ht4": "d1",
    "ht5": "d2",
    "ht6": "d3",
}


class EventCampaignSelectorError(ValueError):
    """Generated Event artifact нельзя безопасно представить как campaign package."""


def _verified_generated_modules(artifact: Mapping[str, Any]) -> dict[str, str]:
    metadata = artifact.get("metadata")
    if not isinstance(metadata, Mapping):
        raise EventCampaignSelectorError("Event artifact не содержит metadata")
    generated = metadata.get("generated_maps")
    if not isinstance(generated, list):
        raise EventCampaignSelectorError("Event artifact не содержит generated_maps")

    modules: dict[str, str] = {}
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
        if not path.parent.parts or any(
            not _SAFE_PACKAGE_PART.fullmatch(part) for part in path.parent.parts
        ):
            raise EventCampaignSelectorError(
                f"Некорректный generated campaign package: {module!r}"
            )
        stem = path.stem.lower()
        if stem in modules and modules[stem] != module:
            raise EventCampaignSelectorError(
                f"Generated map module имеет неоднозначное имя: {stem!r}"
            )
        modules[stem] = module
    if not modules:
        raise EventCampaignSelectorError("Event artifact не содержит verified generated maps")
    return modules


def generated_campaign_package_parts(artifact: Mapping[str, Any]) -> tuple[str, ...]:
    """Вернуть единый проверенный package generated-карт из metadata artifact."""

    modules = _verified_generated_modules(artifact)
    parents = {PurePosixPath(module).parent.parts for module in modules.values()}
    if len(parents) != 1:
        raise EventCampaignSelectorError(
            "Generated maps должны принадлежать одному campaign package"
        )
    return next(iter(parents))


def generated_stage_module(artifact: Mapping[str, Any], stage: str) -> str:
    """Сопоставить имя этапа с каноническим generated module без хардкода события."""

    modules = _verified_generated_modules(artifact)
    requested = str(stage or "").strip().lower()
    if requested in modules:
        return modules[requested]

    canonical = _STAGE_COMPATIBILITY.get(requested)
    if canonical in modules:
        return modules[canonical]
    reverse = {value: key for key, value in _STAGE_COMPATIBILITY.items()}
    alternate = reverse.get(requested)
    if alternate in modules:
        return modules[alternate]
    raise EventCampaignSelectorError(
        f"Generated maps не содержат этап {stage!r}"
    )


def generated_campaign_ui_layout(module_name: str) -> str | None:
    """Прочитать проверенную UI-policy рядом с уже разрешённым generated package."""

    parts = str(module_name or "").split(".")
    if len(parts) < 4 or parts[:2] != ["campaign", "generated_event"]:
        raise EventCampaignSelectorError(
            f"Некорректный generated campaign module: {module_name!r}"
        )
    package_parts = tuple(parts[2:-1])
    if not package_parts or any(
        not _SAFE_PACKAGE_PART.fullmatch(part) for part in package_parts
    ):
        raise EventCampaignSelectorError(
            f"Некорректный generated campaign package: {module_name!r}"
        )
    policy = load_generated_runtime_policy(package_parts)
    if policy is None:
        return None
    campaign_ui = policy.get("campaign_ui")
    if not isinstance(campaign_ui, Mapping):
        raise EventCampaignSelectorError("Runtime-policy не содержит campaign_ui")
    layout = str(campaign_ui.get("layout") or "").strip()
    return layout or None


def _configured_servers(
    selector: str,
    *,
    args_data: Mapping[str, Any],
) -> set[str]:
    event_arg = args_data.get("Event", {}).get("Campaign", {}).get("Event", {})
    if not isinstance(event_arg, Mapping):
        return set()

    servers: set[str] = set()
    for key, raw_options in event_arg.items():
        if not str(key).startswith("option_") or key == "option_bold":
            continue
        server = str(key).removeprefix("option_").strip().upper()
        if not server or not isinstance(raw_options, list):
            continue
        if selector in {str(item) for item in raw_options}:
            servers.add(server)
    return servers


def resolve_generated_campaign_module(
    selector: str,
    stage: str,
    *,
    now: datetime,
    args_data: Mapping[str, Any] | None = None,
    registry_root: Path | str = BUILTIN_ARTIFACT_ROOT,
) -> str | None:
    """Разрешить legacy selector в канонический generated-модуль текущего события.

    `None` означает, что selector не относится к текущему Event-каталогу либо
    разрешение неоднозначно. В таком случае стандартный импорт остаётся без
    изменений.
    """

    selector = str(selector or "").strip()
    stage = str(stage or "").strip().lower()
    if not selector.startswith("event_") or not stage:
        return None

    if args_data is None:
        try:
            args_data = json.loads(_DEFAULT_ARGS_PATH.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
    servers = _configured_servers(selector, args_data=args_data)
    if not servers:
        return None

    # Тяжёлые зависимости registry загружаются только при реальном разрешении
    # campaign stage, а не во время обычного `import campaign`.
    from module.event_datamine.discovery import EventDiscoveryError
    from module.event_datamine.registry import EventArtifactRegistry

    registry = EventArtifactRegistry(registry_root)
    targets: set[str] = set()
    for server in servers:
        try:
            artifact = registry.resolve_current(server, now, supplemental=False)
        except (EventDiscoveryError, OSError, TypeError, ValueError):
            continue
        if artifact is None:
            continue
        try:
            module = generated_stage_module(artifact, stage)
        except EventCampaignSelectorError:
            continue
        path = PurePosixPath(module)
        targets.add("campaign.generated_event." + ".".join(path.with_suffix("").parts))

    if len(targets) != 1:
        return None
    return next(iter(targets))
