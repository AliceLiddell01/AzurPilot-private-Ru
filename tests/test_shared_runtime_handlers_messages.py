from __future__ import annotations

from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests

from module.base.timer import Timer
from module.daemon.benchmark import Benchmark
from module.exception import ScriptError
from module.server_checker import ServerChecker


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _bare_checker(server: str = "test-server") -> ServerChecker:
    checker = object.__new__(ServerChecker)
    checker._base = "http://sc.shiratama.cn"
    checker._api = {
        "get_state": "/server/get_state",
        "get_all_state": "/server/get_all_state",
        "list": "/server/list",
    }
    checker._server = server
    checker._state = deque(maxlen=2)
    checker._timestamp = 0
    checker._expired = 0
    checker._timer = Timer(0)
    checker._recover = False
    checker._retry = False
    return checker


def _response(status_code: int, *, payload=None, text: str = "") -> Mock:
    response = Mock()
    response.status_code = status_code
    response.text = text
    if isinstance(payload, BaseException):
        response.json.side_effect = payload
    else:
        response.json.return_value = payload
    return response


def test_runtime_messages_are_russian_and_technical_values_are_preserved():
    benchmark = _source("module/daemon/benchmark.py")
    game_manager = _source("module/daemon/game_manager.py")
    os_daemon = _source("module/daemon/os_daemon.py")
    uncensored = _source("module/daemon/uncensored.py")
    ocr = _source("module/daemon/ocr_benchmark.py")
    screenshot = _source("module/daemon/screenshot_interval_benchmark.py")
    server = _source("module/server_checker.py")
    logger = _source("module/logger.py")
    gui = _source("gui.py")

    assert "Тестируемая функция" in benchmark
    assert "Результаты бенчмарка" in benchmark
    assert "Рекомендуемый метод снимка экрана" in benchmark
    assert "Экстремально быстро" in benchmark
    assert "Время" in benchmark and "Скорость" in benchmark
    assert "return 'Failed'" in benchmark
    for token in (
        "'ADB'",
        "'ADB_nc'",
        "'uiautomator2'",
        "'aScreenCap'",
        "'DroidCast'",
        "'DroidCast_raw'",
        "'minitouch'",
        "'MaaTouch'",
        "f'sdk_ver: {sdk}'",
    ):
        assert token in benchmark

    assert "Принудительная остановка Azur Lane" in game_manager
    assert "[Daemon-Operation Siren] Ремонт в порту завершён" in os_daemon
    assert "[Daemon-Без цензуры] Команда: {command}" in uncensored
    assert "['push', 'files', f'/sdcard/Android/data/{self.device.package}']" in uncensored

    assert "[Бенчмарк OCR]" in ocr
    assert "[Бенчмарк снимков экрана]" in screenshot
    for token in (
        '"azur_lane"',
        '"onnxruntime"',
        '"ncnn"',
        '"sets_num"',
        'REPORT_PATH = Path("artifacts/ocr/english-model-benchmark.json")',
    ):
        assert token in ocr
    for token in (
        '"SIMULATION"',
        '"BATTLE_SIMULATION"',
        '"page_campaign"',
        '"meta_current_target_battle_simulation"',
        'DEFAULT_REPORT',
        'DEFAULT_MARKDOWN_REPORT',
    ):
        assert token in screenshot

    assert "[Проверка состояния сервера]" in server
    assert "Проверка состояния сервера повторится через" in server
    assert "Ответ: {resp.text}" in server
    assert 'Ответ "{resp.text}" не является корректным JSON.' in server
    assert "http://sc.shiratama.cn" in server
    assert "https://www.baidu.com" in server

    assert "Программа вызвала исключение" in logger
    assert "[bold]<<< {title} >>>[/bold]" in logger
    assert "logger.info(r'大括号 { [ ( ) ] }')" in logger
    assert "logger.info(r'True, False, None')" in logger
    assert 'raise Exception("Exception")' in logger

    assert 'logger.attr("Electron", args.electron)' in gui
    assert "WEBUI_READY_TIMEOUT = 120" in gui
    assert "WEBUI_START_RETRY_LIMIT = 3" in gui
    assert "WEBUI_RUNTIME_RETRY_LIMIT = 3" in gui


def test_benchmark_ratings_change_only_human_facing_text():
    assert Benchmark.evaluate_screenshot(0.020).plain == "Экстремально быстро"
    assert Benchmark.evaluate_screenshot(0.150).plain == "Весьма быстро"
    assert Benchmark.evaluate_screenshot(1.500).plain == "Критически медленно"
    assert Benchmark.evaluate_click(0.050).plain == "Быстро"
    assert Benchmark.evaluate_click(0.500).plain == "Очень медленно"
    assert Benchmark.evaluate_screenshot("Failed").plain == "Failed"


