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
from module.event_datamine.runtime_policy import (
    EventRuntimePolicyError,
    load_generated_runtime_policy,
    map_runtime_policy,
)


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


def _event_maps(artifact: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    event_spec = artifact.get("event_spec")
    if not isinstance(event_spec, Mapping):
        raise EventCampaignSelectorError(
            "Event artifact не содержит event_spec"
        )
    raw_maps = event_spec.get("maps")
    if not isinstance(raw_maps, list):
        raise EventCampaignSelectorError(
            "Event artifact не содержит maps"
        )

    result: dict[int, Mapping[str, Any]] = {}
    for raw in raw_maps:
        if not isinstance(raw, Mapping):
            raise EventCampaignSelectorError(
                "Event artifact содержит некорректную карту"
            )
        map_id = raw.get("id")
        if (
            isinstance(map_id, bool)
            or not isinstance(map_id, int)
            or map_id <= 0
        ):
            raise EventCampaignSelectorError(
                f"Event artifact содержит некорректный map ID: {map_id!r}"
            )
        if map_id in result:
            raise EventCampaignSelectorError(
                f"Event artifact дублирует карту {map_id}"
            )
        result[map_id] = raw
    return result


def _map_has_siren(raw_map: Mapping[str, Any]) -> bool:
    for key in ("spawn_data", "spawn_data_loop"):
        rows = raw_map.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if (
                isinstance(row, Mapping)
                and int(row.get("siren", 0) or 0) > 0
            ):
                return True
    return False


def _verified_generated_modules(
    artifact: Mapping[str, Any],
) -> dict[str, str]:
    metadata = artifact.get("metadata")
    if not isinstance(metadata, Mapping):
        raise EventCampaignSelectorError(
            "Event artifact не содержит metadata"
        )
    generated = metadata.get("generated_maps")
    if not isinstance(generated, list):
        raise EventCampaignSelectorError(
            "Event artifact не содержит generated_maps"
        )

    event_spec = artifact.get("event_spec")
    if not isinstance(event_spec, Mapping):
        raise EventCampaignSelectorError(
            "Event artifact не содержит event_spec"
        )
    event_id = str(event_spec.get("id") or "").strip()
    maps = _event_maps(artifact)
    policies: dict[
        tuple[str, ...],
        Mapping[str, Any] | None,
    ] = {}
    modules: dict[str, str] = {}

    for raw in generated:
        if not isinstance(raw, Mapping):
            continue
        if str(raw.get("source_status") or "") != "verified":
            continue
        map_id = raw.get("map_id")
        if isinstance(map_id, bool) or not isinstance(map_id, int):
            raise EventCampaignSelectorError(
                f"Generated map содержит некорректный map ID: {map_id!r}"
            )
        map_spec = maps.get(map_id)
        if map_spec is None:
            raise EventCampaignSelectorError(
                f"Generated map {map_id} отсутствует в EventSpec"
            )
        chapter_name = str(
            map_spec.get("chapter_name") or ""
        ).strip()
        if chapter_name != str(
            raw.get("chapter_name") or ""
        ).strip():
            raise EventCampaignSelectorError(
                f"Generated map {map_id} не совпадает "
                "с chapter_name EventSpec"
            )

        module = str(raw.get("module") or "").strip()
        if not module:
            continue
        path = PurePosixPath(module)
        if (
            path.is_absolute()
            or ".." in path.parts
            or path.suffix != ".py"
        ):
            raise EventCampaignSelectorError(
                f"Некорректный generated map module: {module!r}"
            )
        package_parts = path.parent.parts
        if not package_parts or any(
            not _SAFE_PACKAGE_PART.fullmatch(part)
            for part in package_parts
        ):
            raise EventCampaignSelectorError(
                f"Некорректный generated campaign package: {module!r}"
            )
        if not _SAFE_PACKAGE_PART.fullmatch(path.stem):
            raise EventCampaignSelectorError(
                f"Некорректное имя generated map module: {module!r}"
            )

        if package_parts not in policies:
            try:
                policies[package_parts] = (
                    load_generated_runtime_policy(package_parts)
                )
            except EventRuntimePolicyError as exc:
                raise EventCampaignSelectorError(
                    f"Runtime-policy generated package "
                    f"{'.'.join(package_parts)!r} повреждена"
                ) from exc
        policy = policies[package_parts]
        if policy is None:
            continue
        if str(policy.get("event_id") or "") != event_id:
            raise EventCampaignSelectorError(
                "Runtime-policy generated package "
                "не соответствует Event identity"
            )
        try:
            map_policy = map_runtime_policy(
                policy,
                map_id=map_id,
                chapter_name=chapter_name,
            )
        except EventRuntimePolicyError as exc:
            raise EventCampaignSelectorError(
                f"Runtime-policy карты {map_id} "
                "не соответствует EventSpec"
            ) from exc
        if map_policy is None or map_policy.boss_clear is None:
            continue
        if (
            _map_has_siren(map_spec)
            and map_policy.siren_recognition is None
        ):
            continue
        if (
            map_policy.camera_calibration is None
            or map_policy.detector_calibration is None
            or map_policy.battle_plan is None
        ):
            continue

        stem = path.stem.lower()
        if stem in modules and modules[stem] != module:
            raise EventCampaignSelectorError(
                f"Generated map module имеет неоднозначное имя: "
                f"{stem!r}"
            )
        modules[stem] = module
    if not modules:
        raise EventCampaignSelectorError(
            "Event artifact не содержит проверенных "
            "generated maps для runtime"
        )
    return modules


def generated_campaign_package_parts(
    artifact: Mapping[str, Any],
) -> tuple[str, ...]:
    """Вернуть единый проверенный package generated-карт из metadata artifact."""

    modules = _verified_generated_modules(artifact)
    parents = {
        PurePosixPath(module).parent.parts
        for module in modules.values()
    }
    if len(parents) != 1:
        raise EventCampaignSelectorError(
            "Generated maps должны принадлежать одному campaign package"
        )
    return next(iter(parents))


def generated_stage_module(
    artifact: Mapping[str, Any],
    stage: str,
) -> str:
    """Сопоставить имя этапа с каноническим generated module без хардкода события."""

    modules = _verified_generated_modules(artifact)
    requested = str(stage or "").strip().lower()
    if requested in modules:
        return modules[requested]

    canonical = _STAGE_COMPATIBILITY.get(requested)
    if canonical in modules:
        return modules[canonical]
    reverse = {
        value: key
        for key, value in _STAGE_COMPATIBILITY.items()
    }
    alternate = reverse.get(requested)
    if alternate in modules:
        return modules[alternate]
    raise EventCampaignSelectorError(
        f"Generated maps не содержат этап {stage!r}"
    )


def generated_campaign_ui_layout(
    module_name: str,
) -> str | None:
    """Прочитать проверенную UI-policy рядом с уже разрешённым generated package."""

    parts = str(module_name or "").split(".")
    if (
        len(parts) < 4
        or parts[:2] != ["campaign", "generated_event"]
    ):
        raise EventCampaignSelectorError(
            f"Некорректный generated campaign module: "
            f"{module_name!r}"
        )
    package_parts = tuple(parts[2:-1])
    if not package_parts or any(
        not _SAFE_PACKAGE_PART.fullmatch(part)
        for part in package_parts
    ):
        raise EventCampaignSelectorError(
            f"Некорректный generated campaign package: "
            f"{module_name!r}"
        )
    policy = load_generated_runtime_policy(package_parts)
    if policy is None:
        return None
    campaign_ui = policy.get("campaign_ui")
    if not isinstance(campaign_ui, Mapping):
        raise EventCampaignSelectorError(
            "Runtime-policy не содержит campaign_ui"
        )
    layout = str(campaign_ui.get("layout") or "").strip()
    return layout or None


def _configured_servers(
    selector: str,
    *,
    args_data: Mapping[str, Any],
) -> set[str]:
    node: Any = args_data
    for key in ("Event", "Campaign", "Event"):
        if not isinstance(node, Mapping):
            return set()
        node = node.get(key, {})
    if not isinstance(node, Mapping):
        return set()
    event_arg = node

    servers: set[str] = set()
    for key, raw_options in event_arg.items():
        if (
            not str(key).startswith("option_")
            or key == "option_bold"
        ):
            continue
        server = (
            str(key)
            .removeprefix("option_")
            .strip()
            .upper()
        )
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
            args_data = json.loads(
                _DEFAULT_ARGS_PATH.read_text(encoding="utf-8")
            )
        except (
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return None
    servers = _configured_servers(
        selector,
        args_data=args_data,
    )
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
            artifact = registry.resolve_current(
                server,
                now,
                supplemental=False,
            )
        except (
            EventDiscoveryError,
            OSError,
            TypeError,
            ValueError,
        ):
            continue
        if artifact is None:
            continue
        try:
            module = generated_stage_module(
                artifact,
                stage,
            )
        except EventCampaignSelectorError:
            continue
        path = PurePosixPath(module)
        targets.add(
            "campaign.generated_event."
            + ".".join(path.with_suffix("").parts)
        )

    if len(targets) != 1:
        return None
    return next(iter(targets))
