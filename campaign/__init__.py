"""Runtime compatibility aliases from the legacy Event selector to generated maps.

The static configuration catalog may lag behind the source-backed current Event
artifact.  When that happens, keep the accepted legacy selector value intact but
route its campaign package to the verified generated package for the same server.
Aliases are derived from args.json + Event registry metadata and fail closed on
ambiguity; historical non-current event packages remain untouched.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from typing import Any

from module.event_datamine.artifact import BUILTIN_ARTIFACT_ROOT
from module.event_datamine.campaign_selector import (
    EventCampaignSelectorError,
    generated_campaign_package_parts,
)
from module.event_datamine.discovery import EventDiscoveryError
from module.event_datamine.registry import EventArtifactRegistry


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ARGS_PATH = _PROJECT_ROOT / "module" / "config" / "argument" / "args.json"
_GENERATED_ROOT = Path(__file__).resolve().parent / "generated_event"


def current_event_alias_targets(
    *,
    now: datetime | None = None,
    args_data: dict[str, Any] | None = None,
    registry_root: Path | str = BUILTIN_ARTIFACT_ROOT,
) -> dict[str, Path]:
    """Return unambiguous legacy-selector -> current generated package aliases."""

    if args_data is None:
        args_data = json.loads(_ARGS_PATH.read_text(encoding="utf-8"))
    event_arg = args_data.get("Event", {}).get("Campaign", {}).get("Event", {})
    if not isinstance(event_arg, dict):
        return {}

    registry = EventArtifactRegistry(registry_root)
    candidates: dict[str, set[Path]] = {}
    current_time = now or datetime.now()
    for key, raw_options in event_arg.items():
        if not key.startswith("option_") or key == "option_bold":
            continue
        server = key.removeprefix("option_").strip().upper()
        if not server or not isinstance(raw_options, list):
            continue
        try:
            artifact = registry.resolve_current(
                server, current_time, supplemental=False
            )
        except (EventDiscoveryError, OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if artifact is None:
            continue
        try:
            parts = generated_campaign_package_parts(artifact)
        except EventCampaignSelectorError:
            continue
        target = _GENERATED_ROOT.joinpath(*parts).resolve()
        if _GENERATED_ROOT.resolve() not in target.parents or not target.is_dir():
            continue
        for raw_selector in raw_options:
            selector = str(raw_selector or "").strip()
            if not selector.startswith("event_"):
                continue
            candidates.setdefault(selector, set()).add(target)

    return {
        selector: next(iter(targets))
        for selector, targets in candidates.items()
        if len(targets) == 1
    }


def _stage_compatibility_aliases(target: Path) -> dict[str, str]:
    """Bridge the two standard AL event chapter naming schemes.

    Legacy current-event selectors can make ``CampaignRun.handle_stage_name``
    translate A/B/C/D into T/HT before the dynamic import.  Generated maps keep
    their source chapter names.  Expose both conventional spellings inside the
    runtime alias so stale selector-specific naming policy cannot select an old
    physical map module.
    """

    available = {path.stem for path in target.glob("*.py") if path.is_file()}
    normal = ("a1", "a2", "a3", "b1", "b2", "b3")
    hard = ("c1", "c2", "c3", "d1", "d2", "d3")
    aliases: dict[str, str] = {}
    for index, canonical in enumerate(normal, start=1):
        transformed = f"t{index}"
        if canonical in available and transformed not in available:
            aliases[transformed] = canonical
        elif transformed in available and canonical not in available:
            aliases[canonical] = transformed
    for index, canonical in enumerate(hard, start=1):
        transformed = f"ht{index}"
        if canonical in available and transformed not in available:
            aliases[transformed] = canonical
        elif transformed in available and canonical not in available:
            aliases[canonical] = transformed
    return aliases


def _install_stage_alias(package_name: str, alias: str, source: Path) -> None:
    fullname = f"{package_name}.{alias}"
    if fullname in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(fullname, source)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules[fullname] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(fullname, None)
        raise


def install_current_event_aliases(targets: dict[str, Path] | None = None) -> None:
    """Install synthetic package aliases without replacing historical files on disk."""

    try:
        resolved = targets if targets is not None else current_event_alias_targets()
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return
    for selector, target in resolved.items():
        fullname = f"{__name__}.{selector}"
        if fullname not in sys.modules:
            package = ModuleType(fullname)
            package.__package__ = fullname
            package.__file__ = str(target / "__init__.py")
            package.__path__ = [str(target)]
            spec = ModuleSpec(fullname, loader=None, is_package=True)
            spec.submodule_search_locations = [str(target)]
            package.__spec__ = spec
            sys.modules[fullname] = package

        for alias, canonical in _stage_compatibility_aliases(target).items():
            source = target / f"{canonical}.py"
            try:
                _install_stage_alias(fullname, alias, source)
            except (ImportError, OSError, RuntimeError, TypeError, ValueError):
                # The canonical module remains importable through package.__path__.
                # A compatibility alias is optional and must fail closed.
                sys.modules.pop(f"{fullname}.{alias}", None)


install_current_event_aliases()
