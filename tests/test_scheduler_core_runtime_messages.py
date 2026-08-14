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
ALAS = ROOT / "alas.py"


def src() -> str:
    return ALAS.read_text(encoding="utf-8")


def node(name: str) -> ast.FunctionDef:
    tree = ast.parse(src(), filename="alas.py")
    cls = next(x for x in tree.body if isinstance(x, ast.ClassDef) and x.name == "AzurLaneAutoScript")
    return next(x for x in cls.body if isinstance(x, ast.FunctionDef) and x.name == name)


def load(name: str, env: dict):
    fn = copy.deepcopy(node(name))
    fn.decorator_list = []
    mod = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(mod)
    ns = dict(env)
    exec(compile(mod, f"<alas.py:{name}>", "exec"), ns)
    return ns[name]


class Log:
    def __init__(self, events):
        self.events = events

    def __getattr__(self, name):
        return lambda *a, **kw: self.events.append((name, a, kw))


class TaskEnd(Exception): pass
class GameNotRunningError(Exception): pass
class GameStuckError(Exception): pass
class GameTooManyClickError(Exception): pass
class GameBugError(Exception): pass
class GamePageUnknownError(Exception): pass
class ScriptError(Exception): pass
class EmulatorNotRunningError(Exception): pass
class RequestHumanTakeover(Exception): pass
class AutoSearchSetError(Exception): pass


ERRORS = {
    "TaskEnd": TaskEnd,
    "GameNotRunningError": GameNotRunningError,
    "GameStuckError": GameStuckError,
    "GameTooManyClickError": GameTooManyClickError,
    "GameBugError": GameBugError,
    "GamePageUnknownError": GamePageUnknownError,
    "ScriptError": ScriptError,
    "EmulatorNotRunningError": EmulatorNotRunningError,
    "RequestHumanTakeover": RequestHumanTakeover,
    "AutoSearchSetError": AutoSearchSetError,
}


