"""Постоянные контракты структуры и запуска тестовой платформы."""

from __future__ import annotations

from tests.support.paths import FIXTURES_ROOT, REPOSITORY_ROOT, TESTS_ROOT


def test_test_modules_are_grouped_by_domain() -> None:
    assert not tuple(TESTS_ROOT.glob("test_*.py"))
    for relative_path in (
        "application",
        "contracts",
        "device",
        "event",
        "game",
        "infrastructure",
        "mcp",
        "notifications",
        "observability",
        "ocr",
        "operation_siren",
        "persistence",
        "platform",
        "runtime",
        "support",
        "webui",
    ):
        assert (TESTS_ROOT / relative_path).is_dir()


def test_shared_support_and_fixture_roots_are_stable() -> None:
    assert (TESTS_ROOT / "support").is_dir()
    assert (FIXTURES_ROOT / "event").is_dir()
    assert (FIXTURES_ROOT / "game").is_dir()
    assert (FIXTURES_ROOT / "operation_siren").is_dir()
    assert (FIXTURES_ROOT / "persistence").is_dir()
    assert (FIXTURES_ROOT / "webui").is_dir()


def test_acceptance_and_diagnostics_are_outside_pytest_tree() -> None:
    acceptance_root = REPOSITORY_ROOT / "tools" / "acceptance"
    diagnostics_root = REPOSITORY_ROOT / "tools" / "diagnostics"

    assert (acceptance_root / "emulator_recovery.py").is_file()
    assert (acceptance_root / "webui_traceback_browser.py").is_file()
    assert (diagnostics_root / "webui_traceback_server.py").is_file()
    assert not (TESTS_ROOT / "live_emulator_recovery_acceptance.py").exists()
    assert not (TESTS_ROOT / "run_webui_traceback_browser.py").exists()


def test_root_conftest_does_not_own_a_custom_scheduler() -> None:
    source = (TESTS_ROOT / "conftest.py").read_text(encoding="utf-8")

    assert "pytest_cmdline_main" not in source
    assert "AZURPILOT_PYTEST_PARALLEL" not in source
    assert "subprocess.Popen" not in source
