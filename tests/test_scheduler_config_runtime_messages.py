from __future__ import annotations

import ast
import builtins
import copy
import re
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _function_nodes(path: str, names: set[str], class_name: str | None = None):
    tree = ast.parse(_source(path), filename=path)
    body = tree.body
    if class_name is not None:
        owner = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        )
        body = owner.body
    nodes = []
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            cloned = copy.deepcopy(node)
            cloned.decorator_list = []
            nodes.append(cloned)
    if {node.name for node in nodes} != names:
        missing = sorted(names - {node.name for node in nodes})
        raise AssertionError(f"Functions not found in {path}: {missing}")
    return nodes


def _load_functions(
    path: str,
    names: set[str],
    *,
    class_name: str | None = None,
    globals_: dict | None = None,
):
    namespace = dict(globals_ or {})
    module = ast.Module(
        body=_function_nodes(path, names, class_name=class_name),
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    exec(compile(module, f"<{path}>", "exec"), namespace)
    return namespace


class _LogRecorder:
    def __init__(self):
        self.calls: list[tuple[str, object, object | None]] = []

    def info(self, message):
        self.calls.append(("info", message, None))

    def warning(self, message):
        self.calls.append(("warning", message, None))

    def critical(self, message):
        self.calls.append(("critical", message, None))

    def attr(self, name, value):
        self.calls.append(("attr", name, value))


class _RequestHumanTakeover(Exception):
    pass


class _ScriptError(Exception):
    pass


class SchedulerConfigMessageContracts(unittest.TestCase):
    def test_candidate_runtime_text_is_russian_and_tokens_are_preserved(self):
        config_source = _source("module/config/config.py")
        server_source = _source("module/config/server.py")
        utils_source = _source("module/config/utils.py")

        expected_config = (
            "[Конфигурация] Задачи в очереди:",
            'logger.attr("Задача", task)',
            "[Конфигурация] Нет задач в очереди",
            "[Конфигурация] Нет задач в очереди или ожидающих запуска",
            "[Конфигурация] Включите хотя бы одну задачу",
            "[Конфигурация] Задача `{task}` отложена до {run} ({kv})",
            "[Конфигурация] Для delay_next_run требуется хотя бы один аргумент",
            "[Конфигурация — Operation Siren] Задача `{task}` отложена до {next_run} ({kv})",
            "[Конфигурация — Operation Siren] До сброса менее суток: задачи отложены на 2,5 часа",
            "[Конфигурация] Вызываемая задача `{task}` отсутствует в пользовательской конфигурации",
            "[Конфигурация] Вызов задачи: {task}",
            "[Конфигурация] Продолжение задачи `{new}`",
            "[Конфигурация] Переключение с задачи `{prev}` на `{new}`",
            "[Конфигурация] Проверка переключения задач временно отключена",
        )
        for text in expected_config:
            with self.subTest(text=text):
                self.assertIn(text, config_source)

        for text in (
            "Неподдерживаемое значение пакета или сервера Global/EN: {package_or_server}",
            "Неподдерживаемый часовой пояс сервера: {server_.server}",
            "Следующий сброс Operation Siren",
            "Дней до сброса",
            "[Конфигурация] Текущая система не Windows; GPU не используется",
            "[Конфигурация] Обнаружен производительный GPU",
            "[Конфигурация] Производительный GPU не обнаружен",
            "[Конфигурация] Не удалось определить производительность GPU",
        ):
            with self.subTest(text=text):
                self.assertIn(text, server_source + utils_source)

        for stale in (
            "[配置] 待处理任务:",
            "[配置] 没有待处理任务",
            "[Config] 没有等待或待处理的任务",
            "[Config] 请启用至少一个任务",
            "[配置] 延迟任务",
            "[配置-大世界] 延迟任务",
            "Unsupported Global/EN package or server:",
            "Unsupported server timezone:",
            "[Config] 当前系统为非 Windows，不使用 GPU",
            "[Config] 检测到高性能 GPU",
            "[Config] 未检测到高性能 GPU",
            "[Config] 检测 GPU 性能失败",
        ):
            with self.subTest(stale=stale):
                self.assertNotIn(stale, config_source + server_source + utils_source)

        for token in (
            "delay_next_run",
            "Scheduler.NextRun",
            '"success": success',
            '"server_update": server_update',
            '"target": target',
            '"minute": minute',
            '"en"',
            "com.YoStarEN.AzurLane",
        ):
            with self.subTest(token=token):
                self.assertIn(token, config_source + server_source)

    def test_logger_exception_and_app_marker_surfaces_are_unchanged(self):
        config_source = _source("module/config/config.py")
        server_source = _source("module/config/server.py")
        app_source = _source("deploy/Windows/app.py")

        self.assertIn(
            "logger.info(f\"[Конфигурация] Задачи в очереди: {[f.command for f in self.pending_task]}\")",
            config_source,
        )
        self.assertIn(
            'logger.critical("[Конфигурация] Нет задач в очереди или ожидающих запуска")',
            config_source,
        )
        self.assertIn("raise RequestHumanTakeover", config_source)
        self.assertIn("raise ScriptError(", config_source)
        self.assertEqual(server_source.count("raise ValueError("), 2)

        self.assertIn(
            "logger.info(f'Обновление app.asar [Update app.asar] {update} -----> {source}')",
            app_source,
        )
        self.assertEqual(app_source.count("[Update app.asar]"), 1)
        for token in ("app.asar", "alas_webapp", "AppAsarUpdate"):
            self.assertIn(token, app_source)

    def test_gpu_threshold_and_command_are_exact(self):
        source = _source("module/config/utils.py")
        self.assertIn("if int(line) >= 1073741824:", source)
        self.assertIn("Get-CimInstance Win32_VideoController", source)
        self.assertIn("capture_output=True, text=True, check=True", source)

    def test_get_next_pending_waiting_and_takeover_paths(self):
        logger = _LogRecorder()

        class ConfigState:
            is_hoarding_task = True

        namespace = _load_functions(
            "module/config/config.py",
            {"get_next"},
            class_name="AzurLaneConfig",
            globals_={
                "AzurLaneConfig": ConfigState,
                "RequestHumanTakeover": _RequestHumanTakeover,
                "copy": copy,
                "logger": logger,
            },
        )
        get_next = namespace["get_next"]

        pending = types.SimpleNamespace(command="Research")
        config = types.SimpleNamespace(
            pending_task=[pending],
            waiting_task=[],
            get_next_task=lambda: None,
            hoarding=timedelta(minutes=0),
        )
        self.assertIs(get_next(config), pending)
        self.assertFalse(ConfigState.is_hoarding_task)
        self.assertEqual(logger.calls[0][0], "info")
        self.assertIn("Research", logger.calls[0][1])
        self.assertEqual(logger.calls[1], ("attr", "Задача", pending))

        logger.calls.clear()
        ConfigState.is_hoarding_task = False
        waiting = types.SimpleNamespace(
            command="Commission",
            next_run=datetime(2030, 1, 1, 12, 0),
        )
        config = types.SimpleNamespace(
            pending_task=[],
            waiting_task=[waiting],
            get_next_task=lambda: None,
            hoarding=timedelta(minutes=15),
        )
        selected = get_next(config)
        self.assertIsNot(selected, waiting)
        self.assertEqual(selected.command, "Commission")
        self.assertEqual(selected.next_run, datetime(2030, 1, 1, 12, 15))
        self.assertTrue(ConfigState.is_hoarding_task)
        self.assertEqual(logger.calls[0], ("info", "[Конфигурация] Нет задач в очереди", None))
        self.assertEqual(logger.calls[1][0:2], ("attr", "Задача"))

        logger.calls.clear()
        config = types.SimpleNamespace(
            pending_task=[],
            waiting_task=[],
            get_next_task=lambda: None,
            hoarding=timedelta(minutes=0),
        )
        with self.assertRaises(_RequestHumanTakeover):
            get_next(config)
        self.assertEqual(
            logger.calls,
            [
                (
                    "critical",
                    "[Конфигурация] Нет задач в очереди или ожидающих запуска",
                    None,
                ),
                (
                    "critical",
                    "[Конфигурация] Включите хотя бы одну задачу",
                    None,
                ),
            ],
        )

    def test_task_delay_preserves_values_and_all_delay_sources(self):
        logger = _LogRecorder()
        now = datetime(2030, 1, 1, 12, 0)
        server_run = datetime(2030, 1, 2, 0, 0)
        target_run = datetime(2030, 1, 1, 13, 30)
        server_inputs: list[object] = []
        target_inputs: list[object] = []

        def get_server_next_update(value):
            server_inputs.append(value)
            return server_run

        def nearest_future(value):
            target_inputs.append(value)
            return target_run

        def dict_to_kv(values, allow_none=True):
            return ", ".join(
                f"{key}={value!r}"
                for key, value in values.items()
                if allow_none or value is not None
            )

        namespace = _load_functions(
            "module/config/config.py",
            {"task_delay"},
            class_name="AzurLaneConfig",
            globals_={
                "ScriptError": _ScriptError,
                "current_time": lambda: now,
                "dict_to_kv": dict_to_kv,
                "ensure_time": lambda value, precision=3: value,
                "get_server_next_update": get_server_next_update,
                "logger": logger,
                "nearest_future": nearest_future,
                "timedelta": timedelta,
            },
        )
        task_delay = namespace["task_delay"]

        def make_config():
            config = types.SimpleNamespace(
                Scheduler_SuccessInterval=10,
                Scheduler_FailureInterval=20,
                Scheduler_ServerUpdate=["00:00"],
                modified={},
                task=types.SimpleNamespace(command="Research"),
                update_count=0,
            )

            def update():
                config.update_count += 1

            config.update = update
            return config

        cases = (
            ({"success": True}, "Research", now + timedelta(minutes=10)),
            ({"success": False}, "Research", now + timedelta(minutes=20)),
            ({"server_update": True}, "Research", server_run),
            ({"target": "2030-01-01T13:30:00"}, "Research", target_run),
            ({"minute": 5, "task": "Commission"}, "Commission", now + timedelta(minutes=5)),
        )
        for kwargs, task, expected in cases:
            with self.subTest(kwargs=kwargs):
                logger.calls.clear()
                config = make_config()
                task_delay(config, **kwargs)
                self.assertEqual(config.modified[f"{task}.Scheduler.NextRun"], expected)
                self.assertEqual(config.update_count, 1)
                self.assertEqual(logger.calls[0][0], "info")
                self.assertIn(f"`{task}`", logger.calls[0][1])
                self.assertIn(str(expected), logger.calls[0][1])

        self.assertEqual(server_inputs, [["00:00"]])
        self.assertEqual(target_inputs, [["2030-01-01T13:30:00"]])

        config = make_config()
        with self.assertRaisesRegex(_ScriptError, "delay_next_run"):
            task_delay(config)
        self.assertEqual(config.modified, {})
        self.assertEqual(config.update_count, 0)

    def test_global_en_server_validation_has_no_invalid_side_effect(self):
        namespace = _load_functions(
            "module/config/server.py",
            {"to_server", "to_package", "set_server"},
            globals_={
                "GLOBAL_PACKAGE": "com.YoStarEN.AzurLane",
                "server": "en",
            },
        )
        self.assertEqual(namespace["to_server"]("en"), "en")
        self.assertEqual(
            namespace["to_server"]("com.YoStarEN.AzurLane"),
            "en",
        )
        self.assertEqual(
            namespace["to_package"]("en"),
            "com.YoStarEN.AzurLane",
        )
        self.assertEqual(
            namespace["to_package"]("com.YoStarEN.AzurLane"),
            "com.YoStarEN.AzurLane",
        )

        invalid = "synthetic.invalid.package"
        real_import = builtins.__import__
        with mock.patch("builtins.__import__", wraps=real_import) as importer:
            with self.assertRaises(ValueError) as raised:
                namespace["set_server"](invalid)
        self.assertIn(invalid, str(raised.exception))
        self.assertIn("Неподдерживаемое значение", str(raised.exception))
        self.assertEqual(namespace["server"], "en")
        imported_names = [call.args[0] for call in importer.call_args_list]
        self.assertNotIn("module.base.resource", imported_names)

    def test_gpu_probe_paths_use_mocked_subprocess_only(self):
        logger = _LogRecorder()
        os_stub = types.SimpleNamespace(name="posix")
        namespace = _load_functions(
            "module/config/utils.py",
            {"is_good_gpu"},
            globals_={"logger": logger, "os": os_stub},
        )
        is_good_gpu = namespace["is_good_gpu"]

        with mock.patch("subprocess.run") as run:
            self.assertFalse(is_good_gpu())
        run.assert_not_called()
        self.assertEqual(
            logger.calls,
            [
                (
                    "info",
                    "[Конфигурация] Текущая система не Windows; GPU не используется",
                    None,
                )
            ],
        )

        os_stub.name = "nt"
        logger.calls.clear()
        result = types.SimpleNamespace(stdout="invalid\n1073741824\n")
        with mock.patch("subprocess.run", return_value=result) as run:
            self.assertTrue(is_good_gpu())
        run.assert_called_once_with(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_VideoController | ForEach-Object { $_.AdapterRAM }",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(
            logger.calls[-1],
            ("info", "[Конфигурация] Обнаружен производительный GPU", None),
        )

        logger.calls.clear()
        result = types.SimpleNamespace(stdout="invalid\n1073741823\n")
        with mock.patch("subprocess.run", return_value=result):
            self.assertFalse(is_good_gpu())
        self.assertEqual(
            logger.calls[-1],
            ("info", "[Конфигурация] Производительный GPU не обнаружен", None),
        )

        logger.calls.clear()
        with mock.patch("subprocess.run", side_effect=RuntimeError("synthetic")):
            self.assertFalse(is_good_gpu())
        self.assertEqual(
            logger.calls[-1],
            (
                "warning",
                "[Конфигурация] Не удалось определить производительность GPU",
                None,
            ),
        )

    def test_utf8_round_trip_has_no_replacement_characters(self):
        paths = (
            "deploy/Windows/app.py",
            "module/config/config.py",
            "module/config/server.py",
            "module/config/utils.py",
        )
        for path in paths:
            with self.subTest(path=path):
                raw = (ROOT / path).read_bytes()
                decoded = raw.decode("utf-8")
                self.assertEqual(decoded.encode("utf-8"), raw)
                self.assertNotIn("�", decoded)

    def test_candidate_strings_keep_expected_placeholder_expressions(self):
        config_source = _source("module/config/config.py")
        server_source = _source("module/config/server.py")
        expected = (
            r"\{\[f\.command for f in self\.pending_task\]\}",
            r"Задача `\{task\}` отложена до \{run\} \(\{kv\}\)",
            r"Задача `\{task\}` отложена до \{next_run\} \(\{kv\}\)",
            r"Переключение с задачи `\{prev\}` на `\{new\}`",
            r"Global/EN: \{package_or_server\}",
        )
        combined = config_source + server_source
        for pattern in expected:
            with self.subTest(pattern=pattern):
                self.assertRegex(combined, pattern)


if __name__ == "__main__":
    unittest.main()
