from __future__ import annotations

import unittest
from unittest.mock import patch

from tools.benchmarks.screenshot_intervals import (
    IntervalResult,
    _parse_intervals,
    _percentile,
    _recommend_profiles,
    _run_phase,
    _summarize_interval,
)
from tools.acceptance.device import AcceptanceFailure


def _result(phase: str, interval: float, stable: bool = True) -> IntervalResult:
    return IntervalResult(
        phase=phase,
        requested_interval_s=interval,
        target_fps=1 / interval,
        frames=10,
        wall_s=1.0,
        achieved_fps=10.0,
        target_achievement_ratio=1.0,
        interval_p50_ms=interval * 1000,
        interval_p95_ms=interval * 1000,
        interval_max_ms=interval * 1000,
        deadline_miss_ratio=0.0,
        process_cpu_percent=10.0,
        system_cpu_percent=20.0,
        rss_delta_mib=0.0,
        stable=stable,
        frame_contract={"color_contract": "BGR"},
    )


class ScreenshotIntervalBenchmarkTests(unittest.TestCase):
    def test_percentile_uses_linear_interpolation(self) -> None:
        self.assertEqual(_percentile([1.0, 2.0, 3.0], 0.5), 2.0)
        self.assertAlmostEqual(_percentile([1.0, 2.0], 0.95), 1.95)

    def test_parse_intervals_sorts_deduplicates_and_checks_bounds(self) -> None:
        self.assertEqual(
            _parse_intervals("0.1, 0.05, 0.1", (), (0.001, 0.3)),
            [0.05, 0.1],
        )
        with self.assertRaises(AcceptanceFailure):
            _parse_intervals("0", (), (0.001, 0.3))

    def test_summary_marks_stable_candidate(self) -> None:
        starts = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]
        ends = [0.01, 0.11, 0.21, 0.31, 0.41, 0.51]
        result = _summarize_interval(
            phase="normal",
            requested_interval_s=0.1,
            starts=starts,
            ends=ends,
            process_cpu_seconds=0.05,
            system_cpu_percent=15.0,
            rss_delta_bytes=0,
            frame_contract={"color_contract": "BGR"},
        )
        self.assertTrue(result.stable)
        self.assertAlmostEqual(result.interval_p95_ms, 100.0)
        self.assertEqual(result.deadline_miss_ratio, 0.0)

    def test_recommendation_uses_stable_candidates_and_balanced_floors(self) -> None:
        normal = [_result("normal", value) for value in (0.01, 0.05, 0.1, 0.2, 0.3)]
        combat = [
            _result("combat", value)
            for value in (0.02, 0.1, 0.15, 0.3, 0.5, 1.0)
        ]
        recommendation = _recommend_profiles(
            normal,
            combat,
            current_normal=0.1,
            current_combat=0.3,
        )
        self.assertEqual(recommendation["status"], "PASS")
        balanced = recommendation["profiles"]["balanced"]
        self.assertEqual(balanced["normal_s"], 0.05)
        self.assertEqual(balanced["combat_s"], 0.15)

    def test_recommendation_falls_back_when_no_stable_samples_exist(self) -> None:
        recommendation = _recommend_profiles(
            [_result("normal", 0.1, stable=False)],
            [_result("combat", 0.3, stable=False)],
            current_normal=0.1,
            current_combat=0.3,
        )
        self.assertEqual(recommendation["recommended_profile"], "current")

    def test_scrcpy_recommendation_uses_forced_backend_interval(self) -> None:
        recommendation = _recommend_profiles(
            [_result("normal", 0.1)],
            [_result("combat", 0.1)],
            current_normal=0.2,
            current_combat=0.5,
            forced_interval_s=0.1,
        )
        self.assertEqual(recommendation["status"], "FORCED_BY_BACKEND")
        self.assertEqual(recommendation["recommended_profile"], "backend_forced")
        forced = recommendation["profiles"]["backend_forced"]
        self.assertEqual(forced["normal_s"], 0.1)
        self.assertEqual(forced["combat_s"], 0.1)

    def test_run_phase_resets_task_stuck_guard_before_each_candidate(self) -> None:
        events: list[str] = []

        class Device:
            def stuck_record_clear(self) -> None:
                events.append("reset")

        def fake_benchmark(
            _device,
            *,
            phase: str,
            interval: float,
            duration: float,
            warmup_frames: int,
        ) -> IntervalResult:
            self.assertEqual(duration, 2.0)
            self.assertEqual(warmup_frames, 2)
            events.append(f"benchmark:{interval}")
            return _result(phase, interval)

        with patch(
            "dev_tools.screenshot_interval_benchmark._benchmark_interval",
            side_effect=fake_benchmark,
        ):
            results = _run_phase(
                Device(),
                phase="combat",
                intervals=[0.1, 0.3],
                duration=2.0,
                warmup_frames=2,
            )

        self.assertEqual(len(results), 2)
        self.assertEqual(
            events,
            ["reset", "benchmark:0.1", "reset", "benchmark:0.3"],
        )


if __name__ == "__main__":
    unittest.main()
