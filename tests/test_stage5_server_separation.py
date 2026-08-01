from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from deploy.language_migration import migrate_deploy_language
from module.config.server import VALID_PACKAGE, VALID_SERVER_LIST, to_server
from module.config.locale import EVENT_NAME_FALLBACK_ORDER, EVENT_NAME_SOURCE, UI_LOCALE


class Stage5ServerSeparationTests(unittest.TestCase):
    def test_en_global_profile_is_independent_from_ui_locale(self) -> None:
        en_package = next(package for package, server in VALID_PACKAGE.items() if server == "en")
        en_server_name = "en-0"
        self.assertIn("en", VALID_SERVER_LIST)
        self.assertEqual(to_server(en_package), "en")

        source = f"""Deploy:\n  Webui:\n    Language: ja-JP\nProfile:\n  PackageName: {en_package}\n  ServerName: {en_server_name}\n  OcrModelVersionEnglish: azur_lane_v6_6\n  OcrLanguage: en\n  Event: campaign_main\n"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deploy.yaml"
            path.write_text(source, encoding="utf-8")
            before = yaml.safe_load(source)
            migrate_deploy_language(str(path))
            after = yaml.safe_load(path.read_text(encoding="utf-8"))

        self.assertEqual(after["Deploy"]["Webui"]["Language"], UI_LOCALE)
        self.assertEqual(after["Profile"], before["Profile"])
        self.assertEqual(to_server(after["Profile"]["PackageName"]), "en")

    def test_event_name_source_is_explicit_and_server_based(self) -> None:
        self.assertEqual(EVENT_NAME_SOURCE, "en")
        self.assertEqual(EVENT_NAME_FALLBACK_ORDER, ("en", "cn", "jp", "tw"))
        self.assertNotEqual(EVENT_NAME_SOURCE, UI_LOCALE)


if __name__ == "__main__":
    unittest.main()
