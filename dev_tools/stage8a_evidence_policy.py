from __future__ import annotations

from typing import Any


SCENARIO_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "adb_state": (
        "device", "offline", "no_device", "unauthorized",
        "unknown_host_service", "connection_reset", "read_timeout", "closed",
        "device_not_found", "more_than_one_device", "wrong_serial",
        "server_unavailable", "server_restart", "tcp_reconnect",
    ),
    "device_readiness": (
        "adb_state_device", "android_boot_incomplete", "package_unavailable",
        "screenshot_unavailable", "input_unavailable",
    ),
    "package_detection": (
        "configured_package", "auto_detection", "package_absent",
        "multiple_known_packages", "en_global_package", "unsupported_package",
        "remote_http_mode",
    ),
    "emulator_lifecycle": (
        "emulator_found", "emulator_not_found", "start_success", "start_timeout",
        "stop_success", "stop_timeout", "platform_unsupported", "dead_process",
        "command_nonzero", "remote_ssh_disabled", "windows", "macos",
    ),
    "screenshot_backend": (
        "init_success", "init_failure", "first_frame", "timeout",
        "truncated_frame", "empty_frame", "black_frame", "invalid_size",
        "rotated_frame", "stream_close", "fallback",
    ),
    "screenshot_backend_matrix": (
        "adb", "adb_nc", "uiautomator2", "ascreencap", "ascreencap_nc",
        "droidcast", "droidcast_raw", "scrcpy", "nemu_ipc", "ldopengl",
    ),
    "image_contract": (
        "numpy_ndarray", "bgr", "width_height", "normalization_1280x720",
        "orientation", "no_binary_log",
    ),
    "input_backend": (
        "click", "swipe", "key", "text", "empty_command",
        "invalid_orientation", "socket_close", "backend_unavailable",
        "timeout", "reconnect", "fallback",
    ),
    "input_backend_matrix": (
        "adb", "uiautomator2", "minitouch", "hermit", "maatouch", "nemu_ipc",
    ),
    "scrcpy": (
        "server_push", "server_startup", "video_stream", "control_stream",
        "initial_metadata", "stream_close", "version_mismatch", "fallback",
        "live_preview", "control_error", "device_messages",
    ),
    "uiautomator2": (
        "connect", "info", "click_timeout", "drag_timeout", "text_input",
        "screenshot", "service_init", "external_exception_context",
        "implicit_wait", "http_timeout", "long_click", "xpath_wait_get",
    ),
    "nemu_ldopengl": (
        "correct_emulator_family", "unsupported_emulator", "version_requirement",
        "dead_instance", "native_library_error", "screenshot_failure",
        "control_failure", "windows_only_fallback",
    ),
    "webui_live_control": (
        "start", "stop", "fallback", "resolution", "prebuffer", "click",
        "drag", "key", "text", "back", "system_key", "socket_close",
        "resource_cleanup", "no_user_text_leak",
    ),
}


RUNTIME_MATRIX_CLASS = (
    "tests.test_stage8a_runtime_scenario_matrix."
    "Stage8ARuntimeScenarioMatrixTests"
)


def runtime_fixture_test_id(category: str, scenario: str) -> str:
    return f"{RUNTIME_MATRIX_CLASS}.test_{category}__{scenario}"


# Supplemental source/semantic guards remain useful, but they are no longer counted as
# regression evidence. Every required scenario below has an executable fixture test.
SEMANTIC_TESTS: dict[str, str | None] = {
    "adb_state": (
        "tests.test_stage8a_scenario_contracts."
        "Stage8AScenarioContractTests.test_adb_state_contracts"
    ),
    "device_readiness": None,
    "package_detection": (
        "tests.test_stage8a_scenario_contracts."
        "Stage8AScenarioContractTests.test_package_detection_contracts"
    ),
    "emulator_lifecycle": (
        "tests.test_stage8a_scenario_contracts."
        "Stage8AScenarioContractTests.test_emulator_lifecycle_contracts"
    ),
    "screenshot_backend": (
        "tests.test_stage8a_scenario_contracts."
        "Stage8AScenarioContractTests.test_screenshot_backend_contracts"
    ),
    "screenshot_backend_matrix": None,
    "image_contract": None,
    "input_backend": (
        "tests.test_stage8a_scenario_contracts."
        "Stage8AScenarioContractTests.test_input_backend_contracts"
    ),
    "input_backend_matrix": None,
    "scrcpy": (
        "tests.test_stage8a_scenario_contracts."
        "Stage8AScenarioContractTests.test_scrcpy_contracts"
    ),
    "uiautomator2": (
        "tests.test_stage8a_scenario_contracts."
        "Stage8AScenarioContractTests.test_uiautomator2_timeout_contracts"
    ),
    "nemu_ldopengl": None,
    "webui_live_control": (
        "tests.test_stage8a_scenario_contracts."
        "Stage8AScenarioContractTests.test_webui_live_control_contracts"
    ),
}


