#!/usr/bin/env python3
from pathlib import Path

ROOT = Path.cwd()
SERVER = '"""Global/EN game-server and package contract."""\n\nserver = "en"\n\nVALID_SERVER = ("en",)\nGLOBAL_PACKAGE = "com.YoStarEN.AzurLane"\nVALID_PACKAGE = {\n    GLOBAL_PACKAGE: "en",\n}\nVALID_CHANNEL_PACKAGE = {}\nDICT_PACKAGE_TO_ACTIVITY = {\n    GLOBAL_PACKAGE: "com.manjuu.azurlane.PrePermissionActivity",\n}\nVALID_SERVER_LIST = {\n    "en": [\n        "Avrora",\n        "Lexington",\n        "Sandy",\n        "Washington",\n        "Amagi",\n        "Little Enterprise",\n    ],\n}\n\n\ndef to_server(package_or_server: str) -> str:\n    """Return EN only for an explicit supported server or package."""\n    if package_or_server == "en":\n        return "en"\n    if package_or_server == GLOBAL_PACKAGE:\n        return "en"\n    raise ValueError(f"Unsupported Global/EN package or server: {package_or_server}")\n\n\ndef to_package(package_or_server: str) -> str:\n    """Return the only supported Global package for an explicit EN value."""\n    if package_or_server in ("en", GLOBAL_PACKAGE):\n        return GLOBAL_PACKAGE\n    raise ValueError(f"Unsupported Global/EN package or server: {package_or_server}")\n\n\ndef set_server(package_or_server: str) -> None:\n    """Validate before changing global state or releasing resources."""\n    global server\n    validated = to_server(package_or_server)\n    server = validated\n\n    from module.base.resource import release_resources\n\n    release_resources()\n'
OOBE_TEST = 'from __future__ import annotations\n\nimport unittest\nfrom types import SimpleNamespace\nfrom unittest.mock import patch\n\nfrom module.config.server import GLOBAL_PACKAGE\nfrom module.device.connection import Connection\nfrom module.webui.oobe import OOBEWizard\n\n\nclass OobeGlobalEnSecurityTests(unittest.TestCase):\n    def test_known_package_detection_uses_exact_allowlist_membership(self) -> None:\n        connection = Connection.__new__(Connection)\n        connection.list_package = lambda show_log=True: [\n            GLOBAL_PACKAGE,\n            GLOBAL_PACKAGE.lower(),\n            f"prefix.{GLOBAL_PACKAGE}",\n            "com.YoStarJP.AzurLane",\n        ]\n        self.assertEqual(connection.list_known_packages(show_log=False), [GLOBAL_PACKAGE])\n\n    def test_legacy_auto_uses_detected_global_package_only(self) -> None:\n        connection = Connection.__new__(Connection)\n        connection.serial = "test-device"\n        connection.package = "auto"\n        connection.config = SimpleNamespace(Emulator_PackageName="auto")\n        connection.list_known_packages = lambda: [GLOBAL_PACKAGE]\n\n        with patch("module.device.connection.set_server") as set_server:\n            connection.detect_package(set_config=True)\n\n        self.assertEqual(connection.package, GLOBAL_PACKAGE)\n        self.assertEqual(connection.config.Emulator_PackageName, GLOBAL_PACKAGE)\n        set_server.assert_called_once_with(GLOBAL_PACKAGE)\n\n    def test_oobe_rejects_dom_tampered_package_before_config_write(self) -> None:\n        wizard = OOBEWizard.__new__(OOBEWizard)\n        wizard.package_name = "com.YoStarJP.AzurLane"\n        wizard.emulator_serial = "auto"\n        wizard._sync_emulator_pin_values = lambda: None\n        with self.assertRaises(ValueError):\n            wizard._collect_emulator_and_go(1)\n\n    def test_review_table_escapes_untrusted_values(self) -> None:\n        wizard = OOBEWizard.__new__(OOBEWizard)\n        wizard.server = "en"\n        wizard.config_name = "<script>alert(1)</script>"\n        wizard.emulator_serial = "auto"\n        wizard.package_name = GLOBAL_PACKAGE\n        wizard.server_name = "en-0"\n        wizard._render_footer = lambda *args, **kwargs: None\n        rendered = []\n        with patch("module.webui.oobe.put_html", side_effect=rendered.append), \\\n                patch("module.webui.oobe.lang.t", side_effect=lambda key: key):\n            wizard._step_review()\n        html = "".join(str(item) for item in rendered)\n        self.assertNotIn("<script>alert(1)</script>", html)\n        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)\n\n\nif __name__ == "__main__":\n    unittest.main()\n'

def repl(path, old, new):
    p = ROOT / path
    text = p.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise RuntimeError(f"{path} replacement count={text.count(old)}")
    p.write_text(text.replace(old, new), encoding="utf-8", newline="\n")

