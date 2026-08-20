from types import ModuleType, SimpleNamespace

import pytest

import campaign as campaign_package
import module.event_datamine.stage_navigation as stage_navigation_module
from module.event_datamine.stage_navigation import (
    EventStageNavigationError,
    StageNavigationPolicy,
    generated_stage_navigation_for_module,
    load_generated_stage_navigation,
    stage_navigation_digest,
    validate_stage_navigation_policy,
)


def _synthetic_runtime(*, digest="1" * 64):
    return {
        "event_id": "test:1",
        "digest": digest,
        "runtime_maps": [
            {
                "map_id": 1,
                "chapter_name": "序章",
                "source_path": "campaign/event_test/alpha.py",
                "boss_clear": {"strategy": "campaign"},
            },
            {
                "map_id": 2,
                "chapter_name": "決戦",
                "source_path": "campaign/event_test/omega.py",
                "boss_clear": {"strategy": "campaign"},
            },
        ],
    }


def _synthetic_navigation(*, auto_next="omega"):
    data = {
        "stage_navigation_schema_version": 1,
        "generated_package": "synthetic",
        "event_id": "test:1",
        "runtime_policy_digest": "1" * 64,
        "stages": [
            {
                "module": "alpha",
                "map_id": 1,
                "chapter_name": "序章",
                "auto_next": auto_next,
                "difficulty": "normal",
                "ui_page": "event",
                "ui_mode": "combat",
                "ui_aside": "part1",
                "ui_chapter_index": 1,
                "entrance_names": ["序章"],
            },
            {
                "module": "omega",
                "map_id": 2,
                "chapter_name": "決戦",
                "difficulty": "hard",
                "ui_page": "event",
                "ui_mode": "combat",
                "ui_aside": "part2",
                "ui_chapter_index": 2,
                "entrance_names": ["決戦"],
            },
        ],
    }
    data["digest"] = stage_navigation_digest(data)
    return data


def test_navigation_schema_accepts_arbitrary_stage_names(monkeypatch):
    monkeypatch.setattr(
        stage_navigation_module,
        "load_generated_runtime_policy",
        lambda _package_parts: _synthetic_runtime(),
    )

    stages = validate_stage_navigation_policy(
        _synthetic_navigation(),
        package_parts=("synthetic",),
    )

    assert stages["alpha"].auto_next == "omega"
    assert stages["alpha"].chapter_name == "序章"
    assert stages["omega"].chapter_name == "決戦"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda data: data["stages"][0].__setitem__("auto_next", "missing"),
            "неизвестный auto_next",
        ),
        (
            lambda data: data["stages"][1].__setitem__("auto_next", "alpha"),
            "цикл автопродвижения",
        ),
    ],
)
def test_navigation_schema_rejects_invalid_graph(monkeypatch, mutate, message):
    monkeypatch.setattr(
        stage_navigation_module,
        "load_generated_runtime_policy",
        lambda _package_parts: _synthetic_runtime(),
    )
    data = _synthetic_navigation()
    mutate(data)
    data["digest"] = stage_navigation_digest(data)

    with pytest.raises(EventStageNavigationError, match=message):
        validate_stage_navigation_policy(
            data,
            package_parts=("synthetic",),
        )


def test_navigation_schema_rejects_runtime_policy_drift(monkeypatch):
    monkeypatch.setattr(
        stage_navigation_module,
        "load_generated_runtime_policy",
        lambda _package_parts: _synthetic_runtime(digest="2" * 64),
    )

    with pytest.raises(
        EventStageNavigationError,
        match="другой версии runtime-policy",
    ):
        validate_stage_navigation_policy(
            _synthetic_navigation(),
            package_parts=("synthetic",),
        )


def test_current_event_navigation_is_complete_and_crosses_partitions():
    stages = load_generated_stage_navigation(("en_51101",))

    assert stages is not None
    assert len(stages) == 13
    assert stages["a3"].auto_next == "b1"
    assert stages["b3"].auto_next == "c1"
    assert stages["c3"].auto_next == "d1"
    assert stages["d3"].auto_next is None
    assert stages["sp"].auto_next is None
    assert stages["b3"].difficulty == "normal"
    assert stages["b3"].ui_aside == "part2"
    assert stages["c1"].difficulty == "hard"
    assert stages["c1"].ui_aside == "part1"
    assert stages["sp"].ui_aside == "sp"