FIXTURE_EVIDENCE: dict[tuple[str, str], str] = {
    (category, scenario): runtime_fixture_test_id(category, scenario)
    for category, scenarios in SCENARIO_REQUIREMENTS.items()
    for scenario in scenarios
}


BACKEND_CI_COVERAGE: tuple[dict[str, Any], ...] = (
    {
        "backend": "ADB",
        "ci_level": "CI_FIXTURE",
        "ci_evidence": [
            "target-explicit argv",
            "state/package/screenshot semantic matrix",
            "TCP reconnect fixtures",
        ],
        "external_acceptance_channel": "device-acceptance.json",
        "expected_external_level": "REAL_ACCEPTANCE",
        "limitations": "CI has no emulator; external report must match exact head.",
    },
    {
        "backend": "NemuIpc",
        "ci_level": "SEMANTIC_CONTRACT",
        "ci_evidence": ["init/error/fallback paths", "BGR screenshot contract"],
        "external_acceptance_channel": "device-acceptance.json",
        "expected_external_level": "REAL_ACCEPTANCE",
        "limitations": "Real coverage depends on configured MuMu backend.",
    },
    {
        "backend": "minitouch",
        "ci_level": "CI_FIXTURE",
        "ci_evidence": ["handshake/no-touch fixture", "forward cleanup"],
        "external_acceptance_channel": "device-acceptance.json",
        "expected_external_level": "REAL_ACCEPTANCE_HANDSHAKE",
        "limitations": "External probe intentionally sends no touch command.",
    },
    {
        "backend": "scrcpy",
        "ci_level": "CI_FIXTURE",
        "ci_evidence": ["v1.20 handshake", "video/control semantic contracts", "fallback fixtures"],
        "external_acceptance_channel": "device-acceptance.json",
        "expected_external_level": "HANDSHAKE_OR_REAL_FRAME",
        "limitations": "Static surface may produce no first H.264 frame; fallback is separately verified.",
    },
    {
        "backend": "DroidCast",
        "ci_level": "SEMANTIC_CONTRACT",
        "ci_evidence": ["retry/error/image payload contracts"],
        "external_acceptance_channel": None,
        "expected_external_level": None,
        "limitations": "No configured real-device backend in this acceptance cycle.",
    },
    {
        "backend": "aScreenCap",
        "ci_level": "SEMANTIC_CONTRACT",
        "ci_evidence": ["header/truncation/empty/fallback contracts"],
        "external_acceptance_channel": None,
        "expected_external_level": None,
        "limitations": "No compatible real-device binary exercised.",
    },
    {
        "backend": "LDOpenGL",
        "ci_level": "SEMANTIC_CONTRACT",
        "ci_evidence": ["init/error/invalid-instance contracts"],
        "external_acceptance_channel": None,
        "expected_external_level": None,
        "limitations": "Windows-only DLL not present in CI.",
    },
    {
        "backend": "MaaTouch",
        "ci_level": "SEMANTIC_CONTRACT",
        "ci_evidence": ["timeout/socket/orientation/reconnect contracts"],
        "external_acceptance_channel": None,
        "expected_external_level": None,
        "limitations": "Configured backend was minitouch.",
    },
    {
        "backend": "Hermit",
        "ci_level": "SEMANTIC_CONTRACT",
        "ci_evidence": ["HTTP/accessibility/retry contracts"],
        "external_acceptance_channel": None,
        "expected_external_level": None,
        "limitations": "Requires Android accessibility service and was not configured.",
    },
    {
        "backend": "WSA",
        "ci_level": "SEMANTIC_CONTRACT",
        "ci_evidence": ["ADB/display/app lifecycle contracts"],
        "external_acceptance_channel": None,
        "expected_external_level": None,
        "limitations": "Windows Subsystem for Android was not available.",
    },
)


