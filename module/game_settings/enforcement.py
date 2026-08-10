"""Explicit fail-closed enforcement for canonical Settings -> Options values."""

from __future__ import annotations

from dataclasses import dataclass

from module.base.button import Button
from module.game_settings.model import (
    GameSettingAppliedChange,
    GameSettingsEnforcementResult,
    GameSettingsScanResult,
    is_unknown_game_setting_value,
)
from module.game_settings.options_detector import (
    GameSettingRowObservation,
    clear_game_settings_ocr_cache,
)
from module.game_settings.preflight import GameSettingsPreflightScanner
from module.game_settings.registry import (
    GameSettingCheckSpec,
    build_game_settings_registry,
)
from module.game_settings.traversal import OptionsViewport
from module.logger import logger


_SAFE_CLICK_HALF_SIZE = 8
_MAX_COMPACT_TARGET_SPAN = 64


@dataclass(frozen=True, slots=True)
class _ApplyFailure:
    key: str
    reason: str


class GameSettingsEnforcementScanner(GameSettingsPreflightScanner):
    """Read-only audit plus an explicitly invoked canonical mutation path."""

    def get_enforce_registry(self) -> tuple[GameSettingCheckSpec, ...]:
        return build_game_settings_registry(
            self.check_registry,
            require_enforce=True,
        )

    def enforce_required_game_settings(
        self,
        *,
        reaudit_on_noop: bool = False,
    ) -> GameSettingsEnforcementResult:
        """Apply only known mismatches after a complete fail-closed audit.

        This method is intentionally separate from ``scan_game_settings()``.
        Unknown or missing required rows block *all* mutation before the first
        click. Successfully applied canonical values are not rolled back if a
        later row fails verification.
        """

        before = self.scan_game_settings()
        registry = self.get_enforce_registry()
        required_unknown = tuple(
            check
            for check in before.required
            if is_unknown_game_setting_value(check.detected_value)
        )
        if required_unknown:
            keys = ", ".join(check.key for check in required_unknown)
            reason = f"Не удалось однозначно определить обязательные настройки: {keys}"
            logger.warning("[Игровые настройки] Применение заблокировано: %s", reason)
            return GameSettingsEnforcementResult(
                before=before,
                success=False,
                blocked_reason=reason,
            )

        plan = tuple(
            entry
            for entry in registry
            if (
                (check := before.get(entry.key)) is not None
                and check.is_required
                and check.compatible is False
            )
        )
        self._log_change_plan(before, plan)

        if not plan:
            # Production no-op is intentionally cheap. Tests/smoke can request
            # one additional fresh audit when they want to prove the no-op
            # result against a second traversal.
            after = self.scan_game_settings() if reaudit_on_noop else before
            success = after.all_required_compatible is True
            return GameSettingsEnforcementResult(
                before=before,
                after=after,
                success=success,
                failure_reason=(
                    None
                    if success
                    else "Финальный аудит не подтвердил совместимость всех настроек"
                ),
            )

        changes, failure = self._apply_change_plan(before, plan)
        if failure is not None:
            return GameSettingsEnforcementResult(
                before=before,
                changes=changes,
                success=False,
                failed_key=failure.key,
                failure_reason=failure.reason,
            )

        after = self.scan_game_settings()
        if after.all_required_compatible is not True:
            return GameSettingsEnforcementResult(
                before=before,
                changes=changes,
                after=after,
                success=False,
                failure_reason="Финальный аудит не подтвердил совместимость всех настроек",
            )

        logger.info("[Игровые настройки] Все обязательные настройки совместимы")
        return GameSettingsEnforcementResult(
            before=before,
            changes=changes,
            after=after,
            success=True,
        )

    @staticmethod
    def _log_change_plan(
        before: GameSettingsScanResult,
        plan: tuple[GameSettingCheckSpec, ...],
    ) -> None:
        logger.info("[Игровые настройки] План изменений:")
        if not plan:
            logger.info("[Игровые настройки] Изменения не требуются")
            return
        for entry in plan:
            check = before.get(entry.key)
            if check is None or check.required_value is None:
                continue
            logger.info(
                "[Игровые настройки] %s: текущее=%s, требуется=%s",
                entry.key,
                check.detected_value.value,
                check.required_value.value,
            )

    def _apply_change_plan(
        self,
        before: GameSettingsScanResult,
        plan: tuple[GameSettingCheckSpec, ...],
    ) -> tuple[tuple[GameSettingAppliedChange, ...], _ApplyFailure | None]:
        pending = {entry.key: entry for entry in plan}
        changes: list[GameSettingAppliedChange] = []
        failure: _ApplyFailure | None = None
        clear_game_settings_ocr_cache()

        def fail(key: str, reason: str) -> bool:
            nonlocal failure
            failure = _ApplyFailure(key=key, reason=reason)
            logger.error("[Игровые настройки] %s: ошибка применения: %s", key, reason)
            return True

        def visit(_viewport: OptionsViewport) -> bool:
            # GameSettingsScanner mirrors the detached traversal snapshot into
            # device.image, so this is the exact ndarray owned by traversal.
            # Keep that object identity stable across clicks: semantic matching
            # and motion after the callback must see the verified post-click UI.
            clear_game_settings_ocr_cache()
            frame = self.device.image

            for entry in plan:
                if entry.key not in pending:
                    continue
                observer = entry.observer
                if observer is None:
                    return fail(entry.key, "Не зарегистрирован observer для применения")

                observation = observer(frame)
                if observation is None:
                    continue
                initial = before.get(entry.key)
                if initial is None or initial.required_value is None:
                    return fail(entry.key, "Отсутствует результат начального аудита")
                required = initial.required_value

                if type(observation.value) is not type(required):
                    return fail(entry.key, "Изменилась типизированная группа значения")
                if is_unknown_game_setting_value(observation.value):
                    return fail(entry.key, "Текущее значение стало UNKNOWN до клика")
                if observation.value is not initial.detected_value:
                    return fail(
                        entry.key,
                        "Текущее значение изменилось после начального аудита",
                    )

                target = observation.option_for(required)
                if target is None:
                    return fail(
                        entry.key,
                        "Не удалось однозначно определить требуемую кнопку значения",
                    )
                click_bounds = self._safe_click_bounds(target.click_bounds)
                if not self._target_within_observed_row(observation, click_bounds):
                    return fail(
                        entry.key,
                        "Цель клика вышла за границы подтверждённой строки",
                    )

                logger.info(
                    "[Игровые настройки] %s: текущее=%s, требуется=%s, применяем",
                    entry.key,
                    observation.value.value,
                    required.value,
                )
                self.device.click(
                    Button(
                        area=click_bounds,
                        color=(0, 0, 0),
                        button=click_bounds,
                        name=f"GAME_SETTINGS_{entry.key.upper()}_TARGET",
                    )
                )

                clear_game_settings_ocr_cache()
                verified_frame = self._wait_options_stable()
                frame[...] = verified_frame
                self.device.image = frame
                verified = observer(frame)
                if verified is None or is_unknown_game_setting_value(verified.value):
                    clear_game_settings_ocr_cache()
                    verified_frame = self._wait_options_stable()
                    frame[...] = verified_frame
                    self.device.image = frame
                    verified = observer(frame)
                if verified is None:
                    return fail(entry.key, "Строка исчезла после клика")
                if is_unknown_game_setting_value(verified.value):
                    return fail(entry.key, "Проверка после клика вернула UNKNOWN")
                if verified.value is not required:
                    return fail(
                        entry.key,
                        (
                            "Проверка вернула {0}, ожидалось {1}"
                            .format(verified.value.value, required.value)
                        ),
                    )

                changes.append(
                    GameSettingAppliedChange(
                        key=entry.key,
                        before=observation.value,
                        after=verified.value,
                        verified=True,
                    )
                )
                pending.pop(entry.key)
                logger.info(
                    "[Игровые настройки] %s: подтверждено=%s",
                    entry.key,
                    verified.value.value,
                )

            return not pending

        primary_error: Exception | None = None
        try:
            traversal = self.traverse_options(visit)
            if failure is not None:
                return tuple(changes), failure
            if pending:
                if not traversal.reached_bottom:
                    failure = _ApplyFailure(
                        key=next(iter(pending)),
                        reason="Обход применения завершился до поиска всех строк",
                    )
                else:
                    failure = _ApplyFailure(
                        key=next(iter(pending)),
                        reason="Обязательная строка не найдена при применении",
                    )
                return tuple(changes), failure
            return tuple(changes), None
        except Exception as exc:
            primary_error = exc
            raise
        finally:
            clear_game_settings_ocr_cache()
            try:
                self.return_to_main()
            except Exception as cleanup_error:
                if primary_error is None and failure is None:
                    raise
                logger.warning(
                    "[Игровые настройки] Не удалось вернуться на Main: %s; "
                    "основная ошибка применения сохранена",
                    cleanup_error,
                )

    @staticmethod
    def _safe_click_bounds(
        target: tuple[int, int, int, int],
    ) -> tuple[int, int, int, int]:
        """Keep compact marker clicks central without escaping the target."""

        x1, y1, x2, y2 = target
        width = x2 - x1
        height = y2 - y1
        if width <= 0 or height <= 0:
            return target
        if width > _MAX_COMPACT_TARGET_SPAN or height > _MAX_COMPACT_TARGET_SPAN:
            return target

        safe_width = min(width, _SAFE_CLICK_HALF_SIZE * 2)
        safe_height = min(height, _SAFE_CLICK_HALF_SIZE * 2)
        center_x = (x1 + x2) / 2.0
        center_y = (y1 + y2) / 2.0
        return (
            int(round(center_x - safe_width / 2.0)),
            int(round(center_y - safe_height / 2.0)),
            int(round(center_x + safe_width / 2.0)),
            int(round(center_y + safe_height / 2.0)),
        )

    @staticmethod
    def _target_within_observed_row(
        observation: GameSettingRowObservation,
        target: tuple[int, int, int, int],
    ) -> bool:
        rx1, ry1, rx2, ry2 = observation.row_bounds
        x1, y1, x2, y2 = target
        horizontal_guard = 48
        vertical_guard = 12
        return (
            rx1 - horizontal_guard <= x1 < x2 <= rx2 + horizontal_guard
            and ry1 - vertical_guard <= y1 < y2 <= ry2 + vertical_guard
        )
