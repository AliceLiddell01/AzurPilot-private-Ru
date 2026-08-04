from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASK = "ScreenshotIntervalBenchmark"


class ScreenshotIntervalBenchmarkWebUiTests(unittest.TestCase):
    def test_task_is_registered_in_source_and_generated_menu(self) -> None:
        source = (ROOT / "module/config/argument/task.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"    {TASK}:\n", source)

        menu = json.loads(
            (ROOT / "module/config/argument/menu.json").read_text(encoding="utf-8")
        )
        self.assertIn(TASK, menu["Tool"]["tasks"])

        args = json.loads(
            (ROOT / "module/config/argument/args.json").read_text(encoding="utf-8")
        )
        self.assertIn(TASK, args)
        self.assertIn("Storage", args[TASK])

    def test_dispatch_method_calls_automated_task(self) -> None:
        source = (ROOT / "alas.py").read_text(encoding="utf-8")
        self.assertIn("def screenshot_interval_benchmark(self):", source)
        self.assertIn("run_screenshot_interval_benchmark", source)

    def test_russian_labels_explain_interval_direction(self) -> None:
        locale = json.loads(
            (ROOT / "module/config/i18n/ru-RU.json").read_text(encoding="utf-8")
        )
        task = locale["Task"][TASK]
        self.assertEqual(task["name"], "Тест интервалов снимков экрана")
        self.assertIn("Battle Simulation", task["help"])

        optimization = locale["Optimization"]
        normal = optimization["ScreenshotInterval"]
        combat = optimization["CombatScreenshotInterval"]
        self.assertEqual(normal["name"], "Интервал между снимками экрана, с")
        self.assertEqual(
            combat["name"],
            "Интервал между снимками экрана в бою, с",
        )
        self.assertIn("чем меньше значение", normal["help"])
        self.assertIn("тем выше нагрузка", normal["help"])
        self.assertIn("чем меньше значение", combat["help"])
        self.assertIn("тем выше нагрузка", combat["help"])

    def test_automation_is_fail_closed_for_meta_attack(self) -> None:
        source = (
            ROOT / "module/daemon/screenshot_interval_benchmark.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"SIMULATION" not in compact', source)
        self.assertIn("ordinary_meta_attack_allowed", source)
        self.assertNotIn("META_MAIN_DOSSIER_ENTRANCE", source)


if __name__ == "__main__":
    unittest.main()
