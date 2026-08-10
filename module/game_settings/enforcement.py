"""Explicit fail-closed enforcement for canonical Settings -> Options values."""

from __future__ import annotations

from dataclasses import dataclass

from module.base.button import Button
from module.game_settings.model import (
    GameSettingAppliedChange,
    GameSettingResult,
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

    def enforce_required_game_settings(self) -> GameSettingsEnforcementResult:
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
            reason = f"Required settings unresolved: {keys}"
            logger.warning("[Game Settings] Enforce blocked: %s", reason)
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
            # A second read-only pass proves idempotent no-op behaviour against
            # a fresh traversal instead of merely echoing the initial audit.
            after = self.scan_game_settings()
            success = after.all_required_compatible is True
            return GameSettingsEnforcementResult(
                before=before,
                after=after,
                success=success,
                failure_reason=(
                    None if success else "Final audit is not fully compatible"
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
                failure_reason="Final audit is not fully compatible",
            )

        logger.info("[Game Settings] all required settings compatible")
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
        logger.info("[Game Settings] Planned changes:")
        if not plan:
            logger.info("[Game Settings] (none)")
            return
        for entry in plan:
            check = before.get(entry.key)
            if check is None or check.required_value is None:
                continue
            logger.info(
                "[Game Settings] %s: current=%s, required=%s",
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
            logger.error("[Game Settings] %s: apply failed: %s", key, reason)
            return True

        def visit(_viewport: OptionsViewport) -> bool:
            nonlocal failure
            frame = self.device.image

            for entry in plan:
                if entry.key not in pending:
                    continue
                observer = entry.observer
                if observer is None:
                    return fail(entry.key, "No enforce observer registered")

                observation = observer(frame)
                if observation is None:
                    continue
                initial = before.get(entry.key)
                if initial is None or initial.required_value is None:
                    return fail(entry.key, "Initial audit result is missing")
                required = initial.required_value

                if type(observation.value) is not type(required):
                    return fail(entry.key, "Apply observation changed value family")
                if is_unknown_game_setting_value(observation.value):
                    return fail(entry.key, "Current value became UNKNOWN before click")

                if observation.value is required:
                    # Another deterministic action may already have made this
                    # row canonical. Do not click an already-compatible target.
                    pending.pop(entry.key)
                    continue
                if observation.value is not initial.detected_value:
                    return fail(
                        entry.key,
                        "Current value drifted from the initial audited value",
                    )

                target = observation.option_for(required)
                if target is None:
                    return fail(entry.key, "Required target option is not uniquely located")
                if not self._target_within_observed_row(observation, target.click_bounds):
                    return fail(entry.key, "Required click target escaped observed row bounds")

                logger.info(
                    "[Game Settings] %s: current=%s, required=%s, applying",
                    entry.key,
                    observation.value.value,
                    required.value,
                )
                self.device.click(
                    Button(
                        area=target.click_bounds,
                        color=(0, 0, 0),
                        button=target.click_bounds,
                        name=f"GAME_SETTINGS_{entry.key.upper()}_TARGET",
                    )
                )

                # The traversal stabilization loop itself takes fresh screenshots.
                # Verification never reuses the pre-click frame.
                clear_game_settings_ocr_cache()
                verified_frame = self._wait_options_stable()
                verified = observer(verified_frame)
                if verified is None:
                    # One additional bounded stabilization is allowed for a row
                    # temporarily disappearing during UI animation.
                    clear_game_settings_ocr_cache()
                    verified_frame = self._wait_options_stable()
                    verified = observer(verified_frame)
                if verified is None:
                    return fail(entry.key, "Row disappeared after click")
                if is_unknown_game_setting_value(verified.value):
                    return fail(entry.key, "Verification returned UNKNOWN")
                if verified.value is not required:
                    return fail(
                        entry.key,
                        f"Verification returned {verified.value.value}, expected {required.value}",
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
                frame = verified_frame
                logger.info(
                    "[Game Settings] %s: verified=%s",
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
                    return (
                        tuple(changes),
                        _ApplyFailure(
                            key=next(iter(pending)),
                            reason="Apply traversal ended before pending rows were found",
                        ),
                    )
                return (
                    tuple(changes),
                    _ApplyFailure(
                        key=next(iter(pending)),
                        reason="Required row not found during apply traversal",
                    ),
                )
            return tuple(changes), None
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
                    "[Game Settings] Не удалось вернуться на главный экран "
                    "после operational apply failure"
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
