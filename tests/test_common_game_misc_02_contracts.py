from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from module.game_setting.player_prefs import PlayerPrefsUnsupported, update_player_prefs_xml
from module.raid.assets import TICKET_USE_CANCEL, TICKET_USE_CONFIRM
from module.raid.raid import Raid
from module.shipyard.shipyard_reward import RewardShipyard


ROOT = Path(__file__).resolve().parents[1]


def source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


class TestCommonGameMisc02RuntimeMessages:
    def test_repaired_owner_files_have_group3_russian_messages(self):
        expected = {
            "module/game_setting/player_prefs.py": (
                "Неподдерживаемый корневой узел PlayerPrefs",
                "Небезопасный формат имени пакета игры",
                "Процесс игры всё ещё запущен; запись пропущена",
                "Метаданные файла PlayerPrefs после записи неверны",
            ),
            "module/raid/raid.py": (
                "Условие остановки: лимит топлива",
                "Условие остановки: лимит PT события",
                "Запуск рейда",
                "Тайм-аут ожидания PT; считаем, что значение достигнуто",
            ),
            "module/shipyard/shipyard_reward.py": (
                "Можно купить не более",
                "Верфь — покупка",
                "Последний запуск верфи DR",
                "Задание верфи PR уже выполнялось сегодня; остановка",
            ),
            "module/shipyard/ui.py": (
                "Можно израсходовать все",
                "[Верфь — UI] Индекс навигации",
                "переход к проверке OCR",
                "исследование текущего корабля ещё не завершено",
            ),
        }
        for path, messages in expected.items():
            text = source(path)
            for message in messages:
                assert message in text, (path, message)


class TestCommonGameMisc02PlayerPrefsSemantics:
    def test_player_prefs_invalid_root_remains_fail_closed(self):
        with pytest.raises(PlayerPrefsUnsupported) as exc_info:
            update_player_prefs_xml(b"<list />")

        assert "Неподдерживаемый корневой узел PlayerPrefs" in str(exc_info.value)

    def test_player_prefs_wrong_target_type_remains_fail_closed(self):
        content = b'<map><string name="fps_limit">60</string></map>'

        with pytest.raises(PlayerPrefsUnsupported) as exc_info:
            update_player_prefs_xml(content)

        assert "XML-тип целевого ключа 'fps_limit' не int" in str(exc_info.value)


class TestCommonGameMisc02ResourceSemantics:
    def test_raid_ticket_setting_still_selects_confirm_or_cancel(self):
        raid = object.__new__(Raid)
        raid.appear = Mock(return_value=True)
        raid.device = SimpleNamespace(click=Mock())

        raid.config = SimpleNamespace(Raid_UseTicket=True)
        assert raid.handle_raid_ticket_use() is True
        raid.device.click.assert_called_once_with(TICKET_USE_CONFIRM)

        raid.device.click.reset_mock()
        raid.config = SimpleNamespace(Raid_UseTicket=False)
        assert raid.handle_raid_ticket_use() is True
        raid.device.click.assert_called_once_with(TICKET_USE_CANCEL)

    def test_shipyard_pr_dr_prices_and_purchase_arithmetic_are_unchanged(self):
        shipyard = object.__new__(RewardShipyard)

        shipyard._shipyard_bp_rarity = "PR"
        assert shipyard._shipyard_get_cost(1) == 0
        assert shipyard._shipyard_get_cost(3) == 150
        assert shipyard._shipyard_get_cost(5) == 300
        assert shipyard._shipyard_get_cost(16) == 1500

        shipyard._shipyard_bp_rarity = "DR"
        assert shipyard._shipyard_get_cost(1) == 0
        assert shipyard._shipyard_get_cost(3) == 600
        assert shipyard._shipyard_get_cost(7) == 1200
        assert shipyard._shipyard_get_cost(16) == 6000

        shipyard._shipyard_bp_rarity = "PR"
        shipyard._coin_count = 450
        assert shipyard._shipyard_calculate(start=1, count=6, pay=False) == (5, 4)
        assert shipyard._coin_count == 450
