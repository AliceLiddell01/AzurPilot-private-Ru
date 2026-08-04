from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path

from dev_tools.stage7_log_audit import Stage7LogAudit
from dev_tools.stage7_semantic_policy import (
    POLICY_GROUPS,
    POLICY_SCOPE_SHA256,
    apply_stage7_policy,
)
from dev_tools.stage8a_device_log_audit import Stage8ADeviceLogAudit
from dev_tools.stage8a_semantic_policy import IMMUTABLE_STAGE8A_BASE_SHA


ROOT = Path(__file__).resolve().parents[1]
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _stage8a_webui_api_ids() -> set[str]:
    result: set[str] = set()
    for group in POLICY_GROUPS:
        if group["classification"] != "stage8a_device":
            continue
        result.update(group["points"].get("module/webui/api.py", ()))
    return result


def _stage8a_policy_point_paths() -> set[str]:
    return {
        path
        for group in POLICY_GROUPS
        if group["classification"] == "stage8a_device"
        for path in group["points"]
    }


class Stage8AStage7PolicyBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.stage7_audit = Stage7LogAudit(ROOT)
        cls.stage7_outputs, cls.stage7_metrics = cls.stage7_audit.build()
        stage7_table = json.loads(cls.stage7_outputs["scope.json"])
        cls.stage7_rows = [
            dict(zip(stage7_table["columns"], row, strict=True))
            for row in stage7_table["entries"]
        ]

        cls.stage8a_audit = Stage8ADeviceLogAudit(
            ROOT,
            base_ref=IMMUTABLE_STAGE8A_BASE_SHA,
        )
        cls.stage8a_outputs, cls.stage8a_metrics = cls.stage8a_audit.build()
        stage8a_table = json.loads(cls.stage8a_outputs["scope.json"])
        cls.stage8a_rows = [
            dict(zip(stage8a_table["columns"], row, strict=True))
            for row in stage8a_table["entries"]
        ]

    def test_stage7_policy_accepts_stage8a_translated_transfer_points(self) -> None:
        _, metrics, errors = apply_stage7_policy(
            self.stage7_outputs,
            self.stage7_metrics,
        )
        self.assertEqual(errors, [])
        self.assertEqual(metrics["stage7_policy_digest"], POLICY_SCOPE_SHA256)
        self.assertEqual(metrics["stage7_unresolved"], 0)
        self.assertEqual(metrics["stage7_unknown_classifications"], 0)

    def test_stage8a_webui_api_owner_resolution_is_explicit(self) -> None:
        expected_ids = _stage8a_webui_api_ids()
        self.assertTrue(expected_ids)
        stage7_rows = {
            row["stable_identifier"]: row
            for row in self.stage7_rows
            if row["path"] == "module/webui/api.py"
            and row["stable_identifier"] in expected_ids
        }
        self.assertEqual(set(stage7_rows), expected_ids)

        webui_rows = [
            row
            for row in self.stage8a_rows
            if row["path"] == "module/webui/api.py"
        ]
        self.assertTrue(webui_rows)
        allowed_transfers = {"stage8c"}
        allowed_technical = {"raw_external_payload", "technical_identifier"}
        for row in webui_rows:
            with self.subTest(
                identifier=row["stable_identifier"],
                owner=row["stage_owner"],
                classification=row["classification"],
            ):
                self.assertFalse(row["translation_required"])
                if row["stage_owner"] == "stage8a":
                    if row["classification"] in allowed_technical:
                        self.assertTrue(row["evidence"].strip())
                    else:
                        self.assertRegex(row["message_or_template"], CYRILLIC_RE)
                else:
                    self.assertIn(row["stage_owner"], allowed_transfers)
                    self.assertTrue(row["runtime_owner"].startswith("Stage 8"))
                    self.assertTrue(row["evidence"].strip())

    def test_shared_statistics_endpoints_are_transferred_to_stage8c(self) -> None:
        expected_functions = {"api_cl1_stats", "api_ap_timeline"}
        rows = {
            row["function_owner"]: row
            for row in self.stage8a_rows
            if row["path"] == "module/webui/api.py"
            and row["function_owner"] in expected_functions
        }
        self.assertEqual(set(rows), expected_functions)
        for function_owner, row in rows.items():
            with self.subTest(function_owner=function_owner):
                self.assertEqual(row["stage_owner"], "stage8c")
                self.assertEqual(row["classification"], "stage8c_scheduler")
                self.assertFalse(row["translation_required"])

    def test_scrcpy_server_output_is_reviewed_raw_external_payload(self) -> None:
        rows = [
            row
            for row in self.stage8a_rows
            if row["path"] == "module/webui/api.py"
            and row["function_owner"] == "LiveScrcpySession.start"
            and row["classification"] == "raw_external_payload"
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stage_owner"], "stage8a")
        self.assertFalse(rows[0]["translation_required"])
        self.assertTrue(rows[0]["evidence"].strip())

    def test_policy_digest_review_is_limited_to_stage8a_runtime_owner(self) -> None:
        # This is a migration invariant, not a current-PR delta.  Comparing a push
        # checkout with origin/personal/stable is a self-diff after the branch ref has
        # advanced and therefore hides the reviewed Stage 7 → Stage 8A policy drift.
        # Always compare the immutable Stage 8A baseline with the checked-out tree.
        _git("rev-parse", "--verify", IMMUTABLE_STAGE8A_BASE_SHA)
        changed = set(
            filter(
                None,
                _git(
                    "diff",
                    "--name-only",
                    f"{IMMUTABLE_STAGE8A_BASE_SHA}..HEAD",
                ).splitlines(),
            )
        )
        self.assertEqual(
            changed & _stage8a_policy_point_paths(),
            {"module/webui/api.py"},
        )


if __name__ == "__main__":
    unittest.main()
