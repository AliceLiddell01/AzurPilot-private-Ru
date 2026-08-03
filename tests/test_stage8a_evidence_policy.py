from __future__ import annotations

import importlib
import unittest

from dev_tools.stage8a_evidence_policy import (
    BACKEND_CI_COVERAGE,
    EXTERNAL_CONTRACTS,
    SCENARIO_REQUIREMENTS,
    SECURITY_REQUIREMENTS,
    scenario_evidence,
)


class Stage8AEvidencePolicyTests(unittest.TestCase):
    def test_every_required_scenario_has_machine_readable_evidence(self):
        rows = scenario_evidence()
        expected = {
            (category, scenario)
            for category, scenarios in SCENARIO_REQUIREMENTS.items()
            for scenario in scenarios
        }
        actual = {(row["category"], row["scenario"]) for row in rows}
        self.assertEqual(actual, expected)
        self.assertTrue(all(row["fixture_test"] for row in rows))
        self.assertTrue(all(row["evidence_level"] == "CI_FIXTURE" for row in rows))
        self.assertEqual(
            len({row["fixture_test"] for row in rows}),
            len(rows),
            "Every scenario must point to a scenario-specific executable fixture test.",
        )
        self.assertTrue(all(row["limitations"] for row in rows))

    def test_all_referenced_tests_exist(self):
        test_ids = {
            row["semantic_test"]
            for row in scenario_evidence()
            if row["semantic_test"]
        } | {
            row["fixture_test"]
            for row in scenario_evidence()
        } | {row["test"] for row in SECURITY_REQUIREMENTS} | {
            row["test"] for row in EXTERNAL_CONTRACTS
        }
        for test_id in sorted(test_ids):
            with self.subTest(test_id=test_id):
                module_name, class_name, method_name = test_id.rsplit(".", 2)
                module = importlib.import_module(module_name)
                test_class = getattr(module, class_name)
                self.assertTrue(callable(getattr(test_class, method_name)))

    def test_backend_coverage_separates_ci_from_external_acceptance(self):
        for row in BACKEND_CI_COVERAGE:
            self.assertIn(row["ci_level"], {"CI_FIXTURE", "SEMANTIC_CONTRACT"})
            self.assertNotIn("actual_user_backend", row)
            self.assertNotIn("REAL_ACCEPTANCE", row["ci_level"])
            self.assertTrue(row["limitations"])

    def test_security_review_checklist_is_complete(self):
        expected = {
            "command_injection", "shell_quoting", "serial_injection",
            "unsafe_subprocess_logging", "ssh_credential_leakage",
            "raw_url_credentials", "device_serial_leakage", "clipboard_leakage",
            "typed_text_leakage", "screenshot_leakage", "binary_log_flooding",
            "html_websocket_injection", "ansi_control_chars", "newline_log_forging",
            "unbounded_external_output", "exception_local_leakage", "temporary_paths",
            "port_exposure", "live_preview_authorization",
        }
        self.assertEqual({row["id"] for row in SECURITY_REQUIREMENTS}, expected)

    def test_external_contracts_cover_all_pinned_integrations(self):
        self.assertEqual(
            {row["dependency"] for row in EXTERNAL_CONTRACTS},
            {"adbutils", "uiautomator2", "scrcpy-server"},
        )


if __name__ == "__main__":
    unittest.main()
