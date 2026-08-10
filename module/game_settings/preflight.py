"""Concrete heterogeneous read-only Game Settings preflight scanner."""

from __future__ import annotations

from module.game_settings.model import (
    GameSettingResult,
    GameSettingsScanResult,
    is_unknown_game_setting_value,
)
from module.game_settings.options_detector import clear_game_settings_ocr_cache
from module.game_settings.registry import (
    GAME_SETTINGS_OPTIONS_REGISTRY,
    GameSettingCheckSpec,
    build_game_settings_registry,
)
from module.game_settings.scanner import GameSettingsScanner
from module.game_settings.traversal import OptionsViewport
from module.logger import logger


class GameSettingsPreflightScanner(GameSettingsScanner):
    """Audit all registered Options requirements without changing settings."""

    check_registry = GAME_SETTINGS_OPTIONS_REGISTRY

    def get_check_registry(self) -> tuple[GameSettingCheckSpec, ...]:
        return build_game_settings_registry(self.check_registry)

    def _scan_game_settings(self) -> GameSettingsScanResult:
        registry = self.get_check_registry()
        if not registry:
            logger.info("[Игровые настройки] Preflight registry пуст; сканирование не требуется")
            return GameSettingsScanResult()

        clear_game_settings_ocr_cache()
        resolved: dict[str, GameSettingResult] = {}

        def visit(viewport: OptionsViewport) -> bool:
            # traverse_options() has already stabilized this viewport. Every
            # unresolved detector receives the exact same frame object. Text
            # detectors additionally share one cached OCR pass for that frame.
            frame = self.device.image

            for entry in registry:
                if entry.key in resolved:
                    continue

                value = entry.detector(frame)
                if value is None:
                    continue

                check = entry.make_result(value)
                resolved[entry.key] = check
                logger.info(
                    "[Игровые настройки] %s найден в окне #%s (смещение %.1f px)",
                    entry.key,
                    viewport.index,
                    viewport.scroll_offset,
                )
                if is_unknown_game_setting_value(value):
                    logger.warning(
                        "[Игровые настройки] %s найден, но значение неоднозначно",
                        entry.key,
                    )
                else:
                    logger.info(
                        "[Игровые настройки] %s: detected=%s",
                        entry.key,
                        value.value,
                    )

            return len(resolved) == len(registry)

        primary_error: Exception | None = None
        try:
            traversal_result = self.traverse_options(visit)

            if len(resolved) != len(registry):
                if not traversal_result.reached_bottom:
                    raise RuntimeError(
                        "[Game Settings] Traversal завершился без hard bottom при "
                        "неразрешённых registry entries."
                    )

                for entry in registry:
                    if entry.key in resolved:
                        continue
                    logger.warning(
                        "[Игровые настройки] %s: строка не найдена до "
                        "подтверждённого фактического низа Options; detected=UNKNOWN",
                        entry.key,
                    )
                    resolved[entry.key] = entry.make_unknown_result()

            result = GameSettingsScanResult(
                resolved[entry.key]
                for entry in registry
            )
            self._log_result(result)
            return result
        except Exception as exc:
            primary_error = exc
            raise
        finally:
            clear_game_settings_ocr_cache()
            try:
                self.return_to_main()
            except Exception:
                if primary_error is None:
                    raise
                logger.warning(
                    "[Игровые настройки] Не удалось вернуться на главный экран "
                    "после ошибки сканирования"
                )

    @staticmethod
    def _log_result(result: GameSettingsScanResult) -> None:
        logger.info("[Game Settings] Audit:")
        for check in result:
            required_value = check.required_value
            required = "none" if required_value is None else required_value.value
            logger.info(
                "[Игровые настройки] %s: detected=%s, required=%s, compatible=%s",
                check.key,
                check.detected_value.value,
                required,
                check.compatible,
            )