SECURITY_REQUIREMENTS = (
    {"id": "command_injection", "test": "tests.test_stage8a_security_review.Stage8ASecurityReviewTests.test_subprocess_calls_do_not_enable_shell"},
    {"id": "shell_quoting", "test": "tests.test_stage8a_security_review.Stage8ASecurityReviewTests.test_subprocess_calls_do_not_enable_shell"},
    {"id": "serial_injection", "test": "tests.test_stage8a_device_acceptance.Stage8ADeviceAcceptanceTests.test_network_serial_detection_requires_valid_host_port"},
    {"id": "unsafe_subprocess_logging", "test": "tests.test_stage8a_security_review.Stage8ASecurityReviewTests.test_external_diagnostics_are_sanitized_before_report"},
    {"id": "ssh_credential_leakage", "test": "tests.test_stage8a_device_acceptance.Stage8ADeviceAcceptanceTests.test_sanitized_text_redacts_credentials_hosts_paths_and_html"},
    {"id": "raw_url_credentials", "test": "tests.test_stage8a_device_acceptance.Stage8ADeviceAcceptanceTests.test_sanitized_text_redacts_credentials_hosts_paths_and_html"},
    {"id": "device_serial_leakage", "test": "tests.test_stage8a_device_acceptance.Stage8ADeviceAcceptanceTests.test_sanitized_text_removes_serial"},
    {"id": "clipboard_leakage", "test": "tests.test_stage8a_security_review.Stage8ASecurityReviewTests.test_acceptance_forbids_clipboard_and_user_text"},
    {"id": "typed_text_leakage", "test": "tests.test_stage8a_security_review.Stage8ASecurityReviewTests.test_acceptance_forbids_clipboard_and_user_text"},
    {"id": "screenshot_leakage", "test": "tests.test_stage8a_device_acceptance.Stage8ADeviceAcceptanceTests.test_binary_command_evidence_records_only_byte_counts"},
    {"id": "binary_log_flooding", "test": "tests.test_stage8a_binary_log_audit_arguments.Stage8ABinaryLogAuditArgumentsTests.test_lazy_formatting_second_argument_is_checked"},
    {"id": "html_websocket_injection", "test": "tests.test_stage8a_security_review.Stage8ASecurityReviewTests.test_websocket_errors_are_json_encoded"},
    {"id": "ansi_control_chars", "test": "tests.test_stage8a_device_acceptance.Stage8ADeviceAcceptanceTests.test_sanitized_text_bounds_external_output_and_removes_controls"},
    {"id": "newline_log_forging", "test": "tests.test_stage8a_device_acceptance.Stage8ADeviceAcceptanceTests.test_sanitized_text_bounds_external_output_and_removes_controls"},
    {"id": "unbounded_external_output", "test": "tests.test_stage8a_device_acceptance.Stage8ADeviceAcceptanceTests.test_sanitized_text_bounds_external_output_and_removes_controls"},
    {"id": "exception_local_leakage", "test": "tests.test_stage8a_device_acceptance.Stage8ADeviceAcceptanceTests.test_failure_report_masks_config_serial_and_adb_path"},
    {"id": "temporary_paths", "test": "tests.test_stage8a_security_review.Stage8ASecurityReviewTests.test_temporary_screenshot_is_deleted"},
    {"id": "port_exposure", "test": "tests.test_stage8a_security_review.Stage8ASecurityReviewTests.test_forwarding_remains_target_scoped"},
    {"id": "live_preview_authorization", "test": "tests.test_stage8a_security_review.Stage8ASecurityReviewTests.test_live_routes_keep_auth_guard"},
)


EXTERNAL_CONTRACTS = (
    {
        "dependency": "adbutils",
        "pinned": "0.11.0",
        "test": (
            "tests.test_stage8a_external_contracts."
            "Stage8AExternalContractTests.test_adb_target_selection_is_explicit_in_fork"
        ),
    },
    {
        "dependency": "uiautomator2",
        "pinned": "2.16.17",
        "test": (
            "tests.test_stage8a_external_contracts."
            "Stage8AExternalContractTests."
            "test_uiautomator2_project_calls_keep_timeout_layers_distinct"
        ),
    },
    {
        "dependency": "scrcpy-server",
        "pinned": "1.20",
        "test": (
            "tests.test_stage8a_external_contracts."
            "Stage8AExternalContractTests.test_bundled_scrcpy_server_version_is_explicit"
        ),
    },
)


def scenario_evidence() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for category, scenarios in SCENARIO_REQUIREMENTS.items():
        for scenario in scenarios:
            fixture = FIXTURE_EVIDENCE.get((category, scenario))
            if not fixture:
                raise ValueError(
                    f"Missing executable fixture evidence for {category}/{scenario}"
                )
            rows.append(
                {
                    "category": category,
                    "scenario": scenario,
                    "semantic_test": SEMANTIC_TESTS.get(category),
                    "fixture_test": fixture,
                    "evidence_level": "CI_FIXTURE",
                    "limitations": (
                        "Synthetic/recorded fixture executes the production branch with "
                        "mocked external transport or native dependency. Physical backend "
                        "availability remains a separate exact-head acceptance channel."
                    ),
                }
            )
    return rows
