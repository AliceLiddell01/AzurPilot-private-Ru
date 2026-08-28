from types import SimpleNamespace

import pytest

from module.combat.emotion import Emotion
from module.exception import RequestHumanTakeover


class _RecordingService:
    def __init__(self):
        self.events = []

    def apply_event(self, instance, event):
        self.events.append((instance, event))
        return SimpleNamespace(
            exact_slots=(1,),
            applied_slots=1,
            skipped_slots=0,
        )


def _config(storage=None, *, run="same-run"):
    return SimpleNamespace(
        config_name="ap",
        Fleet_Fleet1=6,
        Fleet_Fleet2=2,
        Fleet_FleetOrder="fleet1_all_fleet2_standby",
        Scheduler_NextRun=run,
        Storage_Storage=dict(storage or {}),
        task=SimpleNamespace(command="Main"),
    )


def test_restart_before_apply_reuses_persisted_execution_generation():
    caller = "combat:campaign:0:0:1:1"
    first_config = _config({"unrelated": "keep"})
    first = Emotion(first_config)
    first.begin_event(caller, execution_id=caller)
    first_key = first._active_event_key

    restarted_config = _config(first_config.Storage_Storage)
    restarted = Emotion(restarted_config)
    restarted.begin_event(caller, execution_id=caller)

    assert restarted._active_event_key == first_key
    assert restarted_config.Storage_Storage["unrelated"] == "keep"
    assert restarted_config.Storage_Storage["MoraleCombatExecution"] == {
        "run": restarted._active_execution_storage[0],
        "caller": restarted._active_execution_storage[1],
        "sequence": 1,
        "applied": False,
    }


def test_restart_after_applied_battle_advances_persisted_execution_generation():
    caller = "combat:campaign:0:0:1:1"
    service = _RecordingService()
    first_config = _config()
    first = Emotion(first_config, morale_service=service)
    first.begin_event(caller, execution_id=caller)
    first_key = first._active_event_key
    first.reduce(1)

    assert first_config.Storage_Storage["MoraleCombatExecution"]["applied"] is True

    restarted_config = _config(first_config.Storage_Storage)
    restarted = Emotion(restarted_config, morale_service=service)
    restarted.begin_event(caller, execution_id=caller)

    assert restarted._active_event_key != first_key
    assert restarted_config.Storage_Storage["MoraleCombatExecution"]["sequence"] == 2
    assert restarted_config.Storage_Storage["MoraleCombatExecution"]["applied"] is False


def test_new_caller_coordinate_advances_generation_without_prior_apply():
    first_config = _config()
    first = Emotion(first_config)
    first.begin_event(
        "combat:campaign:0:0:1:1",
        execution_id="combat:campaign:0:0:1:1",
    )
    first_key = first._active_event_key

    restarted_config = _config(first_config.Storage_Storage)
    restarted = Emotion(restarted_config)
    restarted.begin_event(
        "combat:campaign:0:0:2:1",
        execution_id="combat:campaign:0:0:2:1",
    )

    assert restarted._active_event_key != first_key
    assert restarted_config.Storage_Storage["MoraleCombatExecution"]["sequence"] == 2


def test_corrupt_persisted_execution_coordinate_fails_closed():
    config = _config({"MoraleCombatExecution": {"sequence": "broken"}})
    emotion = Emotion(config)

    with pytest.raises(RequestHumanTakeover):
        emotion.begin_event(
            "combat:campaign:0:0:1:1",
            execution_id="combat:campaign:0:0:1:1",
        )
