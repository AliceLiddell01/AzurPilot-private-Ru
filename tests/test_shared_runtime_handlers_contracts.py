from __future__ import annotations

from pathlib import Path

from module.logger import sanitize_traceback_text


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_gui_keeps_process_socket_contract():
    source = _source("gui.py")
    for token in (
        "WEBUI_READY_TIMEOUT = 120",
        "WEBUI_START_RETRY_LIMIT = 3",
        "WEBUI_RUNTIME_RETRY_LIMIT = 3",
        "WEBUI_STABLE_RUNTIME = 60",
        "DEPENDENCY_SYNC_START_RETRY_LIMIT = 3",
        "socket.AF_INET",
        "socket.AF_INET6",
        "socket.SO_REUSEADDR",
        "socket.IPV6_V6ONLY",
        '"0.0.0.0"',
        '"::"',
        '"[::]"',
        "uvicorn.Config",
        "ready_event.set()",
        "process.terminate()",
        "process.kill()",
        "process.join(timeout=timeout)",
    ):
        assert token in source


def test_logger_security_redaction_and_rendering_contract_is_intact():
    source = _source("module/logger.py") + _source("module/logging_core.py")
    for token in (
        "_SENSITIVE_NAME_RE = re.compile(",
        "_URL_USERINFO_RE = re.compile(",
        "_SENSITIVE_QUERY_RE = re.compile(",
        "_SENSITIVE_ASSIGNMENT_RE = re.compile(",
        "_ANSI_ESCAPE_RE = re.compile(",
        "_UNSAFE_CONTROL_RE = re.compile(",
        "_BIDI_CONTROL_RE = re.compile(",
        '"<PROJECT_ROOT>"',
        '"<USER_HOME>"',
        'Node(value_repr="\'<скрыто>\'")',
        "sanitize_rich_traceback(traceback)",
        "tracebacks_show_locals=False",
        "tracebacks_extra_lines=3",
        "tracebacks_extra_lines=2",
        "RichRenderableHandler",
        "HTMLConsole",
        "TimedRotatingFileHandler",
        "logger.setLevel(logging.DEBUG)",
        "datefmt='%Y-%m-%d %H:%M:%S'",
        "datefmt='%H:%M:%S'",
    ):
        assert token in source
    assert "tracebacks_show_locals=True" not in source

    raw = (
        "\x1b[31mhttps://alice:secret@example.test/path?token=abc "
        "password=hidden\u202e"
    )
    sanitized = sanitize_traceback_text(raw)
    assert "\x1b" not in sanitized
    assert "\u202e" not in sanitized
    assert "alice:secret" not in sanitized
    assert "token=abc" not in sanitized
    assert "password=hidden" not in sanitized
    assert "https://***@example.test/path?token=***" in sanitized
    assert "password=***" in sanitized


def test_server_checker_http_state_and_retry_constants_are_intact():
    source = _source("module/server_checker.py")
    for token in (
        "deque(maxlen=2)",
        "Timer(0)",
        "http://sc.shiratama.cn",
        "/server/get_state",
        "/server/get_all_state",
        "/server/list",
        "requests.Session()",
        "session.trust_env = False",
        "timeout=15",
        "http://www.msftconnecttest.com/connecttest.txt",
        "Microsoft Connect Test",
        "allow_redirects=False",
        "timeout=5",
        "if self._expired > 3:",
        "if self._timer.limit < 600:",
        "self._timer.limit += 120",
        "for _ in range(3):",
        "self._state.extend(last)",
        "self._server = 'disabled'",
    ):
        assert token in source


def test_daemon_benchmark_algorithm_and_identifiers_are_intact():
    source = _source("module/daemon/benchmark.py")
    for token in (
        "TEST_TOTAL = 15",
        "TEST_BEST = int(TEST_TOTAL * 0.8)",
        "np.mean(np.sort(record)[:self.TEST_BEST])",
        "fastest_screenshot = 'ADB_nc'",
        "fastest_click = 'minitouch'",
        "if 'MaaTouch' in click and fastest[0] == 'minitouch':",
        "self.TEST_TOTAL = 3",
        "self.TEST_BEST = 1",
        "return fastest_screenshot, fastest_click",
    ):
        assert token in source


def test_game_manager_keeps_device_call_order():
    game_manager = _source("module/daemon/game_manager.py")
    assert game_manager.index("self.device.app_stop()") < game_manager.index(
        "if self.config.GameManager_AutoRestart:"
    )
    assert "LoginHandler(config=self.config, device=self.device).app_restart()" in game_manager


def test_ocr_benchmark_models_order_and_report_contract_are_intact():
    source = _source("module/daemon/ocr_benchmark.py")
    expected_order = [
        '"alocr_en_900k"',
        '"azur_lane_v6_6"',
        '"azur_lane_v6_5"',
        '"ppocr_v6"',
        '"alocr_en_v2_6"',
        '"alocr_en_v2_0"',
        '"alocr_en_v1_0"',
    ]
    positions = [source.index(token) for token in expected_order]
    assert positions == sorted(positions)
    for token in (
        'REPORT_PATH = Path("artifacts/ocr/english-model-benchmark.json")',
        "SPEED_ITERATIONS = 100",
        "WARMUP_ITERATIONS = 3",
        "MAX_REPORTED_MISMATCHES = 10",
        'backend_override="onnxruntime"',
        'backend_override="ncnn"',
        'return "cpu"',
    ):
        assert token in source


def test_screenshot_benchmark_schema_navigation_and_intervals_are_intact():
    source = _source("module/daemon/screenshot_interval_benchmark.py")
    for token in (
        "DEFAULT_NORMAL_INTERVALS",
        "DEFAULT_COMBAT_INTERVALS",
        "SCRCPY_FORCED_INTERVAL",
        "duration_per_candidate_s = 2.0",
        "warmup_frames = 2",
        "transition_timeout_s = 25.0",
        "simulation_button_timeout_s = 8.0",
        "simulation_button_min_score = 0.35",
        'if "SIMULATION" not in compact:',
        'description="Current Target"',
        '"BATTLE_SIMULATION"',
        '"normal_context": "campaign_page"',
        '"combat_context": "meta_current_target_battle_simulation"',
        '"automatic_config_write": False',
        "DEFAULT_REPORT.write_text(",
        "_write_markdown(report, DEFAULT_MARKDOWN_REPORT)",
    ):
        assert token in source


def test_os_daemon_operation_siren_flow_is_unchanged():
    source = _source("module/daemon/os_daemon.py")
    for token in (
        "self.config.merge(OSConfig())",
        "self.config.override(HOMO_EDGE_DETECT=False)",
        "while 1:",
        "self.device.screenshot()",
        "self.combat_status(expected_end='no_searching')",
        "except (CampaignEnd, ContinuousCombat):",
        "self.handle_map_event()",
        "self.port_enter()",
        "self.port_dock_repair()",
        "self.port_quit()",
        "self.interval_reset(PORT_ENTER)",
        "self.click_nearest_object()",
        "return True",
    ):
        assert token in source
