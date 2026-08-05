from pathlib import Path


def replace(path: str, old: str, new: str, *, count: int | None = None) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    occurrences = text.count(old)
    expected = 1 if count is None else count
    if occurrences != expected:
        raise SystemExit(
            f"{path}: expected {expected} occurrences of {old!r}, found {occurrences}"
        )
    target.write_text(text.replace(old, new), encoding="utf-8")


replace(
    "tests/test_commission_ocr_acceptance.py",
    '"dev_tools.commission_ocr_acceptance.COMMISSION_SWITCH.get"',
    '"tools.acceptance.ocr_commission.COMMISSION_SWITCH.get"',
)
replace(
    "tests/test_screenshot_interval_benchmark.py",
    '"dev_tools.screenshot_interval_benchmark._benchmark_interval"',
    '"tools.benchmarks.screenshot_intervals._benchmark_interval"',
)

for fixture in (
    "tests/fixtures/webui_traceback/fixture-light.html",
    "tests/fixtures/webui_traceback/fixture-dark.html",
):
    target = Path(fixture)
    text = target.read_text(encoding="utf-8")
    if "stage7-owned" not in text:
        raise SystemExit(f"{fixture}: missing legacy DOM marker")
    if "test_stage7_webui_traceback_rendering.py" not in text:
        raise SystemExit(f"{fixture}: missing legacy traceback path")
    target.write_text(
        text.replace("stage7-owned", "traceback-owned").replace(
            "test_stage7_webui_traceback_rendering.py",
            "test_webui_traceback_rendering.py",
        ),
        encoding="utf-8",
    )

migration_test = Path("tests/test_deploy_language_migration.py")
text = migration_test.read_text(encoding="utf-8")
old = '''    def test_cached_state_migrates_before_deploy_config_constructor(self) -> None:\n        from module.webui.setting import State\n\n        cache_name = "_deploy_config_"\n        missing = object()\n        previous = vars(State).get(cache_name, missing)\n        if previous is not missing:\n            delattr(State, cache_name)\n\n        events: list[str] = []\n        expected_config = object()\n\n        def migrate():\n            events.append("migration")\n            return SimpleNamespace(changed=False)\n\n        def construct():\n            events.append("constructor")\n            return expected_config\n\n        try:\n            with patch(\n                "deploy.language_migration.migrate_deploy_language",\n                side_effect=migrate,\n            ), patch(\n                "module.webui.config.DeployConfig",\n                side_effect=construct,\n            ):\n                self.assertIs(State.deploy_config, expected_config)\n                self.assertIs(State.deploy_config, expected_config)\n            self.assertEqual(events, ["migration", "constructor"])\n        finally:\n            if cache_name in vars(State):\n                delattr(State, cache_name)\n            if previous is not missing:\n                setattr(State, cache_name, previous)\n'''
new = '''    def test_cached_state_migrates_before_deploy_config_constructor(self) -> None:\n        from module.webui.setting import State\n\n        class FreshState(State):\n            pass\n\n        events: list[str] = []\n        expected_config = object()\n\n        def migrate():\n            events.append("migration")\n            return SimpleNamespace(changed=False)\n\n        def construct():\n            events.append("constructor")\n            return expected_config\n\n        with patch(\n            "deploy.language_migration.migrate_deploy_language",\n            side_effect=migrate,\n        ), patch(\n            "module.webui.config.DeployConfig",\n            side_effect=construct,\n        ):\n            self.assertIs(FreshState.deploy_config, expected_config)\n            self.assertIs(FreshState.deploy_config, expected_config)\n\n        self.assertEqual(events, ["migration", "constructor"])\n'''
if text.count(old) != 1:
    raise SystemExit("tests/test_deploy_language_migration.py: cache test block drifted")
migration_test.write_text(text.replace(old, new), encoding="utf-8")

replace(
    "tools/acceptance/device.py",
    'description="Безопасный real-device/emulator acceptance Stage 8A"',
    'description="Безопасная приёмка реального устройства или эмулятора"',
)
replace(
    "tools/acceptance/device.py",
    '"stage": "8A",',
    '"scope": "device",',
    count=2,
)
replace(
    "tools/acceptance/device.py",
    'print("Stage 8A device acceptance: PASS")',
    'print("Device acceptance: PASS")',
)
replace(
    "tools/acceptance/device.py",
    '"Stage 8A device acceptance: FAIL — "',
    '"Device acceptance: FAIL — "',
)