def test_navigation_resolves_by_generated_module_not_stage_letters():
    navigation = generated_stage_navigation_for_module(
        "campaign.generated_event.en_51101.b3"
    )

    assert navigation is not None
    assert navigation.map_id == 2050006
    assert navigation.chapter_name == "B3"
    assert navigation.auto_next == "c1"


def _runner_for_navigation(navigation, *, custom=""):
    class Runner:
        pass

    Runner._generated_event_stage_navigation = navigation
    runner = Runner()
    runner.config = SimpleNamespace(STAGE_INCREASE_CUSTOM=custom)
    runner.checked_stages = []

    def stage_exists(stage):
        runner.checked_stages.append(stage)
        return str(stage).casefold() in {"omega", "special", "c1"}

    runner._campaign_stage_exists = stage_exists
    return runner


def test_generated_auto_advance_uses_explicit_edge_without_legacy_sequences():
    navigation = StageNavigationPolicy(
        module="alpha",
        map_id=1,
        chapter_name="序章",
        auto_next="omega",
    )
    runner = _runner_for_navigation(navigation)

    result = campaign_package._generated_campaign_name_increase(runner, "ALPHA")

    assert result == "OMEGA"
    assert runner.checked_stages == ["omega"]


def test_generated_auto_advance_stops_when_policy_has_no_edge():
    navigation = StageNavigationPolicy(
        module="omega",
        map_id=2,
        chapter_name="決戦",
    )
    runner = _runner_for_navigation(navigation)

    result = campaign_package._generated_campaign_name_increase(runner, "OMEGA")

    assert result == "OMEGA"
    assert runner.checked_stages == []


def test_generated_auto_advance_keeps_explicit_custom_sequence_priority():
    navigation = StageNavigationPolicy(
        module="alpha",
        map_id=1,
        chapter_name="序章",
        auto_next="omega",
    )
    runner = _runner_for_navigation(
        navigation,
        custom="ALPHA > SPECIAL",
    )

    result = campaign_package._generated_campaign_name_increase(runner, "ALPHA")

    assert result == "SPECIAL"
    assert runner.checked_stages == ["SPECIAL"]


@pytest.mark.parametrize(
    ("module", "difficulty", "aside", "chapter_index"),
    [
        ("b3", "normal", "part2", 2),
        ("c1", "hard", "part1", 1),
        ("sp", "normal", "sp", 1),
    ],
)
def test_generated_ui_route_uses_navigation_policy(
    module,
    difficulty,
    aside,
    chapter_index,
):
    navigation = generated_stage_navigation_for_module(
        f"campaign.generated_event.en_51101.{module}"
    )
    assert navigation is not None
    calls = []

    class Config:
        MAP_CHAPTER_SWITCH_20260326 = False
        MAP_CHAPTER_SWITCH_20241219 = True

        def override(self, **kwargs):
            calls.append(("override", kwargs))

    class Runner:
        _generated_event_stage_navigation = navigation

        def __init__(self):
            self.config = Config()

        def ui_goto_event(self):
            calls.append(("page", "event"))

        def ui_goto_sp(self):
            calls.append(("page", "sp"))

        def ui_goto_campaign(self):
            calls.append(("page", "campaign"))

        def campaign_ensure_mode_20241219(self, mode):
            calls.append(("mode", mode))

        def campaign_ensure_aside_20241219(self, value):
            calls.append(("aside", value))

        def campaign_ensure_aside_20260326(self, value):
            calls.append(("aside-20260326", value))

        def campaign_ensure_mode(self, mode):
            calls.append(("legacy-mode", mode))

        def campaign_ensure_chapter(self, index):
            calls.append(("chapter", index))

    campaign_package._generated_campaign_set_chapter(Runner(), module)

    assert calls == [
        ("override", {"Campaign_Mode": difficulty}),
        ("page", "event"),
        ("mode", "combat"),
        ("aside", aside),
        ("chapter", chapter_index),
    ]


