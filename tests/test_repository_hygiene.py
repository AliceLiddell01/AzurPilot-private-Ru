from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_STAGE_NUMBER_TOKEN = re.compile(r"(?:^|[_-])stage[_-]?\d+(?:[_.-]|$)", re.IGNORECASE)


class RepositoryHygieneTests(unittest.TestCase):
    def test_tests_and_scripts_do_not_encode_roadmap_stage_numbers(self) -> None:
        offenders: list[str] = []

        for directory, suffixes in (
            (ROOT / "tests", {".py"}),
            (ROOT / "scripts", {".ps1", ".psm1"}),
        ):
            if not directory.exists():
                continue
            for path in directory.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in suffixes:
                    continue
                relative = path.relative_to(ROOT).as_posix()
                if _STAGE_NUMBER_TOKEN.search(path.name):
                    offenders.append(relative)

        self.assertEqual(
            offenders,
            [],
            "Тесты и эксплуатационные скрипты не должны кодировать номер roadmap Stage в имени файла.",
        )


if __name__ == "__main__":
    unittest.main()