class SchedulerCoreRuntimeMessages(unittest.TestCase):
    def env(self, events):
        rec = lambda name: lambda *a, **kw: events.append((name, a, kw))
        return {
            "logger": Log(events),
            "handle_notify": rec("handle_notify"),
            "notify_webui": rec("notify_webui"),
            **ERRORS,
        }

    def subject(self, events, error):
        cfg = types.SimpleNamespace(
            Error_OnePushConfig={},
            Error_GameStuckRestart=False,
            Error_GameStuckThreshold=3,
            task_call=lambda task: events.append(("task_call", (task,), {})),
        )
        dev = types.SimpleNamespace(
            screenshot=lambda: events.append(("screenshot", (), {})),
            sleep=lambda seconds: events.append(("device_sleep", (seconds,), {})),
            package="com.YoStarEN.AzurLane",
        )

        class Subject:
            config_name = "SYNTHETIC"
            consecutive_game_stuck = 0

            def __init__(self):
                self.config = cfg
                self.device = dev

            def synthetic(self):
                raise error("RAW")

            def save_error_log(self):
                events.append(("save_error_log", (), {}))

            def _check_sensitive_exit(self, command, exc):
                events.append(("_check_sensitive_exit", (command, exc), {}))
                return False

            def _try_restart_game(self):
                events.append(("_try_restart_game", (), {}))
                return True

            def _try_restart_emulator(self):
                events.append(("_try_restart_emulator", (), {}))
                return True

        return Subject()

    def test_static_contracts(self):
        tree = ast.parse(src(), filename="alas.py")
        docs = set()
        for owner in ast.walk(tree):
            body = getattr(owner, "body", None)
            if body and isinstance(body, list) and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    docs.add(id(value))
        cjk = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
        self.assertEqual(
            [
                (x.lineno, x.value)
                for x in ast.walk(tree)
                if isinstance(x, ast.Constant)
                and isinstance(x.value, str)
                and id(x) not in docs
                and cjk.search(x.value)
            ],
            [],
        )
        text = src()
        self.assertNotIn("喵", text)
        for token in (
            "AzurPilot", "Alas", "ADB", "Restart", "GameNotRunningError",
            "GameStuckError", "GameTooManyClickError",
            "EmulatorNotRunningError", "RequestHumanTakeover",
            "ScriptError", "TaskEnd", "'recoverable'",
        ):
            self.assertIn(token, text)

        run = node("run")
        handlers = []
        for h in next(x for x in run.body if isinstance(x, ast.Try)).handlers:
            if isinstance(h.type, ast.Name):
                handlers.append(h.type.id)
            else:
                handlers.append(tuple(x.id for x in h.type.elts))
        self.assertEqual(
            handlers,
            [
                "TaskEnd", "GameNotRunningError",
                ("GameStuckError", "GameTooManyClickError"),
                "GameBugError", "GamePageUnknownError", "ScriptError",
                "EmulatorNotRunningError", "RequestHumanTakeover",
                "AutoSearchSetError", "Exception",
            ],
        )
        for method, values in {
            "_try_restart_emulator": ("self.consecutive_adb_offline > limit", "time.sleep(5)"),
            "run": ("self.config.task_call('Restart')", "self._try_restart_game()", "return 'recoverable'", "return False"),
            "loop": ("MAX_GLOBAL_FAILURES = 3", "RESTART_DELAY = 20", "LONG_WAIT = 300", "failed >= 3"),
            "wait_until": ("time.sleep(5)", "exit(0)"),
        }.items():
            body = ast.get_source_segment(src(), node(method))
            for value in values:
                self.assertIn(value, body)

    def test_run_paths_and_notifications(self):
        cases = (
            (GameNotRunningError, ["screenshot", "error_context", "_check_sensitive_exit", "handle_notify", "notify_webui", "task_call"]),
            (GameStuckError, ["screenshot", "error_context", "save_error_log", "_check_sensitive_exit", "warning", "warning", "handle_notify", "_try_restart_game", "info", "notify_webui"]),
            (GameTooManyClickError, ["screenshot", "error_context", "save_error_log", "_check_sensitive_exit", "warning", "warning", "handle_notify", "_try_restart_game", "info", "notify_webui"]),
            (EmulatorNotRunningError, ["screenshot", "error_context", "save_error_log", "_check_sensitive_exit", "_try_restart_emulator", "task_call", "handle_notify", "notify_webui"]),
        )
        for error, expected in cases:
            with self.subTest(error=error.__name__):
                events = []
                result = load("run", self.env(events))(self.subject(events, error), "synthetic")
                self.assertEqual(result, "recoverable")
                self.assertEqual([x[0] for x in events], expected)
                if error in {GameNotRunningError, EmulatorNotRunningError}:
                    self.assertEqual(next(x for x in events if x[0] == "task_call")[1], ("Restart",))
                else:
                    self.assertNotIn("task_call", [x[0] for x in events])
                    self.assertNotIn("_try_restart_emulator", [x[0] for x in events])
                    self.assertNotIn("device_sleep", [x[0] for x in events])
                notice = "".join(
                    x[2].get("title", "") + x[2].get("content", "")
                    for x in events if x[0] in {"handle_notify", "notify_webui"}
                )
                self.assertNotIn("喵", notice)

        for error, expected in (
            (RequestHumanTakeover, ["screenshot", "error_context", "handle_notify", "notify_webui"]),
            (RuntimeError, ["screenshot", "exception_context", "save_error_log", "handle_notify", "notify_webui"]),
        ):
            events = []
            method = load("run", self.env(events))
            if error is RequestHumanTakeover:
                with self.assertRaises(SystemExit) as raised:
                    method(self.subject(events, error), "synthetic")
                self.assertEqual(raised.exception.code, 1)
            else:
                with self.assertRaisesRegex(RuntimeError, "RAW"):
                    method(self.subject(events, error), "synthetic")
            self.assertEqual([x[0] for x in events], expected)

        events = []
        self.assertTrue(load("run", self.env(events))(self.subject(events, TaskEnd), "synthetic"))

    def test_sensitive_restart_and_long_wait(self):
        events = []
        env = self.env(events)
        env["inflection"] = types.SimpleNamespace(camelize=lambda _: "Synthetic")
        sensitive = load("_check_sensitive_exit", env)
        cfg = types.SimpleNamespace(Error_OnePushConfig={}, cross_get=lambda **_: True)
        with self.assertRaises(SystemExit) as raised:
            sensitive(types.SimpleNamespace(config=cfg, config_name="SYNTHETIC"), "synthetic", RuntimeError("RAW_ERROR"))
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual([x[0] for x in events], ["error_context", "handle_notify", "notify_webui"])
        for item in events[1:]:
            self.assertTrue(item[2]["content"].endswith("\nRAW_ERROR"))

        events.clear()
        restart = load(
            "_try_restart_emulator",
            {
                "logger": Log(events),
                "time": types.SimpleNamespace(sleep=lambda n: events.append(("sleep", (n,), {}))),
                "del_cached_property": lambda obj, name: events.append(("del_cached_property", (name,), {})),
            },
        )
        dev = types.SimpleNamespace(
            emulator_stop=lambda: events.append(("emulator_stop", (), {})),
            emulator_start=lambda: events.append(("emulator_start", (), {})),
        )
        obj = types.SimpleNamespace(
            config=types.SimpleNamespace(Error_AdbOfflineRestart=True, Error_AdbOfflineThreshold=2),
            consecutive_adb_offline=0,
            device=dev,
        )
        self.assertTrue(restart(obj))
        names = [x[0] for x in events]
        self.assertLess(names.index("emulator_stop"), names.index("sleep"))
        self.assertLess(names.index("sleep"), names.index("emulator_start"))
        self.assertEqual(next(x for x in events if x[0] == "sleep")[1], (5,))

        events.clear()
        long_wait = load(
            "_start_emulator_after_long_wait",
            {
                "logger": Log(events),
                "del_cached_property": lambda obj, name: events.append(("del_cached_property", (name,), {})),
            },
        )
        platform = types.ModuleType("module.device.platform")

        class Platform:
            def __init__(self, config, connect=False):
                self.emulator_instance = object()

            def emulator_start(self):
                events.append(("emulator_start", (), {}))
                return True

        platform.Platform = Platform
        with mock.patch.dict(sys.modules, {
            "module": types.ModuleType("module"),
            "module.device": types.ModuleType("module.device"),
            "module.device.platform": platform,
        }):
            self.assertTrue(long_wait(types.SimpleNamespace(config=object(), device=object())))

    def test_loop_failure_limit_stop_and_utf8(self):
        utils = types.ModuleType("module.config.utils")
        utils.is_oobe_needed = lambda: False
        modules = {
            "module": types.ModuleType("module"),
            "module.config": types.ModuleType("module.config"),
            "module.config.utils": utils,
        }
        events = []
        env = self.env(events)
        env.update({
            "deep_get": lambda data, keys, default=None: data.get(keys, default),
            "deep_set": lambda data, keys, value: data.__setitem__(keys, value),
            "del_cached_property": lambda obj, name: None,
            "inflection": types.SimpleNamespace(underscore=lambda _: "synthetic"),
            "_get_task_display_name": lambda task: task,
            "time": types.SimpleNamespace(monotonic=lambda: 0, sleep=lambda _: None),
        })
        with mock.patch.dict(sys.modules, modules):
            loop = load("loop", env)
            cfg = types.SimpleNamespace(
                EmulatorManagement_ScheduledEmulatorRestart=False,
                Scheduler_PushNotification=False,
                Error_OnePushConfig={},
                Error_StrictRestart=False,
                Error_HandleError=False,
                cross_get=lambda **_: False,
            )
            checker = types.SimpleNamespace(wait_until_available=lambda: None, is_recovered=lambda: False)
            dev = types.SimpleNamespace(config=None, stuck_record_clear=lambda: None, click_record_clear=lambda: None)

            class Failure:
                config_name = "SYNTHETIC"
                stop_event = None
                is_first_task = False
                failure_record = {"Synthetic": 2}
                consecutive_game_stuck = 0
                consecutive_adb_offline = 0
                last_emulator_restart_time = 0

                def __init__(self):
                    self.config, self.checker, self.device = cfg, checker, dev

                def get_next_task(self):
                    return "Synthetic"

                def run(self, command):
                    return False

            with self.assertRaises(SystemExit) as raised:
                loop(Failure())
            self.assertEqual(raised.exception.code, 1)
            self.assertEqual(Failure.failure_record["Synthetic"], 3)
            names = [x[0] for x in events]
            self.assertLess(names.index("error_context"), names.index("handle_notify"))
            self.assertLess(names.index("handle_notify"), names.index("notify_webui"))

            events.clear()

            class Stop:
                config_name = "SYNTHETIC"
                stop_event = types.SimpleNamespace(is_set=lambda: True)

            self.assertIsNone(loop(Stop()))
            self.assertEqual(
                [x[1][0] for x in events if x[0] == "info"],
                [
                    "[Alas] Запуск цикла планировщика: SYNTHETIC",
                    "[Alas] Получен запрос на остановку",
                    "[Alas] [SYNTHETIC] Работа завершена. Причина: запрос на остановку",
                ],
            )

        raw = ALAS.read_bytes()
        self.assertEqual(raw.decode("utf-8").encode("utf-8"), raw)
        self.assertNotIn("\ufffd", raw.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
