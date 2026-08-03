from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from dev_tools.stage8a_device_log_audit import Stage8ADeviceLogAudit
from dev_tools.stage8a_semantic_policy import IMMUTABLE_STAGE8A_BASE_SHA
from dev_tools.verify_stage8a import _effective_base_ref


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


class Stage8ASemanticContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        _git(self.root, "init")
        _git(self.root, "config", "user.name", "Stage8A Test")
        _git(self.root, "config", "user.email", "stage8a@example.invalid")
        (self.root / "module/device").mkdir(parents=True)
        (self.root / "module/webui").mkdir(parents=True)
        (self.root / "module/webui/api.py").write_text("", encoding="utf-8")
        (self.root / "module/device/sample.py").write_text(
            "def run(serial):\n"
            "    logger.error(f'[设备 — ADB] 连接失败: {serial}')\n",
            encoding="utf-8",
        )
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-m", "base")
        self.base_sha = _git(self.root, "rev-parse", "HEAD")
        (self.root / "module/device/sample.py").write_text(
            "def run(serial):\n"
            "    logger.error(f'[Устройство — ADB] Не удалось подключиться: {serial}')\n",
            encoding="utf-8",
        )
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-m", "translation")
        self.head_sha = _git(self.root, "rev-parse", "HEAD")

    def tearDown(self):
        self.temp.cleanup()

    def test_pr_head_against_immutable_base_keeps_migration_visible(self):
        _, metrics = Stage8ADeviceLogAudit(self.root, self.base_sha).build()
        self.assertEqual(metrics["stage8a_translation_required_start"], 1)
        self.assertEqual(metrics["stage8a_translated"], 1)
        self.assertEqual(metrics["stage8a_control_flow_mismatches"], 0)

    def test_merged_stable_against_immutable_base_keeps_migration_visible(self):
        _git(self.root, "branch", "personal/stable", self.head_sha)
        _, metrics = Stage8ADeviceLogAudit(self.root, self.base_sha).build()
        self.assertEqual(metrics["stage8a_translated"], 1)

    def test_self_diff_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "self-diff"):
            Stage8ADeviceLogAudit(self.root, self.head_sha).build()

    def test_unknown_base_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "Не удалось разрешить"):
            Stage8ADeviceLogAudit(self.root, "missing-stage8a-base")

    def test_baseline_has_one_policy_source(self):
        self.assertRegex(IMMUTABLE_STAGE8A_BASE_SHA, r"^[0-9a-f]{40}$")
        self.assertEqual(_effective_base_ref(None), IMMUTABLE_STAGE8A_BASE_SHA)
        with self.assertRaisesRegex(RuntimeError, "baseline immutable"):
            _effective_base_ref("0" * 40)


if __name__ == "__main__":
    unittest.main()
