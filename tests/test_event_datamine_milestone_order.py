from module.webui.app_event_datamine import _order_event_milestones


def test_source_backed_event_plan_orders_milestones_by_threshold():
    plan = {
        "milestones": [
            {"threshold": 3000, "name": "third"},
            {"threshold": 1000, "name": "first"},
            {"threshold": 2000, "name": "second"},
        ]
    }

    result = _order_event_milestones(plan)

    assert result is plan
    assert [item["threshold"] for item in result["milestones"]] == [1000, 2000, 3000]
    assert [item["name"] for item in result["milestones"]] == ["first", "second", "third"]
