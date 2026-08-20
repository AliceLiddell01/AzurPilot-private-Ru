"""Проверка и разрешение generated-карт события по registry binding.

Модуль не читает registry и не импортирует campaign-файлы при собственном
импорте. Разрешение события выполняется только по явному вызову.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from module.event_datamine.artifact import BUILTIN_ARTIFACT_ROOT
from module.event_datamine.runtime_policy import (
    EventRuntimePolicyError,
    load_generated_runtime_policy,
    map_runtime_policy,
)


_SAFE_PACKAGE_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
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
_STAGE_COMPATIBILITY_REVERSE = {
    value: key for key, value in _STAGE_COMPATIBILITY.items()
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


def _stage_target(
    modules: Mapping[str, str],
    stage: str,
) -> str | None:
    requested = str(stage or "").strip().lower()
    if not requested:
        return None
    for candidate in (
        requested,
        _STAGE_COMPATIBILITY.get(requested),
        _STAGE_COMPATIBILITY_REVERSE.get(requested),
    ):
        if candidate is None:
            continue
        target = modules.get(candidate)
        if target is not None:
            return target
    return None


def generated_stage_module(
    artifact: Mapping[str, Any],
    stage: str,
) -> str:
    """Сопоставить имя этапа с каноническим generated module без хардкода события."""

    target = _stage_target(_verified_generated_modules(artifact), stage)
    if target is not None:
        return target
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


def generated_stage_target(
    modules: Mapping[str, str],
    stage: str,
) -> str | None:
    """Сопоставить stage с target из уже проверенного generated-каталога."""

    return _stage_target(modules, stage)


def _runtime_server() -> str:
    import module.config.server as server_config

    return str(server_config.server or "").strip().upper()


def resolve_generated_campaign_modules(
    selector: str,
    *,
    server: str | None = None,
    registry_root: Path | str = BUILTIN_ARTIFACT_ROOT,
) -> dict[str, str] | None:
    """Вернуть verified stage-каталог generated-события для selector.

    Связь ``(server, selector) -> event_id`` берётся только из Event registry и
    не зависит от текущей фазы lifecycle. ``None`` означает, что для этого
    сервера selector не закреплён за generated artifact. Повреждённый binding
    считается ошибкой: в таком состоянии переход на legacy-карты небезопасен.
    """

    selector = str(selector or "").strip()
    if not selector.startswith("event_"):
        return None

    normalized_server = str(server or _runtime_server()).strip().upper()
    if not normalized_server:
        raise EventCampaignSelectorError(
            "Не удалось определить сервер для разрешения generated Event selector"
        )

    # Тяжёлые зависимости registry загружаются только при реальном разрешении
    # campaign stage, а не во время обычного `import campaign`.
    from module.event_datamine.registry import load_event_artifact_registry

    try:
        registry = load_event_artifact_registry(registry_root)
        artifact = registry.resolve_campaign_selector(
            normalized_server,
            selector,
        )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise EventCampaignSelectorError(
            f"Не удалось безопасно разрешить Event selector "
            f"{normalized_server}:{selector}"
        ) from exc
    if artifact is None:
        return None

    modules = _verified_generated_modules(artifact)
    return {
        stage: (
            "campaign.generated_event."
            + ".".join(PurePosixPath(module).with_suffix("").parts)
        )
        for stage, module in modules.items()
    }


def resolve_generated_campaign_module(
    selector: str,
    stage: str,
    *,
    server: str | None = None,
    registry_root: Path | str = BUILTIN_ARTIFACT_ROOT,
    strict: bool = True,
) -> str | None:
    """Разрешить selector и stage в канонический generated-модуль события.

    `None` возвращается, когда selector не закреплён за generated-событием для
    текущего сервера. Для распознанного selector неизвестный stage по умолчанию
    является ошибкой, чтобы runtime не проваливался в одноимённую legacy-карту.
    Для фильтров и каталогов можно явно передать `strict=False`.
    """

    stage = str(stage or "").strip().lower()
    if not stage:
        return None
    modules = resolve_generated_campaign_modules(
        selector,
        server=server,
        registry_root=registry_root,
    )
    if modules is None:
        return None

    target = generated_stage_target(modules, stage)
    if target is not None:
        return target
    if strict:
        raise EventCampaignSelectorError(
            f"Текущее generated-событие не содержит проверенный этап {stage!r}"
        )
    return None
