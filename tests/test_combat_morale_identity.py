from types import SimpleNamespace

from module.combat.combat import Combat


class _RecordingEmotion:
    is_calculate = True

    def __init__(self):
        self.active_event_ids = []
        self.reductions = []

    def begin_event(self, event_key, *, execution_id):
        self.active_event_ids.append(execution_id)

    def reduce(self, fleet_index, *, battle=None):
        if self.active_event_ids[-1] not in self.reductions:
            self.reductions.append(self.active_event_ids[-1])


class _Stat:
    class _Drop:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    def new(self, **kwargs):
        return self._Drop()


def test_consecutive_ambush_battles_get_distinct_morale_execution_ids():
    emotion = _RecordingEmotion()
    combat = object.__new__(Combat)
    combat.config = SimpleNamespace(
        HpControl_UseHpBalance=False,
        Fleet_Fleet1Mode="combat_auto",
        Fleet_Fleet2Mode="combat_auto",
        Submarine_Fleet=False,
        campaign_name="campaign",
        DropRecord_CombatRecord=False,
    )
    combat.emotion = emotion
    combat.stat = _Stat()
    combat.battle_count = 0
    combat.combat_preparation = lambda **kwargs: None
    combat.combat_execute = lambda **kwargs: emotion.reduce(1)
    combat.combat_status = lambda **kwargs: None

    combat.combat(save_get_items=False, fleet_index=1)
    combat.combat(save_get_items=False, fleet_index=1)

    assert emotion.active_event_ids == [
        "combat:campaign:0:1:1",
        "combat:campaign:0:2:1",
    ]
    assert emotion.reductions == emotion.active_event_ids


def test_restart_uses_battle_coordinate_before_local_sequence():
    emotion = _RecordingEmotion()

    def make_combat(battle_count):
        combat = object.__new__(Combat)
        combat.config = SimpleNamespace(
            HpControl_UseHpBalance=False,
            Fleet_Fleet1Mode="combat_auto",
            Fleet_Fleet2Mode="combat_auto",
            Submarine_Fleet=False,
            campaign_name="campaign",
            DropRecord_CombatRecord=False,
        )
        combat.emotion = emotion
        combat.stat = _Stat()
        combat.battle_count = battle_count
        combat.combat_preparation = lambda **kwargs: None
        combat.combat_execute = lambda **kwargs: emotion.reduce(1)
        combat.combat_status = lambda **kwargs: None
        return combat

    make_combat(7).combat(save_get_items=False, fleet_index=1)
    make_combat(7).combat(save_get_items=False, fleet_index=1)
    make_combat(8).combat(save_get_items=False, fleet_index=1)

    assert emotion.active_event_ids == [
        "combat:campaign:7:1:1",
        "combat:campaign:7:1:1",
        "combat:campaign:8:1:1",
    ]
    assert emotion.reductions == [
        "combat:campaign:7:1:1",
        "combat:campaign:8:1:1",
    ]
