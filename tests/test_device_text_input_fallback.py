import pytest

from module.device.input import Input


class _Input(Input):
    def __init__(self, *, u2_failures=0, adb_failure=False):
        self.u2_failures = u2_failures
        self.adb_failure = adb_failure
        self.u2_calls = []
        self.adb_calls = []

    def u2_send_keys(self, text: str, clear: bool = False):
        self.u2_calls.append(("keys", text, clear))
        if self.u2_failures:
            self.u2_failures -= 1
            raise RuntimeError("FastInputIME started failed")

    def u2_send_action(self, code):
        self.u2_calls.append(("action", code))

    def adb_shell(self, cmd, **_kwargs):
        call = list(cmd)
        self.adb_calls.append(call)
        if self.adb_failure:
            raise RuntimeError("adb input failed")
        return ""


def test_u2_success_keeps_existing_primary_path():
    device = _Input()

    device.text_input_and_confirm("Argus", clear=True)

    assert device.u2_calls == [("keys", "Argus", True), ("action", 6)]
    assert device.adb_calls == []
    assert not getattr(device, "_text_input_prefer_adb", False)


def test_fastinput_failure_uses_adb_clear_text_and_enter_fallback():
    device = _Input(u2_failures=1)

    device.text_input_and_confirm("Langley II", clear=True)

    assert device.u2_calls == [("keys", "Langley II", True)]
    assert device.adb_calls[0] == ["input", "keyevent", "KEYCODE_MOVE_END"]
    assert device.adb_calls[1][:2] == ["input", "keyevent"]
    assert device.adb_calls[1][2:] == ["KEYCODE_DEL"] * device._ADB_CLEAR_KEY_COUNT
    assert device.adb_calls[2] == ["input", "text", "Langley%sII"]
    assert device.adb_calls[3] == ["input", "keyevent", "KEYCODE_ENTER"]
    assert device._text_input_prefer_adb is True


def test_successful_fallback_is_reused_without_touching_broken_u2_again():
    device = _Input(u2_failures=1)

    device.text_input_and_confirm("Langley II", clear=True)
    first_u2_calls = tuple(device.u2_calls)
    device.adb_calls.clear()

    device.text_input_and_confirm("Arizona", clear=True)

    assert tuple(device.u2_calls) == first_u2_calls
    assert device.adb_calls[0] == ["input", "keyevent", "KEYCODE_MOVE_END"]
    assert device.adb_calls[2] == ["input", "text", "Arizona"]
    assert device.adb_calls[3] == ["input", "keyevent", "KEYCODE_ENTER"]


def test_non_ascii_text_does_not_use_lossy_adb_fallback():
    device = _Input(u2_failures=3)

    with pytest.raises(RuntimeError, match="FastInputIME"):
        device.text_input_and_confirm("Émile Bertin", clear=True)

    assert len(device.u2_calls) == 3
    assert device.adb_calls == []


def test_empty_text_is_rejected_before_device_side_effects():
    device = _Input()

    with pytest.raises(ValueError, match="непустой"):
        device.text_input_and_confirm("", clear=True)

    assert device.u2_calls == []
    assert device.adb_calls == []
