"""Concrete read-only Game Settings preflight scanner."""

from __future__ import annotations

from module.game_settings.definitions import (
    CUSTOM_SHIP_NAMES,
    CUSTOM_SHIP_NAMES_REQUIRED_OFF,
)
from module.game_settings.detector import detect_custom_ship_names
from module.game_settings.model import (
    GameSettingCheckResult,
    GameSettingsScanResult,
    GameSettingState,
)
from module.game_settings.scanner import GameSettingsScanner
from module.game_settings.traversal import OptionsViewport
from module.logger import logger


class GameSettingsPreflightScanner(GameSettingsScanner):
    """Проверить обязательные Game Settings без auto-fix или toggle clicks."""

    def _scan_game_settings(self) -> GameSettingsScanResult:
        detected_state = GameSettingState.UNKNOWN

        def visit(viewport: OptionsViewport) -> bool:
            nonlocal detected_state

            state = detect_custom_ship_names(self.device.image)
            if state is None:
                return False

            detected_state = state
            logger.info(
                "[Игровые настройки] Custom Ship Names найден в окне #%s "
                "(смещение %.1f px)",
                viewport.index,
                viewport.scroll_offset,
            )
            if state is GameSettingState.UNKNOWN:
                logger.warning(
                    "[Игровые настройки] Custom Ship Names найден, "
                    "но состояние неоднозначно"
                )
            else:
                logger.info(
                    "[Игровые настройки] Custom Ship Names: %s",
                    state.value.upper(),
                )
            # Row-present UNKNOWN тоже является терминальным результатом этого
            # single-setting Stage: overlap не должен превращать неоднозначность
            # в «строка ещё не найдена».
            return True

        # Cleanup охватывает и вход в Options: навигация может успеть изменить
        # страницу до того, как подтверждение входа выбросит исключение.
        primary_error: Exception | None = None
        try:
            self.ensure_options_page()
            traversal_result = self.traverse_options(visit)
            if not traversal_result.stopped_early:
                logger.warning(
                    "[Игровые настройки] Custom Ship Names не найден "
                    "до подтверждённого полного цикла Options"
                )

            check = GameSettingCheckResult(
                definition=CUSTOM_SHIP_NAMES,
                detected_state=detected_state,
                requirement=CUSTOM_SHIP_NAMES_REQUIRED_OFF,
            )
            logger.info(
                "[Игровые настройки] Требуемое состояние Custom Ship Names: OFF"
            )
            logger.info(
                "[Игровые настройки] Требование совместимо: %s",
                check.compatible,
            )
            return GameSettingsScanResult((check,))
        except Exception as exc:
            primary_error = exc
            raise
        finally:
            try:
                self.return_to_main()
            except Exception:
                if primary_error is None:
                    raise
                # Ошибка cleanup не должна подменять исходную scanner error.
                logger.warning(
                    "[Игровые настройки] Не удалось вернуться на главный экран "
                    "после ошибки сканирования"
                )
