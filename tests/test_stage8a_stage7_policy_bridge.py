from __future__ import annotations

import json
import os
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


def _policy_point_paths() -> set[str]:
    return {
        path
        for group in POLICY_GROUPS
        for path in group["points"]
    }


class Stage8AStage7PolicyBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.audit = Stage7LogAudit(ROOT)
        cls.outputs, cls.metrics = cls.audit.build()
        table = json.loads(cls.outputs["scope.json"])
        cls.rows = [
            dict(zip(table["columns"], row, strict=True))
            for row in table["entries"]
        ]

    def test_stage7_policy_accepts_stage8a_translated_transfer_points(self) -> None:
        _, metrics, errors = apply_stage7_policy(self.outputs, self.metrics)
        self.assertEqual(errors, [])
        self.assertEqual(metrics["stage7_policy_digest"], POLICY_SCOPE_SHA256)
        self.assertEqual(metrics["stage7_unresolved"], 0)
        self.assertEqual(metrics["stage7_unknown_classifications"], 0)

    def test_stage8a_webui_api_policy_points_are_russian(self) -> None:
        expected_ids = _stage8a_webui_api_ids()
        self.assertTrue(expected_ids)
        rows = {
            row["stable_identifier"]: row
            for row in self.rows
            if row["path"] == "module/webui/api.py"
            and row["stable_identifier"] in expected_ids
        }
        self.assertEqual(set(rows), expected_ids)
        for identifier, row in rows.items():
            with self.subTest(identifier=identifier):
                self.assertRegex(row["message_or_template"], CYRILLIC_RE)

    def test_policy_digest_review_is_limited_to_stage8a_runtime_owner(self) -> None:
        base_ref = (
            f"origin/{os.environ['GITHUB_BASE_REF']}"
            if os.environ.get("GITHUB_BASE_REF")
            else "origin/personal/stable"
        )
        try:
            _git("rev-parse", "--verify", base_ref)
        except subprocess.CalledProcessError:
            base_ref = IMMUTABLE_STAGE8A_BASE_SHA
        changed = set(
            filter(None, _git("diff", "--name-only", f"{base_ref}..HEAD").splitlines())
        )
        self.assertEqual(
            changed & _policy_point_paths(),
            {"module/webui/api.py"},
        )


if __name__ == "__main__":
    unittest.main()
