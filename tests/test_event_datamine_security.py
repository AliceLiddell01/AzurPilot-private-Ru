import tempfile
import unittest
from pathlib import Path

from module.event_datamine.artifact import build_artifact, validate_artifact
from module.event_datamine.source import ShareCfgLoader, SourceSnapshot


ROOT = Path(__file__).resolve().parents[1]
REVISION = "f44b48853d48b400b92738b1f1cf6fcdf1d69169"


class EventDatamineSecurityTests(unittest.TestCase):
    def test_lua_source_is_parsed_without_executing_embedded_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "EN" / "sharecfg"
            folder.mkdir(parents=True)
            marker = root / "executed"
            (folder / "activity_template.lua").write_text(
                "pg.base.activity_template[7] = { id = 7 }\n"
                f'os.execute("touch {marker}")\n',
                encoding="utf-8",
            )
            snapshot = SourceSnapshot(
                root,
                "EN",
                "AzurLaneTools/AzurLaneLuaScripts",
                REVISION,
            )

            self.assertEqual(
                ShareCfgLoader(snapshot).load_table("activity_template")[7]["id"],
                7,
            )
            self.assertFalse(marker.exists())

    def test_artifact_digest_rejects_source_fact_tampering(self):
        artifact = build_artifact({"id": "en:1", "shop_items": []})
        artifact["event_spec"]["id"] = "en:2"
        with self.assertRaisesRegex(ValueError, "Digest"):
            validate_artifact(artifact)

    def test_runtime_bwiki_provider_is_absent(self):
        self.assertFalse((ROOT / "module/webui/event_calculator.py").exists())
        sources = "\n".join(
            (ROOT / path).read_text(encoding="utf-8")
            for path in (
                "module/webui/app_event_layout.py",
                "module/webui/app_event_planner.py",
                "module/webui/app_event_tools.py",
            )
        )
        self.assertNotIn("BWiki", sources)
        self.assertNotIn("load_event_calculator", sources)


if __name__ == "__main__":
    unittest.main()