(ROOT / "module/config/server.py").write_text(SERVER, encoding="utf-8", newline="\n")
repl("tests/test_global_en_runtime.py",
     '        self.assertEqual(server.to_server("auto"), "en")\n        self.assertEqual(server.to_package("auto"), GLOBAL_PACKAGE)\n',
     '        with self.assertRaises(ValueError):\n            server.to_server("auto")\n        with self.assertRaises(ValueError):\n            server.to_package("auto")\n')
repl("module/webui/oobe.py", "import subprocess\nimport os\n", "import subprocess\nimport os\nfrom html import escape\n")
repl("module/webui/oobe.py",
     "from module.config.server import VALID_CHANNEL_PACKAGE, VALID_PACKAGE, VALID_SERVER_LIST, to_server\n",
     "from module.config.server import VALID_CHANNEL_PACKAGE, VALID_PACKAGE, VALID_SERVER_LIST, to_package, to_server\n")
repl("module/webui/oobe.py",
     '        put_input(\n            name="oobe_package",\n            label=lang.t("Gui.OOBE.EmulatorPackage"),\n            value=self.package_name,\n        )\n',
     '        put_select(\n            name="oobe_package",\n            label=lang.t("Gui.OOBE.EmulatorPackage"),\n            value=self.package_name,\n            options=[{"label": f\'{lang.t("Gui.OOBE.ServerEN")} ({self.package_name})\', "value": self.package_name}],\n        )\n')
repl("module/webui/oobe.py",
     '    def _collect_emulator_and_go(self, direction):\n        self._sync_emulator_pin_values()\n        if not self.emulator_serial:\n',
     '    def _collect_emulator_and_go(self, direction):\n        self._sync_emulator_pin_values()\n        self.package_name = to_package(self.package_name)\n        if not self.emulator_serial:\n')
repl("module/webui/oobe.py",
     '        if package:\n            self.package_name = package\n',
     '        if package:\n            self.package_name = to_package(str(package))\n')
repl("module/webui/oobe.py",
     '        rows = "".join(\n            f"<tr><td>{k}</td><td>{v}</td></tr>"\n            for k, v in items\n        )\n',
     '        rows = "".join(\n            f"<tr><td>{escape(str(k))}</td><td>{escape(str(v))}</td></tr>"\n            for k, v in items\n        )\n')
repl("module/webui/oobe.py",
     '        try:\n            self._sync_emulator_pin_values()\n            config = load_config("template")\n',
     '        try:\n            self._sync_emulator_pin_values()\n            self.package_name = to_package(self.package_name)\n            config = load_config("template")\n')
(ROOT / "tests/test_oobe_global_en_security.py").write_text(OOBE_TEST, encoding="utf-8", newline="\n")
repl("README.md",
     "Старые locale-файлы и server-specific assets остаются в репозитории как неактивные данные до отдельной проверки и контролируемого удаления. Игровой сервер, package name, OCR-профиль и английский источник названий событий не зависят от языка WebUI.",
     "Игровой контур поддерживает только Global/EN: пакет `com.YoStarEN.AzurLane`, сервер `en` и канонический каталог `assets/en`. Runtime WebUI использует только `ru-RU`; `en-US.json` сохранён исключительно как build-time источник ключей и placeholders. Названия событий берутся из EN metadata без CN/JP/TW fallback. Все 18 OCR-файлов сохранены как Global/shared ресурсы; foreign OCR aliases недоступны. Неизвестный или foreign package/server отклоняется до device/game side effects.")

def append(path, marker, section):
    p = ROOT / path
    text = p.read_text(encoding="utf-8")
    if marker not in text:
        p.write_text(text.rstrip() + "\n\n" + section.strip() + "\n", encoding="utf-8", newline="\n")

append(".codex/context/03-CONFIG-I18N.md", "## Global/EN product boundary", """
## Global/EN product boundary
- server — только `en`; package — только `com.YoStarEN.AzurLane`;
- legacy `auto` — sentinel device detection и допустим только после exact-match Global package;
- foreign/unknown package или server отклоняется до device/game side effects;
- runtime WebUI — `ru-RU`; `en-US.json` — только build-time key/placeholder parity;
- `ja-JP`, `zh-CN`, `zh-MIAO`, `zh-TW` не runtime-selectable;
- event metadata source — `en`, foreign fallback order пуст.
""")
append(".codex/context/04-DEVICE-UI-OCR.md", "## Global/EN asset и OCR contract", """
## Global/EN asset и OCR contract
- canonical root — `assets/en`; CN/JP/TW roots и string fallback недопустимы;
- generator читает module list из EN и fail-closed при missing asset;
- package detection использует exact match `com.YoStarEN.AzurLane`;
- 18 OCR files — Global recognition либо shared detection/generic resources;
- registry exposes `azur_lane`; `cnocr`, JP и TW aliases отклоняются;
- shared `det`, English routing, RPC allowlist, recovery и privacy controls сохраняются.
""")
