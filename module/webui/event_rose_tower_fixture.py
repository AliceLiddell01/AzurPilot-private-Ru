"""Historical AzurLaneLuaScripts fixture for Event WebUI development.

This is intentionally a temporary fallback, not a live event provider. It lets the
new Event pages exercise the provider-neutral manifest path with a real historical
event while live source parsing is still being implemented.

Source snapshot:
    AzurLaneTools/AzurLaneLuaScripts
    9f84e99c4987dd85f88cb131db84244cd1c9be15

The readable Lua sources expose the event identity and related configuration IDs.
The generated activity/shop tables reference streamed values for their time
payloads, so the EN deadlines below are copied from the official Yostar event
notices and remain unverified for runtime application.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping

from module.webui.event_manifest import event_plan_from_manifest
from module.webui.event_plan import empty_event_plan, normalize_event_plan


ROSE_TOWER_SOURCE_KIND = "azurlane_lua_fixture"
ROSE_TOWER_SOURCE_REPOSITORY = "AzurLaneTools/AzurLaneLuaScripts"
ROSE_TOWER_SOURCE_REVISION = "9f84e99c4987dd85f88cb131db84244cd1c9be15"
ROSE_TOWER_SOURCE_UPDATED_AT = "2026-08-12 22:51:03"
ROSE_TOWER_SOURCE_TIMEZONE = "UTC-7"

ROSE_TOWER_ACTIVITY_ID = "5941"
ROSE_TOWER_SHOP_TEMPLATE_ID = "71136"
ROSE_TOWER_MEMORY_GROUP_ID = "329"
ROSE_TOWER_MEDAL_GROUP_ID = "5970"
ROSE_TOWER_MEDAL_TASK_IDS = tuple(range(21714, 21723))

# The source-backed IDs above are deliberately kept outside EventPlan until the
# live provider has a stable metadata contract. Do not invent stage PT or shop
# rows here: current plain Lua exposes the relevant table identities, while the
# actual shop/time payload is loaded through the game's streamed ShareCfg data.
ROSE_TOWER_MANIFEST: Dict[str, Any] = {
    "event": {
        "id": ROSE_TOWER_ACTIVITY_ID,
        "name": "A Rose on the High Tower",
        "server": "EN",
        "farm_end": "2025-06-11 23:59:59",
        "shop_end": "2025-06-18 23:59:59",
    },
    "stages": [],
    "daily": [],
    "extra": [],
    "shop_items": [],
}


def rose_tower_fixture_plan() -> Dict[str, Any]:
    """Build the deterministic historical EventPlan fixture."""
    return event_plan_from_manifest(
        ROSE_TOWER_MANIFEST,
        source_kind=ROSE_TOWER_SOURCE_KIND,
        verified=False,
        revision=ROSE_TOWER_SOURCE_REVISION,
        updated_at=ROSE_TOWER_SOURCE_UPDATED_AT,
    )


def _is_pristine_local_plan(plan: Mapping[str, Any]) -> bool:
    normalized = normalize_event_plan(plan)
    event = normalized["event"]
    source = event["source"]
    progress = normalized["progress"]

    return (
        source["kind"] == "manual"
        and not source["verified"]
        and not source["revision"]
        and not event["id"]
        and not event["name"]
        and not event["farm_end"]
        and not event["shop_end"]
        and progress["current_pt"] == 0
        and progress["pt_mode"] == "auto"
        and not normalized["stages"]
        and not normalized["daily"]
        and not normalized["extra"]
        and not normalized["shop_items"]
    )


def with_rose_tower_fixture(plan: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the fixture only for a truly untouched local plan."""
    if _is_pristine_local_plan(plan):
        return rose_tower_fixture_plan()
    return normalize_event_plan(plan)


def empty_event_plan_without_fixture(server: str = "EN") -> Dict[str, Any]:
    """Create an explicit empty plan that suppresses the temporary fallback."""
    plan = empty_event_plan(server)
    plan["event"]["source"]["kind"] = "manual_empty"
    return plan
