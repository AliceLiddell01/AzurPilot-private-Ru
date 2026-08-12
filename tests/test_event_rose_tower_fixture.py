from pathlib import Path

from module.webui.event_plan import empty_event_plan
from module.webui.event_rose_tower_fixture import (
    ROSE_TOWER_ACTIVITY_ID,
    ROSE_TOWER_MEDAL_GROUP_ID,
    ROSE_TOWER_MEDAL_TASK_IDS,
    ROSE_TOWER_SHOP_TEMPLATE_ID,
    ROSE_TOWER_SOURCE_KIND,
    ROSE_TOWER_SOURCE_REVISION,
    empty_event_plan_without_fixture,
    rose_tower_fixture_plan,
    with_rose_tower_fixture,
)


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "module" / "webui" / "app.py"


def test_rose_tower_fixture_uses_real_source_identity_without_inventing_rows():
    plan = rose_tower_fixture_plan()

    assert plan["event"]["id"] == "5941"
    assert plan["event"]["name"] == "A Rose on the High Tower"
    assert plan["event"]["server"] == "EN"
    assert plan["event"]["farm_end"] == "2025-06-11 23:59:59"
    assert plan["event"]["shop_end"] == "2025-06-18 23:59:59"
    assert plan["event"]["source"] == {
        "kind": ROSE_TOWER_SOURCE_KIND,
        "verified": False,
        "updated_at": "2026-08-12 22:51:03",
        "revision": ROSE_TOWER_SOURCE_REVISION,
    }
    assert plan["stages"] == []
    assert plan["daily"] == []
    assert plan["extra"] == []
    assert plan["shop_items"] == []

    assert ROSE_TOWER_ACTIVITY_ID == "5941"
    assert ROSE_TOWER_SHOP_TEMPLATE_ID == "71136"
    assert ROSE_TOWER_MEDAL_GROUP_ID == "5970"
    assert ROSE_TOWER_MEDAL_TASK_IDS == tuple(range(21714, 21723))


def test_rose_tower_fixture_is_only_a_pristine_plan_fallback():
    seeded = with_rose_tower_fixture(empty_event_plan("EN"))
    assert seeded["event"]["name"] == "A Rose on the High Tower"

    user_plan = empty_event_plan("EN")
    user_plan["event"]["name"] = "My event"
    preserved = with_rose_tower_fixture(user_plan)
    assert preserved["event"]["name"] == "My event"
    assert preserved["event"]["source"]["kind"] == "manual"


def test_explicit_clear_suppresses_temporary_fixture():
    cleared = empty_event_plan_without_fixture("EN")
    resolved = with_rose_tower_fixture(cleared)

    assert resolved["event"]["name"] == ""
    assert resolved["event"]["source"]["kind"] == "manual_empty"


def test_fixture_mixin_wraps_event_safety_and_layout():
    source = APP.read_text(encoding="utf-8")
    fixture = source.index("    EventFixtureMixin,")
    safety = source.index("    EventShopSafetyMixin,")
    layout = source.index("    EventLayoutMixin,")
    generic = source.index("    TaskConfigMixin,")
    assert fixture < safety < layout < generic
