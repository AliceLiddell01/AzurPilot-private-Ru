from datetime import date, datetime
from pathlib import Path

from module.shop_event.selector import parse_filter_amount
from module.webui.event_plan import (
    empty_event_plan,
    estimate_stage_runs,
    event_farm_summary,
    event_plan_path,
    import_legacy_event_calculator,
    load_event_plan,
    normalize_event_plan,
    projected_recurring_pt,
    remaining_event_days,
    save_event_plan,
    selected_shop_filter_conflicts,
    selected_shop_filter_tokens,
    selected_shop_items_missing_filter,
    selected_shop_items_partial,
    shop_plan_total,
)


def test_event_plan_round_trip_is_instance_scoped_and_normalized(tmp_path: Path):
    plan = empty_event_plan("en")
    plan["event"].update({"name": "Test Event", "farm_end": "2026-08-20 07:00:00"})
    plan["progress"]["current_pt"] = 1234
    plan["shop_items"] = [
        {"name": "Cube", "price": 100, "stock": 5, "selected": 9, "filter": "Cube"}
    ]

    save_event_plan("alas/unsafe name", plan, root=tmp_path)
    restored = load_event_plan("alas/unsafe name", root=tmp_path)

    assert restored["event"]["name"] == "Test Event"
    assert restored["event"]["server"] == "EN"
    assert restored["progress"] == {"current_pt": 1234, "pt_mode": "auto"}
    assert restored["shop_items"][0]["selected"] == 5
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert list(tmp_path.glob("*.tmp")) == []


