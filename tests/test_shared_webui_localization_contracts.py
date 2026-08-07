import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEBUI_ROOT = ROOT / "module/webui"
WEBUI_RUNTIME_SUFFIXES = {".py", ".html", ".js", ".css"}


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _webui_runtime_sources():
    return sorted(
        path
        for path in WEBUI_ROOT.rglob("*")
        if path.is_file() and path.suffix.lower() in WEBUI_RUNTIME_SUFFIXES
    )


def _class_method(path: str, class_name: str, method_name: str) -> ast.FunctionDef:
    tree = ast.parse(_source(path), filename=path)
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return next(
        node
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == method_name
    )


class TestSharedWebUiLocalizationContracts(unittest.TestCase):
    def test_webui_runtime_sources_are_utf8_clean(self):
        for path in _webui_runtime_sources():
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertNotIn("\ufffd", path.read_text(encoding="utf-8"))

    def test_obs_overlay_preserves_dom_and_machine_contract(self):
        source = _source("module/webui/obs_overlay.html")

        for element_id in (
            "status-dot",
            "btn-line",
            "btn-day",
            "btn-month",
            "battle-rounds",
            "avg-ap",
            "akashi-encounters",
            "akashi-prob",
            "chart-title",
            "ap-change",
            "current-ap",
            "ap-canvas",
            "update-time",
            "error-msg",
        ):
            self.assertIn(f'id="{element_id}"', source)

        self.assertIn("const UPDATE_INTERVAL = 10000", source)
        self.assertIn("let currentInstance = 'alas'", source)
        self.assertIn("let chartView = 'day'", source)
        self.assertIn("setChartView('line')", source)
        self.assertIn("setChartView('day')", source)
        self.assertIn("setChartView('month')", source)
        self.assertIn("/api/cl1_stats?instance=${currentInstance}", source)
        self.assertIn("/api/ap_timeline?instance=${currentInstance}", source)
        self.assertIn("stats.battle_rounds", source)
        self.assertIn("stats.average_stamina", source)
        self.assertIn("stats.akashi_encounters", source)
        self.assertIn("stats.akashi_probability", source)
        self.assertIn("document.addEventListener('visibilitychange'", source)

    def test_obs_overlay_visible_runtime_text_is_russian(self):
        source = _source("module/webui/obs_overlay.html")
        for text in (
            "Мониторинг зоны коррозии 1",
            "Средние очки действия",
            "Вероятность появления Акаши",
            "Изменение очков действия",
            "Последнее обновление",
            "Нет данных об очках действия",
            "Не удалось подключиться к Alas",
        ):
            self.assertIn(text, source)

        for old_text in (
            "侵蚀一监控系统",
            "数据实时更新中",
            "当月战斗轮次",
            "平均体力",
            "遇见明石次数",
            "明石出现概率",
            "暂无体力数据流",
            "API获取失败:",
            "连接至 Alas 失败",
        ):
            self.assertNotIn(old_text, source)

    def test_api_routes_and_methods_are_current_product_contract(self):
        path = "module/webui/api.py"
        tree = ast.parse(_source(path), filename=path)
        assignment = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "api_routes" for target in node.targets)
        )
        self.assertIsInstance(assignment.value, ast.List)

        routes = []
        for call in assignment.value.elts:
            self.assertIsInstance(call, ast.Call)
            self.assertIsInstance(call.func, ast.Name)
            self.assertIn(call.func.id, {"Route", "WebSocketRoute"})
            path_value = ast.literal_eval(call.args[0])
            methods = None
            for keyword in call.keywords:
                if keyword.arg == "methods":
                    methods = tuple(ast.literal_eval(keyword.value))
            routes.append((call.func.id, path_value, methods))

        self.assertEqual(
            [
                ("Route", "/api/cl1_stats", None),
                ("Route", "/api/ap_timeline", None),
                ("Route", "/api/notify", ("POST",)),
                ("Route", "/api/notify_stream", None),
                ("Route", "/api/launcher/status", None),
                ("Route", "/api/launcher/startup", ("POST",)),
                ("Route", "/api/launcher/stream", None),
                ("Route", "/api/launcher/report", ("POST",)),
                ("Route", "/api/deploy/settings", None),
                ("Route", "/api/deploy/settings", ("POST",)),
                ("Route", "/api/deploy/startup-run", None),
                ("Route", "/api/deploy/startup-run", ("POST",)),
                ("Route", "/api/import_legacy_upload", ("POST",)),
                ("Route", "/obs", None),
                ("WebSocketRoute", "/ws/live_screenshot", None),
                ("WebSocketRoute", "/ws/live_control", None),
            ],
            routes,
        )

    def test_remote_access_security_and_machine_values_are_preserved(self):
        source = _source("module/webui/remote_access.py")
        for literal in (
            "P2P_SETUP_TIMEOUT = 60",
            "SSH_RECONNECT_DELAY = 2",
            "SSH_RECONNECT_MAX_DELAY = 30",
            'HOST_KEY_CHANGED_MARKER = "REMOTE HOST IDENTIFICATION HAS CHANGED"',
            '("ssh", "webrtc", "auto")',
            '"ssh_not_found"',
            '"ssh_host_key_changed"',
            '"invalid_provider_response"',
            '"too_many_redirects"',
            '"remote_access_failed"',
            '"ssh_forward"',
            '"stopped"',
            "_is_private_redirect_host(host)",
            "fnmatch.fnmatch(host, pattern)",
            "sanitize_traceback_text",
        ):
            self.assertIn(literal, source)

        self.assertIn("Перенаправление на приватный хост запрещено", source)
        self.assertIn("Перенаправление на недоверенный хост запрещено", source)

    def test_process_manager_state_override_keeps_allowed_states(self):
        method = _class_method(
            "module/webui/process_manager.py",
            "ProcessManager",
            "set_state_override",
        )
        first_if = next(node for node in method.body if isinstance(node, ast.If))
        self.assertIsInstance(first_if.test, ast.Compare)
        self.assertIsInstance(first_if.test.left, ast.Name)
        self.assertEqual("state", first_if.test.left.id)
        self.assertIsInstance(first_if.test.ops[0], ast.NotIn)
        self.assertEqual((1, 2, 3), tuple(ast.literal_eval(first_if.test.comparators[0])))
        self.assertTrue(
            any(
                isinstance(node, ast.Raise)
                and isinstance(node.exc, ast.Call)
                and isinstance(node.exc.func, ast.Name)
                and node.exc.func.id == "ValueError"
                for node in ast.walk(method)
            )
        )

    def test_oobe_global_package_mapping_is_preserved(self):
        source = _source("module/webui/oobe_base.py")
        self.assertIn('if package != "com.YoStarEN.AzurLane"', source)
        self.assertIn('if server != "en"', source)
        self.assertIn('return "com.YoStarEN.AzurLane"', source)
        self.assertIn("Неподдерживаемый пакет Global", source)
        self.assertIn("Неподдерживаемый сервер Global", source)

    def test_dynamic_values_and_html_escape_remain_in_localized_surfaces(self):
        self.assertIn(
            'f"[WebUI — Главная] Отправка объявления: {data.get(\'title\')}"',
            _source("module/webui/app_home.py"),
        )
        task_source = _source("module/webui/app_task_config.py")
        self.assertIn("{filepath_config(config_name)}", task_source)
        self.assertIn("{dict_to_kv(modified)}", task_source)
        self.assertIn("{v}", task_source)
        self.assertIn("{k}", task_source)

        calculator_source = _source("module/webui/event_calculator.py")
        self.assertIn("{escape(message)}", calculator_source)
        self.assertNotIn("{message}</div>", calculator_source)

        remote_source = _source("module/webui/remote_access.py")
        self.assertIn("[{host}]", remote_source)
        self.assertIn("{allowed_hosts}", remote_source)


if __name__ == "__main__":
    unittest.main()
