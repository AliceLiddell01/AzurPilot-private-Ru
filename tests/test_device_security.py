from __future__ import annotations

import ast
import asyncio
import json
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _function_source(path: str, name: str) -> str:
    source = _source(path)
    tree = ast.parse(source, filename=path)
    lines = source.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "\n".join(lines[node.lineno - 1 : node.end_lineno])
    raise AssertionError(f"{name} not found in {path}")


def _load_live_guard_functions():
    source = _source("module/webui/api.py")
    tree = ast.parse(source, filename="module/webui/api.py")
    wanted = {
        "_websocket_client_host",
        "_is_local_live_websocket",
        "_reject_nonlocal_live_websocket",
    }
    selected = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in wanted
    ]
    namespace = {"socket": __import__("socket"), "json": json}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "<live-guard>", "exec"), namespace)
    return namespace


class _FakeWebSocket:
    def __init__(self, host: str):
        self.client = types.SimpleNamespace(host=host)
        self.accepted = False
        self.messages: list[str] = []
        self.close_code = None

    async def accept(self):
        self.accepted = True

    async def send_text(self, message: str):
        self.messages.append(message)

    async def close(self, code=1000):
        self.close_code = code


class DeviceSecurityTests(unittest.TestCase):
    def test_gacha_ui_has_no_module_level_debug_runner(self):
        source = _source("module/gacha/ui.py")
        tree = ast.parse(source, filename="module/gacha/ui.py")
        debug_runners = []
        for node in tree.body:
            if not isinstance(node, ast.If):
                continue
            test = node.test
            is_main_guard = (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq)
                and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value == "__main__"
            )
            if is_main_guard:
                debug_runners.append(node.lineno)
        self.assertEqual(debug_runners, [])

    def test_subprocess_calls_do_not_enable_shell(self):
        source = _source("tools/acceptance/device.py")
        tree = ast.parse(source)
        findings = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = ""
            if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                name = f"{node.func.value.id}.{node.func.attr}"
            if name not in {"subprocess.run", "subprocess.Popen"}:
                continue
            for keyword in node.keywords:
                if keyword.arg == "shell" and not (
                    isinstance(keyword.value, ast.Constant) and keyword.value.value is False
                ):
                    findings.append(node.lineno)
        self.assertEqual(findings, [])

    def test_acceptance_forbids_clipboard_and_user_text(self):
        source = _source("tools/acceptance/device.py")
        for token in ('"clipboard_read"', '"user_text_input"', '"adb_kill_server"'):
            self.assertIn(token, source)
        self.assertNotIn("get_clipboard(", source)
        self.assertNotIn("set_clipboard(", source)

    def test_external_diagnostics_are_sanitized_before_report(self):
        main = _function_source("tools/acceptance/device.py", "main")
        self.assertIn("_safe_text(error_text, resolved_serial)", main)
        self.assertIn("json.dumps(report", main)

    def test_temporary_screenshot_is_deleted(self):
        run = _function_source("tools/acceptance/device.py", "run_acceptance")
        self.assertGreaterEqual(run.count("temp_path.unlink(missing_ok=True)"), 2)
        self.assertIn("finally:", run)

    def test_forwarding_remains_target_scoped(self):
        run_adb = _function_source("tools/acceptance/device.py", "_run_adb")
        self.assertIn('[adb, "-s", serial, *args]', run_adb)
        close_probe = _function_source(
            "tools/acceptance/device.py", "_close_minitouch_probe"
        )
        self.assertIn('adb_forward_remove(f"tcp:{port}")', close_probe)

    def test_websocket_errors_are_json_encoded(self):
        source = _source("module/webui/api.py")
        for name in ("ws_live_screenshot", "ws_live_control", "_reject_nonlocal_live_websocket"):
            function = _function_source("module/webui/api.py", name)
            self.assertNotIn("await websocket.send_text(str(", function)
        self.assertIn("json.dumps({", source)

    def test_live_routes_keep_auth_guard(self):
        source = _source("module/webui/api.py")
        screenshot_guard = _function_source(
            "module/webui/api.py", "_ws_live_screenshot_guarded"
        )
        control_guard = _function_source(
            "module/webui/api.py", "_ws_live_control_guarded"
        )
        self.assertIn(
            "if await _reject_nonlocal_live_websocket(websocket):",
            screenshot_guard,
        )
        self.assertIn("await ws_live_screenshot(websocket)", screenshot_guard)
        self.assertIn(
            "if await _reject_nonlocal_live_websocket(websocket):",
            control_guard,
        )
        self.assertIn("await ws_live_control(websocket)", control_guard)
        self.assertIn(
            'WebSocketRoute("/ws/live_screenshot", _ws_live_screenshot_guarded)',
            source,
        )
        self.assertIn(
            'WebSocketRoute("/ws/live_control", _ws_live_control_guarded)',
            source,
        )

    def test_local_live_guard_accepts_ipv4_ipv6_and_localhost(self):
        namespace = _load_live_guard_functions()
        guard = namespace["_reject_nonlocal_live_websocket"]
        for host in ("127.0.0.1", "127.12.34.56", "::1", "::ffff:127.0.0.1", "localhost"):
            with self.subTest(host=host):
                websocket = _FakeWebSocket(host)
                self.assertFalse(asyncio.run(guard(websocket)))
                self.assertFalse(websocket.accepted)
                self.assertEqual(websocket.messages, [])
                self.assertIsNone(websocket.close_code)

    def test_remote_live_guard_rejects_before_device_initialization(self):
        namespace = _load_live_guard_functions()
        guard = namespace["_reject_nonlocal_live_websocket"]
        websocket = _FakeWebSocket("192.0.2.10")
        self.assertTrue(asyncio.run(guard(websocket)))
        self.assertTrue(websocket.accepted)
        self.assertEqual(websocket.close_code, 4403)
        payload = json.loads(websocket.messages[0])
        self.assertEqual(payload["type"], "error")
        self.assertIn("только из локальной WebUI", payload["message"])


if __name__ == "__main__":
    unittest.main()
