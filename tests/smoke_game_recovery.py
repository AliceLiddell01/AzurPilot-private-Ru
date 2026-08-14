#!/usr/bin/env python3
"""Deterministic Stage 1 fault-injection smoke for game-only recovery.

This intentionally reuses the contract tests instead of adding production fault toggles.
It must never require or manipulate a real emulator.
"""

import unittest


TEST_NAMES = (
    'tests.test_alas_error_handling.TestGameStuckRecovery.'
    'test_stuck_restarts_only_game_and_reports_recoverable_after_health_success',
    'tests.test_alas_error_handling.TestGameStuckRecovery.'
    'test_failed_game_restart_is_not_masked_as_recoverable',
    'tests.test_alas_error_handling.TestGameStuckRecovery.'
    'test_threshold_stops_recovery_loop_without_touching_emulator',
)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(name) for name in TEST_NAMES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
