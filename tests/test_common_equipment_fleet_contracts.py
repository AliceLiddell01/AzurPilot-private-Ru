from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from module.auto_equip.auto_equip import AutoEquip
from module.equipment.equipment_code import EMPTY_CODE, EquipmentCodeHandler
from module.retire.scanner import Ship


ROOT = Path(__file__).resolve().parents[1]


def source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


class TestCommonEquipmentFleetRuntimeMessages:
    def test_representative_runtime_messages_are_russian(self):
        expected = {
            "module/auto_equip/auto_equip.py": (
                "Автоэкипировка текущего корабля",
                "Нет кораблей для автоэкипировки",
            ),
            "module/awaken/awaken.py": (
                "Стоимость пробуждения",
                "Недостаточно ресурсов для пробуждения",
            ),
            "module/equipment/equipment.py": (
                "[Экипировка — оснащение] Снятие экипировки",
            ),
            "module/equipment/equipment_change.py": (
                "[Экипировка — замена] Сохранение текущей экипировки",
            ),
            "module/equipment/equipment_code.py": (
                "[Код экипировки] FastInputIME не включён; пробуем включить",
                "Нет доступного кода экипировки; применение экипировки пропущено",
            ),
            "module/equipment/fleet_equipment.py": (
                'logger.attr("Индекс", current)',
            ),
            "module/retire/dock.py": (
                "[Списание — док] Тайм-аут определения количества выбранных кораблей",
            ),
            "module/retire/enhancement.py": (
                "[Списание — усиление] Достигнут лимит проверок",
            ),
            "module/retire/retirement.py": (
                "[Списание — подтверждение] Подтверждение списания",
                "[Списание — режим] Неизвестный режим списания",
            ),
            "module/retire/scanner.py": (
                "OCR настроения в доке",
                "[Списание — сканирование] Корабль не обнаружен",
            ),
        }

        for path, messages in expected.items():
            text = source(path)
            for message in messages:
                assert message in text, (path, message)

    def test_equipment_code_machine_contract_is_preserved(self):
        text = source("module/equipment/equipment_code.py")
        required = (
            'EMPTY_CODE = "MC8wLzAvMC8wXDA="',
            "U2_CONTROL_METHODS = {'uiautomator2', 'minitouch', 'MaaTouch'}",
            "android.settings.INPUT_METHOD_SETTINGS",
            '@text="FastInputIME"',
            "['input', 'keyevent', '4']",
            "input keyevent KEYCODE_MOVE_END",
            "input keyevent KEYCODE_ENTER",
            "['cmd', 'clipboard', 'get']",
            "['cmd', 'clipboard', 'get-primary-clip']",
            "'not found'",
            "'unknown command'",
            "'clipboard text:'",
            "return 'bogue'",
            "return 'hermes'",
            "return 'ranger'",
            "return 'langley'",
            "return 'DD'",
        )
        for token in required:
            assert token in text, token

    def test_resource_and_selection_contract_tokens_are_preserved(self):
        auto_equip = source("module/auto_equip/auto_equip.py")
        for token in (
            "AUTO_EQUIP_AFTER_EQUIP_WAIT = 3",
            "AUTO_EQUIP_EMPTY_SLOT_PLUS_SIMILARITY = 0.8",
            "AUTO_EQUIP_NO_EQUIPMENT_SIMILARITY = 0.85",
            "self.device.click(AUTO_EQUIP_WAREHOUSE_SECOND)",
            "self.device.click(AUTO_EQUIP_WAREHOUSE_FIRST)",
        ):
            assert token in auto_equip, token

        awaken = source("module/awaken/awaken.py")
        for token in (
            "return 'unexpected_array'",
            "return 'insufficient'",
            "return 'timeout'",
            "return 'success'",
            "stop_level = 125",
            "stop_level = 120",
        ):
            assert token in awaken, token

        retirement = source("module/retire/retirement.py")
        for token in (
            "mode = 'one_click_retire'",
            "elif mode == 'old_retire':",
            "filter_5='keep_limit_break'",
            "self.quick_retire_setting_set('all')",
            "if click_count >= 5:",
            "if count > 3:",
            "if swipe_count >= 7:",
            "COMMON_CV_FILTER_REGEX",
            "COMMON_DD_FILTER_REGEX",
        ):
            assert token in retirement, token


class TestCommonEquipmentFleetNegativePaths:
    def test_auto_equip_no_equipment_does_not_select_warehouse_item(self):
        subject = AutoEquip.__new__(AutoEquip)
        subject.device = MagicMock()
        subject.wait_until_stable = MagicMock()
        subject._warehouse_no_equipment = MagicMock(return_value=True)
        slot = SimpleNamespace(name="TEST_SLOT")

        result = AutoEquip._quick_fill_slot_from_warehouse(subject, slot)

        assert result is False
        subject.device.click.assert_called_once_with(slot)
        subject.device.screenshot.assert_called_once_with()
        subject._warehouse_no_equipment.assert_called_once_with()

    def test_equipment_code_parser_preserves_raw_error_boundary(self):
        assert EquipmentCodeHandler._code_from_text(f"error: {EMPTY_CODE}") is None
        assert EquipmentCodeHandler._code_from_text(f"clipboard text: {EMPTY_CODE}") == EMPTY_CODE

    def test_ship_filtering_is_decision_only(self):
        ship = Ship(rarity="common", level=42, emotion=100, fleet=0, status="free")

        assert ship.satisfy_limitation(
            {
                "rarity": "common",
                "level": (1, 125),
                "emotion": (0, 150),
                "fleet": 0,
                "status": "free",
            }
        )
        assert not ship.satisfy_limitation({"fleet": 1})
