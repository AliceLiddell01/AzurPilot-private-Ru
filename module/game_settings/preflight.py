"""Concrete read-only Game Settings preflight scanner."""

from __future__ import annotations

from module.game_settings.model import (
    GameSettingCheckResult,
    GameSettingsScanResult,
    GameSettingState,
)
from module.game_settings.registry import (
    GAME_SETTINGS_PREFLIGHT_REGISTRY,
    GameSettingCheckSpec,
    build_game_settings_registry,
)
from module.game_settings.scanner import GameSettingsScanner
from module.game_settings.traversal import OptionsViewport
from module.logger import logger


class GameSettingsPreflightScanner(GameSettingsScanner):
    """Проверить registered tri-state Game Settings без изменения настроек."""

    check_registry = GAME_SETTINGS_PREFLIGHT_REGISTRY

    def get_check_registry(self) -> tuple[GameSettingCheckSpec, ...]:
        """Вернуть validated registry, допускающий безопасный test override."""
        return build_game_settings_registry(self.check_registry)

    def _scan_game_settings(self) -> GameSettingsScanResult:
        registry = self.get_check_registry()
        if not registry:
            logger.info("[Игровые настройки] Preflight registry пуст; сканирование не требуется")
            return GameSettingsScanResult()

        resolved: dict[str, GameSettingCheckResult] = {}

        def visit(viewport: OptionsViewport) -> bool:
            # Все ещё unresolved detectors этого viewport анализируют один и тот
            # же уже стабилизированный frame. Дополнительный screenshot здесь
            # намеренно не выполняется.
            frame = self.device.image

            for entry in registry:
                if entry.key in resolved:
                    continue

                state = entry.detector(frame)
                if state is None:
                    continue

                check = GameSettingCheckResult(
                    definition=entry.definition,
                    detected_state=state,
                    requirement=entry.requirement,
                )
                resolved[entry.key] = check
                logger.info(
                    "[Игровые настройки] %s найден в окне #%s (смещение %.1f px)",
                    entry.key,
                    viewport.index,
                    viewport.scroll_offset,
                )
                if state is GameSettingState.UNKNOWN:
                    logger.warning(
                        "[Игровые настройки] %s найден, но состояние неоднозначно",
                        entry.key,
                    )
                else:
                    logger.info(
                        "[Игровые настройки] %s: detected=%s",
                        entry.key,
                        state.value.upper(),
                    )

            # Row-present UNKNOWN является resolved result. Traversal прекращается
            # только когда разрешены все entries, а не после первой найденной строки.
            return len(resolved) == len(registry)

        primary_error: Exception | None = None
        try:
            # traverse_options() сам гарантирует вход в Options ровно один раз.
            # Cleanup охватывает и ошибку навигации внутри traversal.
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
                    resolved[entry.key] = GameSettingCheckResult(
                        definition=entry.definition,
                        detected_state=GameSettingState.UNKNOWN,
                        requirement=entry.requirement,
                    )

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

    @staticmethod
    def _log_result(result: GameSettingsScanResult) -> None:
        for check in result:
            expected = check.expected_state
            required = "NONE" if expected is None else expected.value.upper()
            logger.info(
                "[Игровые настройки] %s: detected=%s, required=%s, compatible=%s",
                check.key,
                check.detected_state.value.upper(),
                required,
                check.compatible,
            )
