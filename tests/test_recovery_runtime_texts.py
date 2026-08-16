from __future__ import annotations

from module.webui.lang import _RECOVERY_STAGE3_TRANSLATIONS


def test_recovery_help_describes_default_on_escalation_order():
    game_help = _RECOVERY_STAGE3_TRANSLATIONS['Error.GameStuckRestart.help']
    adb_help = _RECOVERY_STAGE3_TRANSLATIONS['Error.AdbOfflineRestart.help']

    assert 'Включено по умолчанию' in game_help
    assert game_help.index('сначала перезапускает только Azur Lane') < game_help.index('эмулятор')
    assert 'штатной остановки' in game_help
    assert 'можно отключить вручную' in game_help

    assert 'Включено по умолчанию' in adb_help
    assert 'ограниченную проверяемую цепочку' in adb_help
    assert 'можно отключить вручную' in adb_help


def test_threshold_help_matches_incident_budget_semantics_not_seconds():
    game_name = _RECOVERY_STAGE3_TRANSLATIONS['Error.GameStuckThreshold.name']
    game_help = _RECOVERY_STAGE3_TRANSLATIONS['Error.GameStuckThreshold.help']
    adb_name = _RECOVERY_STAGE3_TRANSLATIONS['Error.AdbOfflineThreshold.name']
    adb_help = _RECOVERY_STAGE3_TRANSLATIONS['Error.AdbOfflineThreshold.help']

    assert 'инцидентов' in game_name
    assert 'Внутренние попытки запуска эмулятора не увеличивают этот счётчик' in game_help
    assert 'успешно завершённой задачи счётчик сбрасывается' in game_help

    assert 'инцидентов' in adb_name
    assert 'секунд' not in adb_name.lower()
    assert 'отдельный budget' in adb_help
    assert 'успешно завершённой задачи счётчик сбрасывается' in adb_help
