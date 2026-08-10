from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_STAGE_NUMBER_TOKEN = re.compile(r"(?:^|[_.-])stage[_-]?\d+(?:[_.-]|$)", re.IGNORECASE)
_TRACKED_GUARD_SUFFIXES = {".py", ".ps1", ".psm1"}
_TRACKED_GUARD_ROOTS = {"tests", "scripts"}


class RepositoryHygieneTests(unittest.TestCase):
    def test_tests_and_scripts_do_not_encode_roadmap_stage_numbers(self) -> None:
        completed = subprocess.run(
            ["git", "ls-files", "--", "tests", "scripts"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        offenders: list[str] = []

        for relative in completed.stdout.splitlines():
            path = Path(relative)
            if not path.parts or path.parts[0] not in _TRACKED_GUARD_ROOTS:
                continue
            if path.suffix.lower() not in _TRACKED_GUARD_SUFFIXES:
                continue
            if _STAGE_NUMBER_TOKEN.search(path.name):
                offenders.append(path.as_posix())

        self.assertEqual(
            offenders,
            [],
            "Отслеживаемые тесты и эксплуатационные скрипты не должны "
            "кодировать номер roadmap Stage в имени файла.",
        )

    def test_stage_token_guard_covers_dot_hyphen_and_underscore_boundaries(self) -> None:
        blocked = (
            "test.stage7.py",
            "test.stage-7.ps1",
            "test_stage7.py",
            "stage_7.psm1",
        )
        allowed = (
            "test_staging7.py",
            "test_stage.py",
            "test7stage.py",
        )

        for name in blocked:
            with self.subTest(name=name):
                self.assertIsNotNone(_STAGE_NUMBER_TOKEN.search(name))
        for name in allowed:
            with self.subTest(name=name):
                self.assertIsNone(_STAGE_NUMBER_TOKEN.search(name))


if __name__ == "__main__":
    unittest.main()
