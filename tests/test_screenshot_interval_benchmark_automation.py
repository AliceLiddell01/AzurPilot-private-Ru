from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from module.daemon.screenshot_interval_benchmark import (
    AutomatedScreenshotIntervalBenchmark,
    ScreenshotIntervalBenchmarkError,
    _compact_ocr_text,
    run_screenshot_interval_benchmark,
)
from module.os_ash.assets import ASH_START
from module.os_ash.meta import OpsiAshBeacon
from module.ui.assets import BACK_ARROW


class ScreenshotIntervalBenchmarkAutomationTests(unittest.TestCase):
    def test_compact_ocr_text_preserves_only_ascii_letters(self) -> None:
        self.assertEqual(_compact_ocr_text("Battle Simulation"), "BATTLESIMULATION")
        self.assertEqual(_compact_ocr_text(" SIMULATION! "), "SIMULATION")

    def test_simulation_button_requires_simulation_token(self) -> None:
        benchmark = AutomatedScreenshotIntervalBenchmark.__new__(
            AutomatedScreenshotIntervalBenchmark
        )
        benchmark.device = SimpleNamespace(image=object())
        ocr = SimpleNamespace(
            det=Mock(
                return_value=[
                    (
                        "Battle",
                        [[1000, 580], [1120, 580], [1120, 620], [1000, 620]],
                        0.99,
                    )
                ]
            )
        )
        models = SimpleNamespace(ppocr_v6=ocr)
        with (
            patch("module.ocr.models.OCR_MODEL", models),
            self.assertRaises(ScreenshotIntervalBenchmarkError) as raised,
        ):
            benchmark._find_simulation_button()
        self.assertIn("Обычная атака не запускалась", str(raised.exception))

    def test_simulation_button_uses_highest_scoring_safe_candidate(self) -> None:
        benchmark = AutomatedScreenshotIntervalBenchmark.__new__(
            AutomatedScreenshotIntervalBenchmark
        )
        benchmark.device = SimpleNamespace(image=object())
        ocr = SimpleNamespace(
            det=Mock(
                return_value=[
                    (
                        "Simulation",
                        [[900, 500], [1000, 500], [1000, 540], [900, 540]],
                        0.55,
                    ),
                    (
                        "Battle Simulation",
                        [[1000, 580], [1240, 580], [1240, 640], [1000, 640]],
                        0.95,
                    ),
                    (
                        "Simulation",
                        [[100, 100], [300, 100], [300, 140], [100, 140]],
                        0.99,
                    ),
                ]
            )
        )
        models = SimpleNamespace(ppocr_v6=ocr)
        with patch("module.ocr.models.OCR_MODEL", models):
            self.assertEqual(benchmark._find_simulation_button(), (1120, 610))

    def test_simulation_button_wait_retries_after_loading_frame(self) -> None:
        benchmark = AutomatedScreenshotIntervalBenchmark.__new__(
            AutomatedScreenshotIntervalBenchmark
        )
        benchmark.device = SimpleNamespace(screenshot=Mock(), sleep=Mock())
        benchmark.simulation_button_timeout_s = 1.0
        benchmark._find_simulation_button = Mock(
            side_effect=[
                ScreenshotIntervalBenchmarkError("loading"),
                (1120, 610),
            ]
        )

        self.assertEqual(benchmark._wait_for_simulation_button(), (1120, 610))
        self.assertEqual(benchmark.device.screenshot.call_count, 2)
        benchmark.device.sleep.assert_called_once_with(0.5)

    def test_wait_until_handles_popup_before_target_screen(self) -> None:
        benchmark = AutomatedScreenshotIntervalBenchmark.__new__(
            AutomatedScreenshotIntervalBenchmark
        )
        benchmark.transition_timeout_s = 1.0
        benchmark.device = SimpleNamespace(screenshot=Mock(), sleep=Mock())
        predicate = Mock(side_effect=[False, True])
        additional = Mock(return_value=True)

        with patch(
            "module.daemon.screenshot_interval_benchmark.time.monotonic",
            side_effect=[0.0, 0.1, 0.2],
        ):
            benchmark._wait_until(
                predicate,
                description="Formation fixture",
                additional=additional,
            )

        self.assertEqual(benchmark.device.screenshot.call_count, 2)
        additional.assert_called_once_with()
        benchmark.device.sleep.assert_not_called()

    def test_meta_simulation_click_uses_button_asset(self) -> None:
        benchmark = AutomatedScreenshotIntervalBenchmark.__new__(
            AutomatedScreenshotIntervalBenchmark
        )
        benchmark.config = SimpleNamespace(SERVER="en")
        benchmark.device = SimpleNamespace(click=Mock(), screenshot=Mock())
        benchmark._enter_current_target = Mock()
        benchmark._wait_for_simulation_button = Mock(return_value=(1167, 668))
        benchmark._wait_until = Mock()
        benchmark.handle_popup_confirm = Mock(return_value=True)
        benchmark._benchmark_combat = None
        combat = SimpleNamespace(
            combat_preparation=Mock(),
            is_combat_executing=Mock(return_value=True),
        )

        with patch(
            "module.daemon.screenshot_interval_benchmark.AshCombat",
            return_value=combat,
        ):
            result = benchmark._enter_meta_simulation()

        self.assertIs(result, combat)
        benchmark.device.click.assert_called_once_with(ASH_START)
        benchmark._wait_for_simulation_button.assert_called_once_with()
        wait_kwargs = benchmark._wait_until.call_args.kwargs
        self.assertTrue(wait_kwargs["additional"]())
        benchmark.handle_popup_confirm.assert_called_once_with(
            "BATTLE_SIMULATION",
            interval=0,
        )
        self.assertIs(benchmark._benchmark_combat, combat)

    def test_finish_meta_simulation_waits_for_natural_battle_result(self) -> None:
        benchmark = AutomatedScreenshotIntervalBenchmark.__new__(
            AutomatedScreenshotIntervalBenchmark
        )
        benchmark.device = SimpleNamespace(
            screenshot_interval_set=Mock(),
            screenshot=Mock(),
            click=Mock(),
        )
        benchmark.appear = Mock(return_value=False)
        benchmark._in_meta_page = Mock(return_value=True)
        combat = SimpleNamespace(
            combat_execute=Mock(),
            combat_status=Mock(),
        )

        benchmark._finish_meta_simulation(combat)

        benchmark.device.screenshot_interval_set.assert_called_once_with("combat")
        combat.combat_execute.assert_called_once_with(
            auto="combat_auto",
            submarine="do_not_use",
            drop=None,
        )
        combat.combat_status.assert_called_once()
        self.assertIsNone(combat.combat_status.call_args.kwargs["drop"])
        expected_end = combat.combat_status.call_args.kwargs["expected_end"]
        self.assertTrue(expected_end())
        benchmark.device.click.assert_not_called()

    def test_natural_completion_callback_leaves_stray_formation(self) -> None:
        benchmark = AutomatedScreenshotIntervalBenchmark.__new__(
            AutomatedScreenshotIntervalBenchmark
        )
        benchmark.device = SimpleNamespace(
            screenshot_interval_set=Mock(),
            screenshot=Mock(),
            click=Mock(),
        )
        benchmark.appear = Mock(return_value=False)
        benchmark._in_meta_page = Mock(return_value=True)
        combat = SimpleNamespace(
            combat_execute=Mock(),
            combat_status=Mock(),
        )

        benchmark._finish_meta_simulation(combat)
        expected_end = combat.combat_status.call_args.kwargs["expected_end"]
        benchmark.appear.return_value = True
        benchmark._in_meta_page.return_value = False

        self.assertFalse(expected_end())
        benchmark.device.click.assert_called_once_with(BACK_ARROW)

    def test_run_phase_resets_stuck_guard_before_each_candidate(self) -> None:
        benchmark = AutomatedScreenshotIntervalBenchmark.__new__(
            AutomatedScreenshotIntervalBenchmark
        )
        benchmark.device = SimpleNamespace(stuck_record_clear=Mock())
        benchmark.duration_per_candidate_s = 0.5
        benchmark.warmup_frames = 0
        result = SimpleNamespace(
            achieved_fps=20.0,
            interval_p95_ms=50.0,
            deadline_miss_ratio=0.0,
            stable=True,
            error=None,
        )
        with patch(
            "module.daemon.screenshot_interval_benchmark._benchmark_interval",
            return_value=result,
        ) as measure:
            results = benchmark._run_phase(phase="normal", intervals=[0.05, 0.1])

        self.assertEqual(results, [result, result])
        self.assertEqual(benchmark.device.stuck_record_clear.call_count, 2)
        self.assertEqual(
            measure.call_args_list,
            [
                call(
                    benchmark.device,
                    phase="normal",
                    interval=0.05,
                    duration=0.5,
                    warmup_frames=0,
                ),
                call(
                    benchmark.device,
                    phase="normal",
                    interval=0.1,
                    duration=0.5,
                    warmup_frames=0,
                ),
            ],
        )

    def test_meta_navigation_helpers_are_inherited_from_ash_task(self) -> None:
        self.assertTrue(
            issubclass(AutomatedScreenshotIntervalBenchmark, OpsiAshBeacon)
        )
        benchmark = AutomatedScreenshotIntervalBenchmark.__new__(
            AutomatedScreenshotIntervalBenchmark
        )
        self.assertTrue(callable(benchmark._ensure_meta_page))
        self.assertTrue(callable(benchmark._in_meta_page))

    def test_no_active_boss_screen_still_allows_battle_simulation(self) -> None:
        benchmark = AutomatedScreenshotIntervalBenchmark.__new__(
            AutomatedScreenshotIntervalBenchmark
        )
        benchmark._ensure_meta_showdown = Mock()
        benchmark._wait_until = Mock()
        benchmark.device = SimpleNamespace(click=Mock())
        benchmark.appear = Mock(return_value=True)

        benchmark._enter_current_target()

        benchmark._ensure_meta_showdown.assert_called_once_with()
        benchmark._wait_until.assert_called_once()
        benchmark.device.click.assert_called_once()

    def test_automated_route_runs_campaign_before_meta_simulation(self) -> None:
        benchmark = AutomatedScreenshotIntervalBenchmark.__new__(
            AutomatedScreenshotIntervalBenchmark
        )
        events: list[str] = []
        benchmark.config = SimpleNamespace(
            config_name="alas",
            Optimization_ScreenshotInterval=0.1,
            Optimization_CombatScreenshotInterval=0.3,
            Emulator_ScreenshotMethod="nemu_ipc",
            Emulator_PackageName="com.YoStarEN.AzurLane",
        )
        benchmark.device = SimpleNamespace(
            screenshot_interval_set=Mock(),
        )
        benchmark._prepare_normal_scene = lambda: events.append("normal_scene")
        benchmark._enter_meta_simulation = lambda: (
            events.append("combat_scene") or SimpleNamespace()
        )
        benchmark._finish_meta_simulation = lambda _combat: events.append("finish")
        benchmark.ui_goto_main = lambda: events.append("main")
        normal_result = SimpleNamespace()
        combat_result = SimpleNamespace()

        def run_phase(*, phase, intervals):
            del intervals
            events.append(phase)
            return [normal_result if phase == "normal" else combat_result]

        benchmark._run_phase = run_phase
        with patch(
            "module.daemon.screenshot_interval_benchmark._sha256",
            return_value="same",
        ), patch(
            "module.daemon.screenshot_interval_benchmark._recommend_profiles",
            return_value={
                "recommended_profile": "balanced",
                "profiles": {
                    "balanced": {"normal_s": 0.05, "combat_s": 0.15}
                },
            },
        ), patch(
            "module.daemon.screenshot_interval_benchmark.asdict",
            side_effect=lambda result: {
                "phase": "normal" if result is normal_result else "combat"
            },
        ), patch(
            "module.daemon.screenshot_interval_benchmark.DEFAULT_REPORT"
        ) as report_path, patch(
            "module.daemon.screenshot_interval_benchmark._write_markdown"
        ), patch(
            "module.daemon.screenshot_interval_benchmark._result_table"
        ), patch(
            "module.daemon.screenshot_interval_benchmark.logger.print"
        ):
            report_path.parent.mkdir = Mock()
            report_path.write_text = Mock()
            report = benchmark.run()

        self.assertEqual(
            events,
            [
                "normal_scene",
                "normal",
                "combat_scene",
                "combat",
                "finish",
                "main",
            ],
        )
        self.assertEqual(report["normal_context"], "campaign_page")
        self.assertEqual(
            report["combat_context"],
            "meta_current_target_battle_simulation",
        )
        self.assertEqual(
            report["automation"]["simulation_completion"],
            "natural_combat_execute_and_status",
        )
        self.assertTrue(report["automation"]["returned_to_main"])
        self.assertFalse(report["automatic_config_write"])

    def test_partial_navigation_failure_still_returns_to_main(self) -> None:
        benchmark = AutomatedScreenshotIntervalBenchmark.__new__(
            AutomatedScreenshotIntervalBenchmark
        )
        benchmark.config = SimpleNamespace(
            config_name="alas",
            Optimization_ScreenshotInterval=0.1,
            Optimization_CombatScreenshotInterval=0.3,
            Emulator_ScreenshotMethod="nemu_ipc",
            Emulator_PackageName="com.YoStarEN.AzurLane",
        )
        benchmark.device = SimpleNamespace(screenshot_interval_set=Mock())
        benchmark._prepare_normal_scene = Mock()
        benchmark._run_phase = Mock(return_value=[SimpleNamespace()])
        benchmark._enter_meta_simulation = Mock(
            side_effect=ScreenshotIntervalBenchmarkError("simulation fixture")
        )
        benchmark.ui_goto_main = Mock()

        with patch(
            "module.daemon.screenshot_interval_benchmark._sha256",
            return_value="same",
        ), self.assertRaises(ScreenshotIntervalBenchmarkError):
            benchmark.run()

        benchmark.ui_goto_main.assert_called_once_with()
        self.assertGreaterEqual(
            benchmark.device.screenshot_interval_set.call_count,
            1,
        )
        benchmark.device.screenshot_interval_set.assert_any_call(0.1)

    def test_runner_reports_safe_failure(self) -> None:
        with patch(
            "module.daemon.screenshot_interval_benchmark."
            "AutomatedScreenshotIntervalBenchmark.run",
            side_effect=ScreenshotIntervalBenchmarkError("fixture"),
        ):
            self.assertFalse(run_screenshot_interval_benchmark(object(), object()))


if __name__ == "__main__":
    unittest.main()
