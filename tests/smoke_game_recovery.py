#!/usr/bin/env python3
"""Детерминированная smoke-проверка Stage 1 для восстановления только игры.

Проверка переиспользует существующие контрактные тесты вместо production debug-переключателей.
Она не требует реального эмулятора и не должна им управлять.
"""

import importlib.util
import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEST_FILE = Path(__file__).with_name('test_alas_error_handling.py')
TEST_METHODS = (
    'test_stuck_restarts_only_game_and_reports_recoverable_after_health_success',
    'test_failed_game_restart_is_not_masked_as_recoverable',
    'test_threshold_stops_recovery_loop_without_touching_emulator',
)


def load_test_case():
    """Загрузить TestGameStuckRecovery напрямую из соседнего тестового файла."""
    if str(REPOSITORY_ROOT) not in sys.path:
        sys.path.insert(0, str(REPOSITORY_ROOT))

    spec = importlib.util.spec_from_file_location(
        '_stage1_game_recovery_contract_tests',
        TEST_FILE,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f'Не удалось загрузить тестовый модуль: {TEST_FILE}')

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TestGameStuckRecovery


def main() -> int:
    test_case = load_test_case()
    suite = unittest.TestSuite(test_case(method) for method in TEST_METHODS)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
