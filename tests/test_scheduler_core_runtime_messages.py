from __future__ import annotations

import ast
import copy
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "alas.py"


def _source() -> str:
    return SOURCE_PATH.read_text(encoding="utf-8")


def _tree() -> ast.Module:
    return ast.parse(_source(), filename="alas.py")


def _method_node(name: str) -> ast.FunctionDef:
    for node in _tree().body:
        if isinstance(node, ast.ClassDef) and node.name == "AzurLaneAutoScript":
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name == name:
                    return child
    raise AssertionError(f"Method not found: {name}")


def _method_source(name: str) -> str:
    source = _source()
    lines = source.splitlines()
    node = _method_node(name)
    return "\n".join(lines[node.lineno - 1 : node.end_lineno])


def _load_method(name: str, globals_: dict) -> object:
    node = copy.deepcopy(_method_node(name))
    node.decorator_list = []
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = dict(globals_)
    exec(compile(module, f"<alas.py:{name}>", "exec"), namespace)
    return namespace[name]


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        parts = [call.func.attr]
        value = call.func.value
        while isinstance(value, ast.Attribute):
            parts.append(value.attr)
            value = value.value
        if isinstance(value, ast.Name):
            parts.append(value.id)
        return ".".join(reversed(parts))
    return ""


class _Logger:
    def __init__(self, events: list):
        self.events = events

    def _record(self, name, *args, **kwargs):
        self.events.append((name, args, kwargs))

    def debug(self, *args, **kwargs): self._record("debug", *args, **kwargs)
    def info(self, *args, **kwargs): self._record("info", *args, **kwargs)
    def warning(self, *args, **kwargs): self._record("warning", *args, **kwargs)
    def error(self, *args, **kwargs): self._record("error", *args, **kwargs)
    def critical(self, *args, **kwargs): self._record("critical", *args, **kwargs)
    def hr(self, *args, **kwargs): self._record("hr", *args, **kwargs)
    def attr(self, *args, **kwargs): self._record("attr", *args, **kwargs)
    def error_context(self, *args, **kwargs): self._record("error_context", *args, **kwargs)
    def exception_context(self, *args, **kwargs): self._record("exception_context", *args, **kwargs)
    def set_file_logger(self, *args, **kwargs): self._record("set_file_logger", *args, **kwargs)


class _TaskEnd(Exception): pass
class _GameNotRunningError(Exception): pass
class _GameStuckError(Exception): pass
class _GameTooManyClickError(Exception): pass
class _GameBugError(Exception): pass
class _GamePageUnknownError(Exception): pass
class _ScriptError(Exception): pass
class _EmulatorNotRunningError(Exception): pass
class _RequestHumanTakeover(Exception): pass
class _AutoSearchSetError(Exception): pass


