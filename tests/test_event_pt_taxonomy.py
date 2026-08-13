from module.event_datamine.artifact import load_builtin_artifact
from module.event_datamine.compiler import classify_pt_task_config


def test_pt_taxonomy_comes_from_structured_task_config_relation():
    task_config = [
        [1, "localized text may change", [101]],
        [2, "another label", [9001]],
        [3, "ignored prose", [102]],
        [4, "challenge prose", [103]],
        [99, "unknown group", [104]],
    ]

    tasks, maps = classify_pt_task_config(
        task_config, task_ids={101, 102, 103, 104}, map_ids={9001}
    )

    assert tasks == {101: "first_clear", 102: "daily", 103: "challenge", 104: "unknown"}
    assert maps == {9001}


def test_builtin_artifact_exposes_complete_stage4_taxonomy_and_runtime_currency_relation():
    spec = load_builtin_artifact()["event_spec"]
    kinds = {item["kind"] for item in spec["pt_sources"]}
    tokens = {item["id"]: item.get("runtime_token") for item in spec["currencies"]}

    assert {
        "first_clear",
        "daily",
        "daily_first_clear",
        "repeatable_map_clear",
        "challenge",
    } <= kinds
    assert tokens == {498: "pt", 499: "URpt"}