def test_corrupt_event_plan_is_preserved_before_empty_fallback(tmp_path: Path):
    path = event_plan_path("alas", root=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"event": ', encoding="utf-8")

    restored = load_event_plan("alas", root=tmp_path)

    assert restored == empty_event_plan()
    assert not path.exists()
    backups = list(tmp_path.glob(f"{path.name}.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == '{"event": '

    restored["event"]["name"] = "Recovered"
    save_event_plan("alas", restored, root=tmp_path)
    assert load_event_plan("alas", root=tmp_path)["event"]["name"] == "Recovered"
    assert backups[0].read_text(encoding="utf-8") == '{"event": '


def test_default_event_plan_storage_lives_under_ignored_runtime_state():
    from module.webui.event_plan import EVENT_PLAN_ROOT

    assert EVENT_PLAN_ROOT.as_posix() == "config/state/event_plans"


def test_schema_v1_plan_migrates_without_losing_old_point_rows():
    raw = {
        "schema_version": 1,
        "event": {"name": "Old", "server": "en"},
        "daily": [{"name": "Daily", "points": 300}],
        "extra": [{"name": "SP", "points": 800}],
    }

    plan = normalize_event_plan(raw)
    assert plan["schema_version"] == 3
    assert plan["progress"] == {"current_pt": 0, "pt_mode": "auto"}
    assert plan["daily"] == [
        {"name": "Daily", "points": 300, "skip": False, "completed_date": ""}
    ]
    assert plan["extra"][0]["name"] == "SP"


def test_legacy_import_is_explicitly_unverified_and_provider_neutral():
    plan = import_legacy_event_calculator(
        {
            "event_name": "Legacy Event",
            "end_date": "2026-08-20",
            "updated_at": "2026-08-13 00:00:00",
            "stages": [{"name": "HT3", "points": 180}],
            "daily": [{"name": "Daily", "points": 300}],
            "extra": [{"name": "SP", "points": 800}],
            "shop_items": [
                {"name": "Cube", "price": 100, "quantity": 5, "filter": "Cube"}
            ],
        },
        server="EN",
    )

    assert plan["event"]["source"]["kind"] == "legacy_bwiki"
    assert plan["event"]["source"]["verified"] is False
    assert plan["event"]["shop_end"] == ""
    assert plan["shop_items"][0]["stock"] == 5
    assert plan["daily"][0]["skip"] is False


def test_shop_plan_total_filter_and_fail_closed_constraints():
    plan = empty_event_plan()
    plan["shop_items"] = [
        {"name": "Cube", "price": 100, "stock": 5, "selected": 5, "filter": "Cube"},
        {"name": "Chip", "price": 200, "stock": 10, "selected": 3, "filter": "Chip"},
        {"name": "Unknown", "price": 50, "stock": 2, "selected": 2, "filter": ""},
    ]

    tokens = selected_shop_filter_tokens(plan)
    assert shop_plan_total(plan) == 1200
    assert tokens == ["Cube", "Chip:3"]
    assert parse_filter_amount(" > ".join(tokens)) == {"chip": 3}
    assert selected_shop_items_partial(plan) == ["Chip"]
    assert selected_shop_items_missing_filter(plan) == ["Unknown"]
    assert selected_shop_filter_conflicts(plan) == {}


def test_shop_filter_conflict_detects_selected_and_excluded_rows_sharing_token():
    plan = empty_event_plan()
    plan["shop_items"] = [
        {"name": "Chip A", "price": 100, "stock": 5, "selected": 5, "filter": "Chip"},
        {"name": "Chip B", "price": 100, "stock": 5, "selected": 0, "filter": "Chip"},
        {"name": "Cube", "price": 100, "stock": 5, "selected": 5, "filter": "Cube"},
    ]

    assert selected_shop_filter_conflicts(plan) == {"Chip": ["Chip A", "Chip B"]}


def test_shop_filter_conflict_detects_partial_selection_on_shared_token():
    plan = empty_event_plan()
    plan["shop_items"] = [
        {"name": "Chip A", "price": 100, "stock": 5, "selected": 5, "filter": "Chip"},
        {"name": "Chip B", "price": 100, "stock": 5, "selected": 3, "filter": "Chip"},
    ]

    assert selected_shop_filter_conflicts(plan) == {"Chip": ["Chip A", "Chip B"]}


def test_shared_filter_token_is_safe_when_every_matching_row_is_selected_fully():
    plan = empty_event_plan()
    plan["shop_items"] = [
        {"name": "Chip A", "price": 100, "stock": 5, "selected": 5, "filter": "Chip"},
        {"name": "Chip B", "price": 100, "stock": 5, "selected": 5, "filter": "Chip"},
    ]

    assert selected_shop_filter_conflicts(plan) == {}
    assert selected_shop_filter_tokens(plan) == ["Chip"]


def test_stage_run_estimate_uses_remaining_pt():
    plan = empty_event_plan()
    plan["stages"] = [
        {"name": "T3", "points": 80},
        {"name": "HT3", "points": 180},
    ]

    result = estimate_stage_runs(plan, 1000)
    assert result == [
        {"name": "T3", "points": 80, "runs": 13},
        {"name": "HT3", "points": 180, "runs": 6},
    ]


def test_recurring_projection_is_inclusive_and_today_completion_does_not_go_stale():
    plan = empty_event_plan()
    plan["event"]["farm_end"] = "2026-08-15 07:00:00"
    plan["daily"] = [
        {"name": "Daily", "points": 300, "completed_date": "2026-08-13"},
        {"name": "Skipped", "points": 999, "skip": True},
    ]
    plan["extra"] = [{"name": "SP", "points": 800}]

    assert remaining_event_days(plan, today=date(2026, 8, 13)) == 3
    assert projected_recurring_pt(plan, today=date(2026, 8, 13)) == 3000
    assert projected_recurring_pt(plan, today=date(2026, 8, 14)) == 2200


def test_remaining_event_days_respects_end_time_on_current_day():
    plan = empty_event_plan()
    plan["event"]["farm_end"] = "2026-08-15 07:00:00"

    assert remaining_event_days(plan, today=datetime(2026, 8, 15, 6, 59, 59)) == 1
    assert remaining_event_days(plan, today=datetime(2026, 8, 15, 7, 0, 1)) == 0


def test_date_only_farm_end_is_inclusive_through_end_of_day():
    plan = empty_event_plan()
    plan["event"]["farm_end"] = "2026-08-15"

    assert remaining_event_days(plan, today=datetime(2026, 8, 15, 23, 59, 58)) == 1
    assert remaining_event_days(plan, today=datetime(2026, 8, 16, 0, 0, 0)) == 0


def test_farm_summary_uses_local_current_pt_and_recurring_sources():
    plan = empty_event_plan()
    plan["event"]["farm_end"] = "2026-08-15"
    plan["progress"]["current_pt"] = 100
    plan["daily"] = [{"name": "Daily", "points": 300, "completed_date": "2026-08-13"}]
    plan["extra"] = [{"name": "SP", "points": 800}]

    summary = event_farm_summary(plan, 5000, today=date(2026, 8, 13))
    assert summary == {
        "target_pt": 5000,
        "current_pt": 100,
        "remaining_days": 3,
        "recurring_pt": 3000,
        "remaining_before_recurring": 4900,
        "farm_required_pt": 1900,
    }
