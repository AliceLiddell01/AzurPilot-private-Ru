#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

from global_en_shared import SHARED

ROOT = Path(__file__).resolve().parents[1]
IMPL = ROOT / "tools/global_en_migrate_impl.py"


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise RuntimeError(f"Migration target drifted: {path}")
    target.write_text(text.replace(old, new), encoding="utf-8")


replace_once("module/config/server.py", '    if package_or_server == GLOBAL_PACKAGE:\n        return "en"\n', '    if package_or_server in (GLOBAL_PACKAGE, "auto"):\n        return "en"\n')
replace_once("module/config/server.py", '    if package_or_server in ("en", GLOBAL_PACKAGE):\n        return GLOBAL_PACKAGE\n', '    if package_or_server in ("en", GLOBAL_PACKAGE, "auto"):\n        return GLOBAL_PACKAGE\n')
replace_once("module/config/locale.py", 'EVENT_NAME_FALLBACK_ORDER = ("en",)\n', 'EVENT_NAME_FALLBACK_ORDER = ()\n')
replace_once("tests/test_global_en_runtime.py", '        self.assertEqual(server.to_package(GLOBAL_PACKAGE), GLOBAL_PACKAGE)\n', '        self.assertEqual(server.to_package(GLOBAL_PACKAGE), GLOBAL_PACKAGE)\n        self.assertEqual(server.to_server("auto"), "en")\n        self.assertEqual(server.to_package("auto"), GLOBAL_PACKAGE)\n')
replace_once("tests/test_global_en_assets.py", 'ASSET_PATTERN = re.compile(r"(?:\\./)?assets/(?P<server>en|cn|jp|tw)/[^\'\\"\\s]+")\n', 'ASSET_PATTERN = re.compile(r"(?:\\./)?assets/(?P<server>en|cn|jp|tw)/[^\'\\"]+")\n')

(ROOT / "tests/test_global_en_metadata.py").write_text(
    'from __future__ import annotations\n\nimport unittest\nfrom pathlib import Path\n\nfrom module.config.locale import (\n    BUILD_TIME_LOCALES,\n    EVENT_NAME_FALLBACK_ORDER,\n    EVENT_NAME_SOURCE,\n    UI_LOCALE,\n)\n\nROOT = Path(__file__).resolve().parents[1]\n\n\nclass GlobalEnMetadataTests(unittest.TestCase):\n    def test_runtime_and_build_time_locale_roles(self) -> None:\n        self.assertEqual(UI_LOCALE, "ru-RU")\n        self.assertEqual(BUILD_TIME_LOCALES, ("en-US",))\n        self.assertTrue((ROOT / "module/config/i18n/ru-RU.json").is_file())\n        self.assertTrue((ROOT / "module/config/i18n/en-US.json").is_file())\n        for locale in ("ja-JP", "zh-CN", "zh-MIAO", "zh-TW"):\n            self.assertFalse((ROOT / f"module/config/i18n/{locale}.json").exists())\n\n    def test_event_metadata_has_no_foreign_fallback(self) -> None:\n        self.assertEqual(EVENT_NAME_SOURCE, "en")\n        self.assertEqual(EVENT_NAME_FALLBACK_ORDER, ())\n\n\nif __name__ == "__main__":\n    unittest.main()\n',
    encoding="utf-8",
)

source = IMPL.read_text(encoding="utf-8")
start = source.index("SHARED=(")
end = source.index("\nFOREIGN_ROOTS=", start)
source = source[:start] + f"SHARED={SHARED!r}" + source[end:]

old_validation = '    pattern=re.compile(r"(?:\\./)?assets/(en|cn|jp|tw)/([^\'\\"\\s]+)")\n    for rel in generated:\n        path=ROOT/rel; source=path.read_text(encoding="utf-8")\n        for node in ast.walk(ast.parse(source,str(path))):\n            if isinstance(node,ast.Dict):\n                keys={k.value for k in node.keys if isinstance(k,ast.Constant) and isinstance(k.value,str)}\n                if keys & {"cn","en","jp","tw"}: raise RuntimeError(f"server dict: {path}")\n        for m in pattern.finditer(source):\n            if m.group(1)!="en": raise RuntimeError(f"foreign path: {path} {m.group(0)}")\n            if not (ROOT/"assets/en"/m.group(2)).is_file(): raise FileNotFoundError(m.group(2))\n'
new_validation = '    for rel in generated:\n        path=ROOT/rel; source=path.read_text(encoding="utf-8")\n        tree=ast.parse(source,str(path))\n        for node in ast.walk(tree):\n            if isinstance(node,ast.Dict):\n                keys={k.value for k in node.keys if isinstance(k,ast.Constant) and isinstance(k.value,str)}\n                if keys & {"cn","en","jp","tw"}: raise RuntimeError(f"server dict: {path}")\n            if not isinstance(node,ast.Call): continue\n            for keyword in node.keywords:\n                if keyword.arg!="file": continue\n                value=literal(keyword.value)\n                if not value: continue\n                match=re.fullmatch(r"(?:\\./)?assets/(en|cn|jp|tw)/(.+)",value)\n                if not match: continue\n                if match.group(1)!="en": raise RuntimeError(f"foreign path: {path} {value}")\n                if not (ROOT/"assets/en"/match.group(2)).is_file(): raise FileNotFoundError(match.group(2))\n'
if source.count(old_validation) != 1:
    raise RuntimeError("Migration verifier contract drifted")
source = source.replace(old_validation, new_validation)

old_cleanup = '    (ROOT/".github/workflows/global-en-migration.yml").unlink()\n    Path(__file__).unlink()\n    run("git","diff","--check")'
new_cleanup = '    for relative in (\n        ".github/workflows/global-en-migration.yml",\n        "tools/global_en_migrate.py",\n        "tools/global_en_migrate_impl.py",\n        "tools/global_en_shared.py",\n    ):\n        (ROOT / relative).unlink()\n    run("git","diff","--check")'
if source.count(old_cleanup) != 1:
    raise RuntimeError("Migration implementation cleanup contract drifted")
source = source.replace(old_cleanup, new_cleanup)

namespace = {
    "__name__": "__main__",
    "__file__": str(IMPL),
}
exec(compile(source, str(IMPL), "exec"), namespace)