def test_server_checker_available_and_maintenance_keep_http_contract():
    checker = _bare_checker()
    session = Mock()
    session.trust_env = True
    session.post.return_value = _response(
        200,
        payload={"state": 0, "last_update": 11},
    )

    with patch("module.server_checker.requests.Session", return_value=session):
        checker._load_server()

    assert checker._state[-1] is True
    assert checker._timestamp == 11
    assert session.trust_env is False
    session.post.assert_called_once_with(
        url="http://sc.shiratama.cn/server/get_state",
        params={"server_name": "test-server"},
        timeout=15,
    )

    checker = _bare_checker()
    session = Mock()
    session.post.return_value = _response(
        200,
        payload={"state": 1, "last_update": 12},
    )
    with patch("module.server_checker.requests.Session", return_value=session):
        checker._load_server()
    assert checker._state[-1] is False


def test_server_checker_stale_timestamp_and_local_404_paths_are_unchanged():
    checker = _bare_checker()
    checker._timestamp = 20
    checker._expired = 3
    session = Mock()
    session.post.return_value = _response(
        200,
        payload={"state": 0, "last_update": 20},
    )
    with patch("module.server_checker.requests.Session", return_value=session):
        checker._load_server()
    assert checker._expired == 4

    checker = _bare_checker()
    checker._server_in_local_list = Mock(return_value=True)
    session = Mock()
    session.post.return_value = _response(404, text="raw-404")
    with patch("module.server_checker.requests.Session", return_value=session):
        checker._load_server()
    assert checker._state[-1] is True

    checker = _bare_checker()
    checker._server_in_local_list = Mock(return_value=False)
    session = Mock()
    session.post.return_value = _response(404, text="raw-404")
    with patch("module.server_checker.requests.Session", return_value=session):
        with pytest.raises(ScriptError, match="не существует"):
            checker._load_server()
    assert checker._state[-1] is False


def test_server_checker_preserves_raw_http_and_json_payloads():
    checker = _bare_checker()
    raw_body = "RAW_EXTERNAL_BODY::do-not-normalize"
    session = Mock()
    session.post.return_value = _response(503, text=raw_body)
    with patch("module.server_checker.requests.Session", return_value=session):
        with pytest.raises(ScriptError) as caught:
            checker._load_server()
    assert "503" in str(caught.value)
    assert raw_body in str(caught.value)

    checker = _bare_checker()
    raw_json_body = "RAW_INVALID_JSON::{x}"
    decode_error = __import__("json").JSONDecodeError("bad", raw_json_body, 0)
    session = Mock()
    session.post.return_value = _response(
        200,
        payload=decode_error,
        text=raw_json_body,
    )
    with patch("module.server_checker.requests.Session", return_value=session):
        with pytest.raises(ScriptError) as caught:
            checker._load_server()
    assert raw_json_body in str(caught.value)
    assert checker._state[-1] is False


def test_server_checker_connection_failure_and_retry_escalation_are_unchanged():
    checker = _bare_checker()
    checker._retry = True
    session = Mock()
    session.post.side_effect = requests.exceptions.ConnectionError("synthetic")
    with patch("module.server_checker.requests.Session", return_value=session):
        checker._load_server()
    assert checker._state[-1] is False

    checker = _bare_checker()
    checker._state.append(False)
    checker._load_server = Mock(side_effect=lambda: checker._state.append(False))
    checker._timer = SimpleNamespace(limit=0, reset=Mock())
    checker.check_now()
    assert checker._timer.limit == 120
    checker._timer.reset.assert_called_once_with()


def test_server_checker_fast_retry_keeps_three_attempt_limit_and_network_probe():
    checker = _bare_checker()
    checker._state.append(False)
    session = Mock()
    session.get.return_value = Mock()

    calls = 0

    def fail_load():
        nonlocal calls
        calls += 1
        checker._state.clear()
        checker._state.append(False)

    checker._load_server = fail_load
    with patch("module.server_checker.requests.Session", return_value=session):
        assert checker.fast_retry() is False
    assert calls == 3
    session.get.assert_called_once_with("https://www.baidu.com", timeout=5)
    assert checker._retry is False

    checker = _bare_checker()
    checker._state.append(False)
    session = Mock()
    session.get.side_effect = requests.exceptions.ConnectionError("offline")
    checker._load_server = Mock()
    with patch("module.server_checker.requests.Session", return_value=session):
        assert checker.fast_retry() is False
    checker._load_server.assert_not_called()
    assert checker._retry is False
