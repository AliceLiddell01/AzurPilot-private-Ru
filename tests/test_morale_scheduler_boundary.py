from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from module.combat.emotion import Emotion
from module.exception import ScriptEnd


def test_check_reduce_converts_aware_recovery_to_local_naive_scheduler_target():
    recovered = datetime(
        2026,
        8,
        29,
        12,
        34,
        56,
        tzinfo=timezone(timedelta(hours=7)),
    )
    delayed = []
    config = SimpleNamespace(
        Emotion_Mode="calculate",
        task_delay=lambda **kwargs: delayed.append(kwargs),
    )
    emotion = Emotion(config)
    emotion._check_reduce = lambda battle: (recovered, True)

    with pytest.raises(ScriptEnd):
        emotion.check_reduce(1)

    assert len(delayed) == 1
    target = delayed[0]["target"]
    assert target.tzinfo is None
    assert target == recovered.astimezone().replace(tzinfo=None)
