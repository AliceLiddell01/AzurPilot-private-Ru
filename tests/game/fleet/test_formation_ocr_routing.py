from module.ocr.global_english import should_use_general_english


def test_formation_ship_name_uses_general_english_model() -> None:
    assert should_use_general_english(
        None,
        name="FORMATION_SHIP_NAME",
        recognizer_type="_FormationNameOcrModel",
    ) is True


def test_formation_fleet_index_stays_on_compact_numeric_model() -> None:
    assert should_use_general_english(
        "0123456789IDSB",
        name="FORMATION_FLEET_INDEX",
        recognizer_type="Digit",
    ) is False