def test_generated_entrance_uses_policy_aliases_case_insensitively():
    navigation = generated_stage_navigation_for_module(
        "campaign.generated_event.en_51101.c1"
    )
    assert navigation is not None
    button = SimpleNamespace(name="старое имя")

    class Runner:
        _generated_event_stage_navigation = navigation
        stage_entrance = {"c1": button}

    entrance = campaign_package._generated_campaign_get_entrance(Runner(), "C1")

    assert entrance is button
    assert entrance.name == "C1"


def test_generated_adapter_binds_real_policy_and_canonical_map_name():
    module = ModuleType("campaign.generated_event.en_51101.b3")

    class Config:
        MAP_CHAPTER_SWITCH_20241219 = False
        MAP_CHAPTER_SWITCH_20241219_SP = False
        MAP_CHAPTER_SWITCH_20241219_SPEX = False
        MAP_CHAPTER_SWITCH_20260326 = False

    class Campaign:
        MAP = SimpleNamespace(name="B3")

        def campaign_set_chapter(self, _name, mode="normal"):
            return mode

        def campaign_get_entrance(self, name):
            return name

        def campaign_name_increase(self, name):
            return name

        def ensure_campaign_ui(
            self,
            name,
            mode="normal",
            skip_first_screenshot=True,
        ):
            return name, mode, skip_first_screenshot

    module.Config = Config
    module.Campaign = Campaign
    module.MAP = Campaign.MAP

    campaign_package._adapt_generated_campaign_ui(module, "20241219")

    navigation = Campaign._generated_event_stage_navigation
    assert navigation is not None
    assert navigation.auto_next == "c1"
    assert Campaign.campaign_name_increase is campaign_package._generated_campaign_name_increase
    assert Campaign.campaign_set_chapter is campaign_package._generated_campaign_set_chapter
    assert Campaign.campaign_get_entrance is campaign_package._generated_campaign_get_entrance
    assert Config.MAP_CHAPTER_SWITCH_20241219 is True
    assert Campaign().ensure_campaign_ui(
        "T6",
        mode="hard",
        skip_first_screenshot=False,
    ) == ("b3", "hard", False)


def test_generated_adapter_without_navigation_fails_closed_for_auto_advance(
    monkeypatch,
):
    module = ModuleType("campaign.generated_event.synthetic.alpha")

    class Config:
        MAP_CHAPTER_SWITCH_20241219 = False
        MAP_CHAPTER_SWITCH_20241219_SP = False
        MAP_CHAPTER_SWITCH_20241219_SPEX = False
        MAP_CHAPTER_SWITCH_20260326 = False

    class Campaign:
        MAP = SimpleNamespace(name="ALPHA")

        def campaign_set_chapter(self, name, mode="normal"):
            return name, mode

        def campaign_get_entrance(self, name):
            return name

        def campaign_name_increase(self, name):
            return f"legacy:{name}"

        def ensure_campaign_ui(
            self,
            name,
            mode="normal",
            skip_first_screenshot=True,
        ):
            return name, mode, skip_first_screenshot

    original_set_chapter = Campaign.campaign_set_chapter
    original_get_entrance = Campaign.campaign_get_entrance
    module.Config = Config
    module.Campaign = Campaign
    module.MAP = Campaign.MAP

    monkeypatch.setattr(
        campaign_package,
        "generated_stage_navigation_for_module",
        lambda _module_name: None,
    )
    campaign_package._adapt_generated_campaign_ui(module, "legacy")

    assert Campaign.campaign_set_chapter is original_set_chapter
    assert Campaign.campaign_get_entrance is original_get_entrance
    assert Campaign.campaign_name_increase is campaign_package._generated_campaign_name_increase

    runner = Campaign()
    runner.config = SimpleNamespace(STAGE_INCREASE_CUSTOM="")
    runner._campaign_stage_exists = lambda _stage: (_ for _ in ()).throw(
        AssertionError("Без auto_next проверка следующего этапа не нужна")
    )
    assert campaign_package._generated_campaign_name_increase(
        runner,
        "ALPHA",
    ) == "ALPHA"
