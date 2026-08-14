"""Standalone synthetic smoke проверяемого Stage 2 emulator recovery."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_test_module(name: str, relative_path: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'Не удалось загрузить smoke-модуль: {path}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def build_suite() -> unittest.TestSuite:
    transport = load_test_module(
        '_stage2_transport_tests',
        'tests/test_emulator_recovery_transport.py',
    )
    process = load_test_module(
        '_stage2_process_tests',
        'tests/test_mumu_process_control.py',
    )
    scheduler = load_test_module(
        '_stage2_scheduler_tests',
        'tests/test_alas_error_handling.py',
    )

    suite = unittest.TestSuite()
    selected = (
        (transport.EmulatorRecoveryTransportTests, 'test_graceful_success_skips_hard_kill'),
        (transport.EmulatorRecoveryTransportTests, 'test_still_alive_after_graceful_uses_hard_kill_before_start'),
        (transport.EmulatorRecoveryTransportTests, 'test_hard_kill_failure_blocks_cold_start'),
        (transport.EmulatorRecoveryTransportTests, 'test_start_false_is_failure_and_fresh_device_is_not_created'),
        (process.MuMuProcessControlTests, 'test_wrong_instance_and_shared_processes_are_never_killed'),
        (process.MuMuProcessControlTests, 'test_access_denied_is_failure_not_success'),
        (scheduler.TestGameStuckRecovery, 'test_stuck_restarts_only_game_and_reports_recoverable_after_health_success'),
        (scheduler.TestGameStuckRecovery, 'test_failed_game_restart_escalates_once_when_policy_enabled'),
        (scheduler.TestGameStuckRecovery, 'test_post_emulator_game_health_failure_does_not_recurse'),
        (scheduler.TestGameStuckRecovery, 'test_sensitive_task_never_attempts_game_or_emulator_recovery'),
    )
    for case, method in selected:
        suite.addTest(case(method))
    return suite


def main() -> int:
    result = unittest.TextTestRunner(verbosity=2).run(build_suite())
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
