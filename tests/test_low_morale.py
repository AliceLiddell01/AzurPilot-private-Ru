from types import SimpleNamespace

import pytest

from module.application.low_morale import LowMoraleWarningDetector
from module.exception import ScriptEnd
from module.handler.info_handler import InfoHandler


def test_detector_requires_morale_and_consequence_evidence():
    detector = LowMoraleWarningDetector()

    assert detector.detect("Confirm or Cancel") is None
    assert detector.detect("The mood is low") is None
    assert detector.detect("Affinity will drop if you force the attack") is None


@pytest.mark.parametrize(
    "text",
    [
        "A ship's mood is very low. Affinity will be reduced if you force her to attack.",
        "Morale is low and the ship will lose affinity when continuing the battle.",
        "The spirit is red. Friendship drops if forced to fight.",
    ],
)
def test_detector_accepts_semantic_low_morale_warning_without_entity_literals(text):
    evidence = LowMoraleWarningDetector().detect(text)

    assert evidence is not None
    assert evidence.proven
    assert "hermione" not in evidence.normalized_text
    assert "fleet 1" not in evidence.normalized_text


def test_handler_cancels_proven_warning_reconciles_and_stops(monkeypatch):
    handler = object.__new__(InfoHandler)
    events = []
    handler.emotion = SimpleNamespace(
        is_ignore=False,
        record_warning=lambda fleet: events.append(("warning", fleet)),
    )
    handler._morale_fleet_index = 1
    evidence = LowMoraleWarningDetector().detect(
        "Morale is low. Affinity will be reduced if forced to attack."
    )
    assert evidence is not None
    monkeypatch.setattr(handler, "_low_morale_warning_evidence", lambda: evidence)
    monkeypatch.setattr(
        handler,
        "handle_popup_cancel",
        lambda *args, **kwargs: events.append(("cancel", args, kwargs)) or True,
    )
    monkeypatch.setattr(
        handler,
        "_reconcile_morale_after_warning",
        lambda fleet: events.append(("reconcile", fleet)) or "done",
    )

    with pytest.raises(ScriptEnd):
        handler._handle_low_morale_warning()

    assert events == [
        ("warning", 1),
        ("cancel", ("LOW_MORALE_WARNING",), {"interval": 0}),
        ("reconcile", 1),
    ]


def test_handler_never_confirms_proven_warning_under_safe_policy(monkeypatch):
    handler = object.__new__(InfoHandler)
    actions = []
    handler.emotion = SimpleNamespace(
        is_ignore=False,
        record_warning=lambda fleet: actions.append(("warning", fleet)),
    )
    handler._morale_fleet_index = 1
    evidence = LowMoraleWarningDetector().detect(
        "Morale is low. Affinity will be reduced if forced to attack."
    )
    assert evidence is not None
    monkeypatch.setattr(handler, "_low_morale_warning_evidence", lambda: evidence)
    monkeypatch.setattr(
        handler,
        "handle_popup_cancel",
        lambda *args, **kwargs: actions.append("cancel") or False,
    )
    monkeypatch.setattr(
        handler,
        "handle_popup_confirm",
        lambda *args, **kwargs: actions.append("confirm") or True,
    )

    with pytest.raises(ScriptEnd):
        handler._handle_low_morale_warning()

    assert "confirm" not in actions


def test_handler_cancels_before_clean_stop_when_warning_record_fails(monkeypatch):
    handler = object.__new__(InfoHandler)
    actions = []

    def record_warning(_fleet):
        actions.append("record")
        raise RuntimeError("ledger unavailable")

    handler.emotion = SimpleNamespace(
        is_ignore=False,
        record_warning=record_warning,
    )
    handler._morale_fleet_index = 1
    evidence = LowMoraleWarningDetector().detect(
        "Morale is low. Affinity will be reduced if forced to attack."
    )
    assert evidence is not None
    monkeypatch.setattr(handler, "_low_morale_warning_evidence", lambda: evidence)
    monkeypatch.setattr(
        handler,
        "handle_popup_cancel",
        lambda *args, **kwargs: actions.append("cancel") or True,
    )
    monkeypatch.setattr(
        handler,
        "handle_popup_confirm",
        lambda *args, **kwargs: actions.append("confirm") or True,
    )

    with pytest.raises(ScriptEnd):
        handler._handle_low_morale_warning(allow_confirm=True)

    assert actions == ["record", "cancel"]


def test_handler_does_not_guess_fleet_when_context_is_missing(monkeypatch):
    handler = object.__new__(InfoHandler)
    actions = []
    handler.emotion = SimpleNamespace(
        is_ignore=False,
        record_warning=lambda fleet: actions.append(("warning", fleet)),
    )
    evidence = LowMoraleWarningDetector().detect(
        "Morale is low. Affinity will be reduced if forced to attack."
    )
    assert evidence is not None
    monkeypatch.setattr(handler, "_low_morale_warning_evidence", lambda: evidence)
    monkeypatch.setattr(
        handler,
        "handle_popup_cancel",
        lambda *args, **kwargs: actions.append("cancel") or True,
    )
    monkeypatch.setattr(
        handler,
        "handle_popup_confirm",
        lambda *args, **kwargs: actions.append("confirm") or True,
    )

    with pytest.raises(ScriptEnd):
        handler._handle_low_morale_warning()

    assert actions == ["cancel"]
