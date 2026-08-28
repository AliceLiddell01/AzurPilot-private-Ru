from xml.etree import ElementTree
from types import SimpleNamespace

import numpy as np
import pytest

from module.application.low_morale import LowMoraleWarningDetector
from module.campaign.gems_farming import GemsFarming
from module.exception import ScriptEnd
from module.handler.info_handler import InfoHandler


def test_detector_requires_morale_and_consequence_evidence():
    detector = LowMoraleWarningDetector()

    assert detector.detect("Confirm or Cancel") is None
    assert detector.detect("The mood is low") is None
    assert detector.detect("Affinity will drop if you force the attack") is None
    assert (
        detector.detect(
            "Mood status. Affinity will be reduced if you continue attack."
        )
        is None
    )
    assert detector.detect("Low fuel. Morale status. Confirm or Cancel") is None


def test_detector_accepts_split_hierarchy_nodes():
    evidence = LowMoraleWarningDetector().detect_many(
        (
            "Morale",
            "is low",
            "Affinity will be reduced",
            "if forced to attack",
        )
    )

    assert evidence is not None
    assert evidence.proven


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
        ("cancel", ("LOW_MORALE_WARNING",), {"interval": 0}),
        ("warning", 1),
        ("reconcile", 1),
    ]


def _warning_handler(hierarchy_texts):
    root = ElementTree.Element("hierarchy")
    for text in hierarchy_texts:
        ElementTree.SubElement(root, "node", text=text)
    handler = object.__new__(InfoHandler)
    handler.device = SimpleNamespace(
        image=np.zeros((720, 1280, 3), dtype=np.uint8),
        dump_hierarchy=lambda: root,
    )
    return handler


def test_handler_uses_proven_hierarchy_without_unnecessary_ocr(monkeypatch):
    handler = _warning_handler(
        ("Cancel", "Confirm", "Morale is low. Affinity will be reduced if forced to attack.")
    )
    monkeypatch.setattr(handler, "appear", lambda *_args, **_kwargs: True)
    calls = []

    def unexpected_ocr(_image):
        calls.append("ocr")
        raise AssertionError("OCR must not run after proven hierarchy evidence")

    monkeypatch.setattr(
        "module.ocr.global_english.GLOBAL_ENGLISH_OCR.det",
        unexpected_ocr,
    )

    evidence = handler._low_morale_warning_evidence()

    assert evidence is not None
    assert calls == []


def test_handler_combines_partial_hierarchy_with_bounded_ocr(monkeypatch):
    handler = _warning_handler(("Cancel", "Confirm", "Morale"))
    monkeypatch.setattr(handler, "appear", lambda *_args, **_kwargs: True)
    received = []

    def fallback_ocr(image):
        received.append(image.shape)
        return [
            ("is low.", (0, 0, 1, 1), 0.99),
            ("Affinity will be reduced if forced to attack.", (0, 0, 1, 1), 0.99),
        ]

    monkeypatch.setattr("module.ocr.global_english.GLOBAL_ENGLISH_OCR.det", fallback_ocr)

    evidence = handler._low_morale_warning_evidence()

    assert evidence is not None
    assert received == [(355, 680, 3)]


def test_handler_keeps_unproven_hierarchy_and_malformed_ocr_closed(monkeypatch):
    handler = _warning_handler(("Cancel", "Confirm", "Mood status"))
    monkeypatch.setattr(handler, "appear", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        "module.ocr.global_english.GLOBAL_ENGLISH_OCR.det",
        lambda _image: [(None, (0, 0, 1, 1), 0.2), ("Affinity", None, 0.1)],
    )

    assert handler._low_morale_warning_evidence() is None


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


def test_handler_prefers_current_logical_fleet_over_stale_context():
    handler = object.__new__(InfoHandler)
    handler.fleet_current_index = 2
    handler._morale_fleet_index = 1
    handler._auto_search_fleet_index = 1

    assert handler._logical_morale_fleet_index() == 2


def test_gems_morale_compatibility_read_returns_safe_lower_bound_on_error():
    farming = object.__new__(GemsFarming)
    farming.config = SimpleNamespace(Fleet_FleetOrder="fleet1_all_fleet2_standby")
    farming.campaign = SimpleNamespace(
        config=SimpleNamespace(Fleet_Fleet1=2),
        emotion=SimpleNamespace(
            _service=lambda: (_ for _ in ()).throw(RuntimeError("projection unavailable"))
        ),
    )

    assert farming.get_emotion() == 0
