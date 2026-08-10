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
        ambiguous: dict[str, GameSettingResult] = {}

        def visit(viewport: OptionsViewport) -> bool:
            # Cache lifetime is exactly one stabilized viewport. This remains
            # safe even if a screenshot backend reuses one numpy buffer and
            # overwrites its contents between captures.
            clear_game_settings_ocr_cache()
            frame = self.device.image

            for entry in registry:
                if entry.key in resolved:
                    continue

                value = entry.detector(frame)
                if value is None:
                    continue

                check = entry.make_result(value)
                if is_unknown_game_setting_value(value):
                    ambiguous[entry.key] = check
                    logger.warning(
                        "[Игровые настройки] %s найден в окне #%s, но значение "
                        "неоднозначно; продолжаем искать устойчивое значение до "
                        "фактического низа Options",
                        entry.key,
                        viewport.index,
                    )
                    continue

                resolved[entry.key] = check
                ambiguous.pop(entry.key, None)
                logger.info(
                    "[Игровые настройки] %s найден в окне #%s (смещение %.1f px)",
                    entry.key,
                    viewport.index,
                    viewport.scroll_offset,
                )
                logger.info(
                    "[Игровые настройки] %s: обнаружено=%s",
                    entry.key,
                    value.value,
                )

            # Read-only audit is a full-page contract. Finding every registry
            # row early is not sufficient evidence that the physical bottom
            # was reached, and UNKNOWN observations are intentionally retried.
            return False

        primary_error: Exception | None = None
        try:
            traversal_result = self.traverse_options(visit)

            if not traversal_result.reached_bottom:
                raise RuntimeError(
                    "Обход Game Settings завершился до подтверждённого "
                    "фактического низа Options."
                )

            for entry in registry:
                if entry.key in resolved:
                    continue
                if entry.key in ambiguous:
                    logger.warning(
                        "[Игровые настройки] %s: строка была найдена, но значение "
                        "осталось неоднозначным до подтверждённого фактического "
                        "низа Options; значение=UNKNOWN",
                        entry.key,
                    )
                    resolved[entry.key] = ambiguous[entry.key]
                    continue
                logger.warning(
                    "[Игровые настройки] %s: строка не найдена до "
                    "подтверждённого фактического низа Options; значение=UNKNOWN",
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
        logger.info("[Игровые настройки] Аудит:")
        for check in result:
            required_value = check.required_value
            required = "нет" if required_value is None else required_value.value
            logger.info(
                "[Игровые настройки] %s: обнаружено=%s, требуется=%s, совместимо=%s",
                check.key,
                check.detected_value.value,
                required,
                check.compatible,
            )