class SchedulerCoreRuntimeMessageTests(unittest.TestCase):
    def test_runtime_literals_are_russian_or_preserved_technical(self):
        tree = _tree()
        docstrings = set()
        for owner in ast.walk(tree):
            body = getattr(owner, "body", None)
            if (
                isinstance(body, list)
                and body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))

        cjk = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
        findings = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and (cjk.search(node.value) or "喵" in node.value)
            ):
                findings.append((node.lineno, node.value))
        self.assertEqual(findings, [])

        source = _source()
        for token in (
            "AzurPilot", "Alas", "WebUI", "ADB", "OnePush", "Restart",
            "GameNotRunningError", "GameStuckError",
            "GameTooManyClickError", "EmulatorNotRunningError",
            "RequestHumanTakeover", "ScriptError", "TaskEnd",
            "'recoverable'",
        ):
            with self.subTest(token=token):
                self.assertIn(token, source)

    def test_notification_surfaces_keep_shape_and_raw_payloads(self):
        for method_name in ("_check_sensitive_exit", "run", "loop"):
            method = _method_node(method_name)
            for call in (n for n in ast.walk(method) if isinstance(n, ast.Call)):
                name = _call_name(call)
                if name not in {"handle_notify", "notify_webui"}:
                    continue
                self.assertEqual(len(call.args), 1)
                keyword_names = [keyword.arg for keyword in call.keywords]
                self.assertEqual(keyword_names, ["title", "content"])

        sensitive = _method_source("_check_sensitive_exit")
        self.assertIn("AzurPilot", sensitive)
        self.assertIn(r"\n{error}", sensitive)
        self.assertNotIn("喵", sensitive)

        run = _method_source("run")
        for raw in (
            "GamePageUnknownError",
            "ScriptError",
            "EmulatorNotRunningError",
            "RequestHumanTakeover",
        ):
            self.assertIn(raw, run)
        self.assertIn("Причина: GamePageUnknownError", run)
        self.assertIn("Причина: ScriptError", run)
        self.assertIn("Причина: EmulatorNotRunningError", run)

        loop = _method_source("loop")
        self.assertIn(r"RequestHumanTakeover\nЗадача `{task}`", loop)
        self.assertIn("Задача `{task}` завершилась с ошибкой не менее {failed} раз.", loop)
        self.assertNotIn("crashed", loop)
        self.assertNotIn("failed {failed} or more times", loop)

    def test_exception_order_returns_and_restart_target_are_unchanged(self):
        run = _method_node("run")
        try_node = next(node for node in run.body if isinstance(node, ast.Try))
        handler_names = []
        for handler in try_node.handlers:
            value = handler.type
            if value is None:
                handler_names.append(None)
            elif isinstance(value, ast.Name):
                handler_names.append(value.id)
            elif isinstance(value, ast.Tuple):
                handler_names.append(tuple(e.id for e in value.elts if isinstance(e, ast.Name)))
        self.assertEqual(
            handler_names,
            [
                "TaskEnd",
                "GameNotRunningError",
                ("GameStuckError", "GameTooManyClickError"),
                "GameBugError",
                "GamePageUnknownError",
                "ScriptError",
                "EmulatorNotRunningError",
                "RequestHumanTakeover",
                "AutoSearchSetError",
                "Exception",
            ],
        )

        source = _method_source("run")
        self.assertGreaterEqual(source.count("self.config.task_call('Restart')"), 4)
        self.assertIn("return 'recoverable'", source)
        self.assertIn("return False", source)
        self.assertIn("return True", source)
        self.assertIn("self.device.sleep(10)", source)
        self.assertIn("self.consecutive_game_stuck >= limit", source)

    def test_restart_stop_and_loop_constants_are_exact(self):
        restart = _method_source("_try_restart_emulator")
        self.assertIn("self.consecutive_adb_offline > limit", restart)
        self.assertIn("time.sleep(5)", restart)
        self.assertLess(restart.index("device.emulator_stop()"), restart.index("time.sleep(5)"))
        self.assertLess(restart.index("time.sleep(5)"), restart.index("device.emulator_start()"))

        wait = _method_source("wait_until")
        self.assertIn("time.sleep(5)", wait)
        self.assertIn("exit(0)", wait)
        self.assertIn("[Alas] Получен запрос на остановку", wait)
        self.assertNotIn("Reason: Stop request", wait)

        loop = _method_source("loop")
        for invariant in (
            "MAX_GLOBAL_FAILURES = 3",
            "RESTART_DELAY = 20",
            "LONG_WAIT = 300",
            "failed >= 3",
            "failed >= 1",
            "consecutive_global_failures >= MAX_GLOBAL_FAILURES",
            "self.config.task_call('Restart')",
            "self.consecutive_game_stuck = 0",
            "self.consecutive_adb_offline = 0",
        ):
            with self.subTest(invariant=invariant):
                self.assertIn(invariant, loop)

    def test_structured_error_calls_keep_diagnostic_fields(self):
        allowed = {
            "title", "reason", "impact", "action", "exc", "level", "with_traceback"
        }
        required = {"title"}
        for method_name in (
            "_try_restart_emulator",
            "config",
            "device",
            "checker",
            "_check_sensitive_exit",
            "run",
            "save_error_log",
            "loop",
        ):
            for call in (
                node for node in ast.walk(_method_node(method_name))
                if isinstance(node, ast.Call)
                and _call_name(node) in {"logger.error_context", "logger.exception_context"}
            ):
                names = {keyword.arg for keyword in call.keywords}
                self.assertTrue(required <= names)
                self.assertTrue(names <= allowed)
                self.assertEqual(len(call.args), 0)

    def test_synthetic_sensitive_notification_preserves_raw_error_and_order(self):
        events = []
        logger = _Logger(events)

        def handle_notify(*args, **kwargs):
            events.append(("handle_notify", args, kwargs))

        def notify_webui(*args, **kwargs):
            events.append(("notify_webui", args, kwargs))

        method = _load_method(
            "_check_sensitive_exit",
            {
                "inflection": types.SimpleNamespace(camelize=lambda value: "SyntheticTask"),
                "logger": logger,
                "handle_notify": handle_notify,
                "notify_webui": notify_webui,
            },
        )
        config = types.SimpleNamespace(
            Error_OnePushConfig={"provider": "synthetic"},
            cross_get=lambda **kwargs: True,
        )
        subject = types.SimpleNamespace(config=config, config_name="SYNTHETIC")
        error = RuntimeError("RAW_SYNTHETIC_ERROR")

        with self.assertRaises(SystemExit) as raised:
            method(subject, "synthetic_task", error)
        self.assertEqual(raised.exception.code, 1)

        names = [event[0] for event in events]
        self.assertEqual(names, ["error_context", "handle_notify", "notify_webui"])
        for name in ("handle_notify", "notify_webui"):
            event = next(item for item in events if item[0] == name)
            self.assertTrue(event[2]["content"].endswith("\nRAW_SYNTHETIC_ERROR"))
            self.assertNotIn("喵", event[2]["title"] + event[2]["content"])

    def test_synthetic_emulator_restart_keeps_stop_sleep_start_order(self):
        events = []
        logger = _Logger(events)
        device = types.SimpleNamespace(
            emulator_stop=lambda: events.append(("emulator_stop", (), {})),
            emulator_start=lambda: events.append(("emulator_start", (), {})),
        )
        time_stub = types.SimpleNamespace(
            sleep=lambda seconds: events.append(("sleep", (seconds,), {}))
        )
        method = _load_method(
            "_try_restart_emulator",
            {
                "logger": logger,
                "time": time_stub,
                "del_cached_property": lambda obj, name: events.append(
                    ("del_cached_property", (name,), {})
                ),
            },
        )
        config = types.SimpleNamespace(
            Error_AdbOfflineRestart=True,
            Error_AdbOfflineThreshold=2,
        )
        subject = types.SimpleNamespace(
            config=config,
            consecutive_adb_offline=0,
            device=device,
        )
        self.assertTrue(method(subject))
        names = [event[0] for event in events]
        self.assertLess(names.index("emulator_stop"), names.index("sleep"))
        self.assertLess(names.index("sleep"), names.index("emulator_start"))
        sleep_event = next(event for event in events if event[0] == "sleep")
        self.assertEqual(sleep_event[1], (5,))
        self.assertEqual(subject.consecutive_adb_offline, 1)

    def test_synthetic_run_game_not_running_keeps_calls_and_return(self):
        events = []
        logger = _Logger(events)

        def handle_notify(*args, **kwargs):
            events.append(("handle_notify", args, kwargs))

        def notify_webui(*args, **kwargs):
            events.append(("notify_webui", args, kwargs))

        globals_ = {
            "logger": logger,
            "handle_notify": handle_notify,
            "notify_webui": notify_webui,
            "TaskEnd": _TaskEnd,
            "GameNotRunningError": _GameNotRunningError,
            "GameStuckError": _GameStuckError,
            "GameTooManyClickError": _GameTooManyClickError,
            "GameBugError": _GameBugError,
            "GamePageUnknownError": _GamePageUnknownError,
            "ScriptError": _ScriptError,
            "EmulatorNotRunningError": _EmulatorNotRunningError,
            "RequestHumanTakeover": _RequestHumanTakeover,
            "AutoSearchSetError": _AutoSearchSetError,
        }
        method = _load_method("run", globals_)

        class Subject:
            config_name = "SYNTHETIC"
            consecutive_game_stuck = 0
            config = types.SimpleNamespace(
                Error_OnePushConfig={},
                Error_GameStuckRestart=False,
                task_call=lambda task: events.append(("task_call", (task,), {})),
            )
            device = types.SimpleNamespace(
                screenshot=lambda: events.append(("screenshot", (), {})),
                sleep=lambda seconds: events.append(("device_sleep", (seconds,), {})),
                package="com.YoStarEN.AzurLane",
            )

            def synthetic_task(self):
                raise _GameNotRunningError("RAW_NOT_RUNNING")

            def _check_sensitive_exit(self, command, error):
                events.append(("_check_sensitive_exit", (command, error), {}))
                return False

            def save_error_log(self):
                events.append(("save_error_log", (), {}))

            def _try_restart_emulator(self):
                return False

        result = method(Subject(), "synthetic_task")
        self.assertEqual(result, "recoverable")
        names = [event[0] for event in events]
        self.assertEqual(
            names,
            [
                "screenshot",
                "error_context",
                "_check_sensitive_exit",
                "handle_notify",
                "notify_webui",
                "task_call",
            ],
        )
        self.assertEqual(events[-1][1], ("Restart",))
        notification_text = "".join(
            event[2].get("title", "") + event[2].get("content", "")
            for event in events if event[0] in {"handle_notify", "notify_webui"}
        )
        self.assertNotIn("喵", notification_text)
        self.assertIn("Игра не запущена", notification_text)

    def test_synthetic_wait_until_stop_exits_without_sleep(self):
        events = []
        logger = _Logger(events)
        time_stub = types.SimpleNamespace(
            sleep=lambda seconds: events.append(("sleep", (seconds,), {}))
        )
        method = _load_method(
            "wait_until",
            {
                "logger": logger,
                "time": time_stub,
                "timedelta": __import__("datetime").timedelta,
                "current_time": lambda: __import__("datetime").datetime(2030, 1, 1),
            },
        )
        config = types.SimpleNamespace(
            start_watching=lambda: events.append(("start_watching", (), {})),
            should_reload=lambda: False,
        )
        stop_event = types.SimpleNamespace(is_set=lambda: True)
        subject = types.SimpleNamespace(
            config=config,
            config_name="SYNTHETIC",
            stop_event=stop_event,
        )
        future = __import__("datetime").datetime(2030, 1, 2)
        with self.assertRaises(SystemExit) as raised:
            method(subject, future)
        self.assertEqual(raised.exception.code, 0)
        self.assertNotIn("sleep", [event[0] for event in events])
        texts = [event[1][0] for event in events if event[0] == "info"]
        self.assertEqual(
            texts,
            [
                "[Alas] Получен запрос на остановку",
                "[SYNTHETIC] Работа завершена. Причина: запрос на остановку",
            ],
        )


    @staticmethod
    def _run_globals(logger, handle_notify, notify_webui):
        return {
            "logger": logger,
            "handle_notify": handle_notify,
            "notify_webui": notify_webui,
            "TaskEnd": _TaskEnd,
            "GameNotRunningError": _GameNotRunningError,
            "GameStuckError": _GameStuckError,
            "GameTooManyClickError": _GameTooManyClickError,
            "GameBugError": _GameBugError,
            "GamePageUnknownError": _GamePageUnknownError,
            "ScriptError": _ScriptError,
            "EmulatorNotRunningError": _EmulatorNotRunningError,
            "RequestHumanTakeover": _RequestHumanTakeover,
            "AutoSearchSetError": _AutoSearchSetError,
        }

    def test_synthetic_run_stuck_paths_keep_recovery_order(self):
        for exception_type in (_GameStuckError, _GameTooManyClickError):
            with self.subTest(exception_type=exception_type.__name__):
                events = []
                logger = _Logger(events)

                def handle_notify(*args, **kwargs):
                    events.append(("handle_notify", args, kwargs))

                def notify_webui(*args, **kwargs):
                    events.append(("notify_webui", args, kwargs))

                method = _load_method(
                    "run",
                    self._run_globals(logger, handle_notify, notify_webui),
                )

                class Subject:
                    config_name = "SYNTHETIC"
                    consecutive_game_stuck = 0
                    config = types.SimpleNamespace(
                        Error_OnePushConfig={},
                        Error_GameStuckRestart=False,
                        Error_GameStuckThreshold=3,
                        task_call=lambda task: events.append(
                            ("task_call", (task,), {})
                        ),
                    )
                    device = types.SimpleNamespace(
                        screenshot=lambda: events.append(("screenshot", (), {})),
                        sleep=lambda seconds: events.append(
                            ("device_sleep", (seconds,), {})
                        ),
                        package="com.YoStarEN.AzurLane",
                    )

                    def synthetic_task(self):
                        raise exception_type("RAW_STUCK")

                    def _check_sensitive_exit(self, command, error):
                        events.append(
                            ("_check_sensitive_exit", (command, error), {})
                        )
                        return False

                    def save_error_log(self):
                        events.append(("save_error_log", (), {}))

                    def _try_restart_emulator(self):
                        events.append(("_try_restart_emulator", (), {}))
                        return False

                result = method(Subject(), "synthetic_task")
                self.assertEqual(result, "recoverable")
                names = [event[0] for event in events]
                self.assertEqual(
                    names,
                    [
                        "screenshot",
                        "error_context",
                        "save_error_log",
                        "_check_sensitive_exit",
                        "warning",
                        "warning",
                        "handle_notify",
                        "notify_webui",
                        "task_call",
                        "device_sleep",
                    ],
                )
                self.assertEqual(events[-2][1], ("Restart",))
                self.assertEqual(events[-1][1], (10,))
                text = "".join(
                    event[2].get("title", "") + event[2].get("content", "")
                    for event in events
                    if event[0] in {"handle_notify", "notify_webui"}
                )
                self.assertIn("Игра зависла", text)
                self.assertNotIn("喵", text)

    def test_synthetic_run_emulator_not_running_keeps_restart_and_notifications(self):
        events = []
        logger = _Logger(events)

        def handle_notify(*args, **kwargs):
            events.append(("handle_notify", args, kwargs))

        def notify_webui(*args, **kwargs):
            events.append(("notify_webui", args, kwargs))

        method = _load_method(
            "run",
            self._run_globals(logger, handle_notify, notify_webui),
        )

        class Subject:
            config_name = "SYNTHETIC"
            config = types.SimpleNamespace(
                Error_OnePushConfig={},
                task_call=lambda task: events.append(("task_call", (task,), {})),
            )
            device = types.SimpleNamespace(
                screenshot=lambda: events.append(("screenshot", (), {})),
            )

            def synthetic_task(self):
                raise _EmulatorNotRunningError("RAW_ADB_OFFLINE")

            def _check_sensitive_exit(self, command, error):
                events.append(("_check_sensitive_exit", (command, error), {}))
                return False

            def save_error_log(self):
                events.append(("save_error_log", (), {}))

            def _try_restart_emulator(self):
                events.append(("_try_restart_emulator", (), {}))
                return True

        result = method(Subject(), "synthetic_task")
        self.assertEqual(result, "recoverable")
        names = [event[0] for event in events]
        self.assertEqual(
            names,
            [
                "screenshot",
                "error_context",
                "save_error_log",
                "_check_sensitive_exit",
                "_try_restart_emulator",
                "task_call",
                "handle_notify",
                "notify_webui",
            ],
        )
        self.assertEqual(events[5][1], ("Restart",))
        text = "".join(
            event[2].get("title", "") + event[2].get("content", "")
            for event in events
            if event[0] in {"handle_notify", "notify_webui"}
        )
        self.assertIn("эмулятор", text.lower())
        self.assertNotIn("喵", text)

    def test_synthetic_run_takeover_and_generic_failure_keep_fatal_contracts(self):
        for exception_type, expected_reason in (
            (_RequestHumanTakeover, "RequestHumanTakeover"),
            (RuntimeError, "необработанная ошибка"),
        ):
            with self.subTest(exception_type=exception_type.__name__):
                events = []
                logger = _Logger(events)

                def handle_notify(*args, **kwargs):
                    events.append(("handle_notify", args, kwargs))

                def notify_webui(*args, **kwargs):
                    events.append(("notify_webui", args, kwargs))

                method = _load_method(
                    "run",
                    self._run_globals(logger, handle_notify, notify_webui),
                )

                class Subject:
                    config_name = "SYNTHETIC"
                    config = types.SimpleNamespace(Error_OnePushConfig={})
                    device = types.SimpleNamespace(
                        screenshot=lambda: events.append(("screenshot", (), {})),
                    )

                    def synthetic_task(self):
                        raise exception_type("RAW_FATAL")

                    def save_error_log(self):
                        events.append(("save_error_log", (), {}))

                if exception_type is _RequestHumanTakeover:
                    with self.assertRaises(SystemExit) as raised:
                        method(Subject(), "synthetic_task")
                    self.assertEqual(raised.exception.code, 1)
                    self.assertEqual(
                        [event[0] for event in events],
                        [
                            "screenshot",
                            "error_context",
                            "handle_notify",
                            "notify_webui",
                        ],
                    )
                else:
                    with self.assertRaisesRegex(RuntimeError, "RAW_FATAL"):
                        method(Subject(), "synthetic_task")
                    self.assertEqual(
                        [event[0] for event in events],
                        [
                            "screenshot",
                            "exception_context",
                            "save_error_log",
                            "handle_notify",
                            "notify_webui",
                        ],
                    )

                text = "".join(
                    event[2].get("title", "") + event[2].get("content", "")
                    for event in events
                    if event[0] in {"handle_notify", "notify_webui"}
                )
                self.assertIn(expected_reason.lower(), text.lower())
                self.assertNotIn("喵", text)

    def test_emulator_restart_disabled_threshold_and_long_wait_paths(self):
        events = []
        logger = _Logger(events)
        method = _load_method(
            "_try_restart_emulator",
            {
                "logger": logger,
                "time": types.SimpleNamespace(
                    sleep=lambda seconds: events.append(("sleep", (seconds,), {}))
                ),
                "del_cached_property": lambda obj, name: events.append(
                    ("del_cached_property", (name,), {})
                ),
            },
        )

        disabled = types.SimpleNamespace(
            config=types.SimpleNamespace(
                Error_AdbOfflineRestart=False,
                Error_AdbOfflineThreshold=2,
            ),
            consecutive_adb_offline=0,
        )
        self.assertFalse(method(disabled))
        self.assertEqual(disabled.consecutive_adb_offline, 0)
        self.assertEqual([event[0] for event in events], ["error_context"])

        events.clear()
        threshold = types.SimpleNamespace(
            config=types.SimpleNamespace(
                Error_AdbOfflineRestart=True,
                Error_AdbOfflineThreshold=0,
            ),
            consecutive_adb_offline=0,
        )
        self.assertFalse(method(threshold))
        self.assertEqual(threshold.consecutive_adb_offline, 1)
        self.assertEqual(
            [event[0] for event in events],
            ["warning", "error_context"],
        )

        long_wait = _load_method(
            "_start_emulator_after_long_wait",
            {
                "logger": logger,
                "del_cached_property": lambda obj, name: events.append(
                    ("del_cached_property", (name,), {})
                ),
            },
        )
        platform_module = types.ModuleType("module.device.platform")

        class MissingPlatform:
            def __init__(self, config, connect=False):
                self.emulator_instance = None

        platform_module.Platform = MissingPlatform
        module_stub = types.ModuleType("module")
        device_stub = types.ModuleType("module.device")
        with mock.patch.dict(
            sys.modules,
            {
                "module": module_stub,
                "module.device": device_stub,
                "module.device.platform": platform_module,
            },
        ):
            subject = types.SimpleNamespace(config=object())
            self.assertFalse(long_wait(subject))

        events.clear()

        class SuccessPlatform:
            def __init__(self, config, connect=False):
                self.emulator_instance = object()

            def emulator_start(self):
                events.append(("emulator_start", (), {}))
                return True

        platform_module.Platform = SuccessPlatform
        with mock.patch.dict(
            sys.modules,
            {
                "module": module_stub,
                "module.device": device_stub,
                "module.device.platform": platform_module,
            },
        ):
            subject = types.SimpleNamespace(config=object(), device=object())
            self.assertTrue(long_wait(subject))
        self.assertEqual(
            [event[0] for event in events],
            ["hr", "emulator_start", "info", "del_cached_property"],
        )

    def test_synthetic_loop_failure_limit_and_stop_request(self):
        utils_module = types.ModuleType("module.config.utils")
        utils_module.is_oobe_needed = lambda: False
        module_stub = types.ModuleType("module")
        config_stub = types.ModuleType("module.config")

        def deep_get(data, keys, default=None):
            return data.get(keys, default)

        def deep_set(data, keys, value):
            data[keys] = value

        with mock.patch.dict(
            sys.modules,
            {
                "module": module_stub,
                "module.config": config_stub,
                "module.config.utils": utils_module,
            },
        ):
            events = []
            logger = _Logger(events)

            def handle_notify(*args, **kwargs):
                events.append(("handle_notify", args, kwargs))

            def notify_webui(*args, **kwargs):
                events.append(("notify_webui", args, kwargs))

            loop = _load_method(
                "loop",
                {
                    "logger": logger,
                    "handle_notify": handle_notify,
                    "notify_webui": notify_webui,
                    "deep_get": deep_get,
                    "deep_set": deep_set,
                    "del_cached_property": lambda obj, name: events.append(
                        ("del_cached_property", (name,), {})
                    ),
                    "inflection": types.SimpleNamespace(
                        underscore=lambda value: "synthetic_task"
                    ),
                    "_get_task_display_name": lambda task: task,
                    "time": types.SimpleNamespace(
                        monotonic=lambda: 0,
                        sleep=lambda seconds: events.append(
                            ("sleep", (seconds,), {})
                        ),
                    ),
                },
            )

            config = types.SimpleNamespace(
                EmulatorManagement_ScheduledEmulatorRestart=False,
                Scheduler_PushNotification=False,
                Error_OnePushConfig={},
                Error_StrictRestart=False,
                Error_HandleError=False,
                cross_get=lambda **kwargs: False,
            )
            checker = types.SimpleNamespace(
                wait_until_available=lambda: events.append(
                    ("wait_until_available", (), {})
                ),
                is_recovered=lambda: False,
                check_now=lambda: events.append(("check_now", (), {})),
            )
            device = types.SimpleNamespace(
                config=None,
                stuck_record_clear=lambda: events.append(
                    ("stuck_record_clear", (), {})
                ),
                click_record_clear=lambda: events.append(
                    ("click_record_clear", (), {})
                ),
            )

            class FailureSubject:
                config_name = "SYNTHETIC"
                stop_event = None
                is_first_task = False
                failure_record = {"SyntheticTask": 2}
                consecutive_game_stuck = 0
                consecutive_adb_offline = 0
                last_emulator_restart_time = 0

                def __init__(self):
                    self.config = config
                    self.checker = checker
                    self.device = device

                def get_next_task(self):
                    events.append(("get_next_task", (), {}))
                    return "SyntheticTask"

                def run(self, command):
                    events.append(("run", (command,), {}))
                    return False

            with self.assertRaises(SystemExit) as raised:
                loop(FailureSubject())
            self.assertEqual(raised.exception.code, 1)
            self.assertEqual(FailureSubject.failure_record["SyntheticTask"], 3)
            names = [event[0] for event in events]
            self.assertLess(names.index("error_context"), names.index("handle_notify"))
            self.assertLess(names.index("handle_notify"), names.index("notify_webui"))
            self.assertLess(names.index("notify_webui"), names.index("warning"))
            notice = next(
                event for event in events if event[0] == "handle_notify"
            )
            self.assertIn("RequestHumanTakeover", notice[2]["content"])
            self.assertIn("`SyntheticTask`", notice[2]["content"])
            self.assertIn("3", notice[2]["content"])

            events.clear()
            stop_event = types.SimpleNamespace(is_set=lambda: True)

            class StopSubject:
                config_name = "SYNTHETIC"

                def __init__(self):
                    self.stop_event = stop_event

            self.assertIsNone(loop(StopSubject()))
            self.assertEqual(
                [event[1][0] for event in events if event[0] == "info"],
                [
                    "[Alas] Запуск цикла планировщика: SYNTHETIC",
                    "[Alas] Получен запрос на остановку",
                    "[Alas] [SYNTHETIC] Работа завершена. Причина: запрос на остановку",
                ],
            )
            self.assertNotIn("wait_until_available", [event[0] for event in events])

    def test_utf8_round_trip_and_no_replacement_character(self):
        raw = SOURCE_PATH.read_bytes()
        decoded = raw.decode("utf-8")
        self.assertEqual(decoded.encode("utf-8"), raw)
        self.assertNotIn("\ufffd", decoded)


if __name__ == "__main__":
    unittest.main()
