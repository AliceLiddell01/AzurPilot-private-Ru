from __future__ import annotations

import unittest
from pathlib import Path

from module.config.locale import (
    BUILD_TIME_LOCALES,
    EVENT_NAME_FALLBACK_ORDER,
    EVENT_NAME_SOURCE,
    UI_LOCALE,
)

ROOT = Path(__file__).resolve().parents[1]


class GlobalEnMetadataTests(unittest.TestCase):
    def test_runtime_and_build_time_locale_roles(self) -> None:
        self.assertEqual(UI_LOCALE, "ru-RU")
        self.assertEqual(BUILD_TIME_LOCALES, ("en-US",))
        self.assertTrue((ROOT / "module/config/i18n/ru-RU.json").is_file())
        self.assertTrue((ROOT / "module/config/i18n/en-US.json").is_file())
        for locale in ("ja-JP", "zh-CN", "zh-MIAO", "zh-TW"):
            self.assertFalse((ROOT / f"module/config/i18n/{locale}.json").exists())

    def test_event_metadata_has_no_foreign_fallback(self) -> None:
        self.assertEqual(EVENT_NAME_SOURCE, "en")
        self.assertEqual(EVENT_NAME_FALLBACK_ORDER, ())


if __name__ == "__main__":
    unittest.main()
