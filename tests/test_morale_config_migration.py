from module.config.config_updater import ConfigUpdater, legacy_emotion_state_present


def test_legacy_emotion_state_is_detected_in_supported_legacy_groups():
    assert legacy_emotion_state_present(
        {"Main": {"Emotion": {"Fleet1Value": 119}}}
    )
    assert not legacy_emotion_state_present(
        {"Main": {"Other": {"Fleet1Value": 119}}}
    )
    assert legacy_emotion_state_present(
        {"Main": {"PublicEmotion": {"Enable": True, "Tasks": "Main"}}}
    )


def test_config_update_removes_numeric_emotion_state_and_preserves_policy():
    updated = ConfigUpdater().config_update(
        {
            "Main": {
                "Emotion": {
                    "Fleet1Value": 17,
                    "Fleet1Record": "2026-08-27 10:00:00",
                    "Fleet1Recover": "not_in_dormitory",
                    "Fleet1Control": "prevent_red_face",
                }
            }
        }
    )

    emotion = updated["Main"]["Emotion"]
    assert emotion["Fleet1Control"] == "prevent_red_face"
    assert "Fleet1Value" not in emotion
    assert "Fleet1Record" not in emotion
    assert "Fleet1Recover" not in emotion


def test_config_update_removes_public_emotion_leftovers_without_touching_policy():
    updated = ConfigUpdater().config_update(
        {
            "Main": {
                "Emotion": {
                    "Fleet1Control": "keep_exp_bonus",
                    "Fleet2Control": "prevent_yellow_face",
                },
                "PublicEmotion": {
                    "Enable": True,
                    "Tasks": "Main, Event",
                },
            }
        }
    )

    assert "PublicEmotion" not in updated["Main"]
    assert updated["Main"]["Emotion"]["Fleet1Control"] == "keep_exp_bonus"
    assert updated["Main"]["Emotion"]["Fleet2Control"] == "prevent_yellow_face"
