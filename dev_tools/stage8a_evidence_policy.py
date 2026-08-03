from __future__ import annotations

from typing import Any


SCENARIO_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "adb_state": (
        "device",
        "offline",
        "no_device",
        "unauthorized",
        "unknown_host_service",
        "connection_reset",
        "read_timeout",
        "closed",
        "device_not_found",
        "more_than_one_device",
        "wrong_serial",
        "server_unavailable",
        "server_restart",
        "tcp_reconnect",
    ),
    "package_detection": (
        "configured_package",
        "auto_detection",
        "package_absent",
        "multiple_known_packages",
        "en_global_package",
        "unsupported_package",
        "remote_http_mode",
    ),
    "emulator_lifecycle": (
        "emulator_found",
        "emulator_not_found",
        "start_success",
        "start_timeout",
        "stop_success",
        "stop_timeout",
        "platform_unsupported",
        "dead_process",
        "command_nonzero",
        "remote_ssh_disabled",
        "windows",
        "macos",
    ),
    "screenshot_backend": (
        "init_success",
        "init_failure",
        "first_frame",
        "timeout",
        "truncated_frame",
        "empty_frame",
        "black_frame",
        "invalid_size",
        "rotated_frame",
        "stream_close",
        "fallback",
    ),
    "input_backend": (
        "click",
        "swipe",
        "key",
        "text",
        "empty_command",
        "invalid_orientation",
        "socket_close",
        "backend_unavailable",
        "timeout",
        "reconnect",
        "fallback",
    ),
    "scrcpy": (
        "server_push",
        "server_startup",
        "video_stream",
        "control_stream",
        "initial_metadata",
        "stream_close",
        "version_mismatch",
        "fallback",
        "live_preview",
        "control_error",
        "device_messages",
    ),
    "uiautomator2_timeout": (
        "implicit_wait",
        "http_timeout",
        "click_long_click",
        "drag_swipe",
        "text_input",
        "xpath_wait_get",
        "service_initialization",
    ),
    "webui_live_control": (
        "start",
        "stop",
        "fallback",
        "resolution",
        "prebuffer",
        "click",
        "drag",
        "key",
        "text",
        "back",
        "system_key",
        "socket_close",
        "resource_cleanup",
        "no_user_text_leak",
    ),
}


SEMANTIC_TESTS = {
    "adb_state": (
        "tests.test_stage8a_scenario_contracts."
        "Stage8AScenarioContractTests.test_adb_state_contracts"
    ),
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
    "input_backend": (
        "tests.test_stage8a_scenario_contracts."
        "Stage8AScenarioContractTests.test_input_backend_contracts"
    ),
    "scrcpy": (
        "tests.test_stage8a_scenario_contracts."
        "Stage8AScenarioContractTests.test_scrcpy_contracts"
    ),
    "uiautomator2_timeout": (
        "tests.test_stage8a_scenario_contracts."
        "Stage8AScenarioContractTests.test_uiautomator2_timeout_contracts"
    ),
    "webui_live_control": (
        "tests.test_stage8a_scenario_contracts."
        "Stage8AScenarioContractTests.test_webui_live_control_contracts"
    ),
}


FIXTURE_EVIDENCE: dict[tuple[str, str], str] = {
    ("adb_state", "wrong_serial"): (
        "tests.test_stage8a_device_acceptance."
        "Stage8ADeviceAcceptanceTests.test_serial_from_config_must_be_explicit_not_auto"
    ),
    ("adb_state", "tcp_reconnect"): (
        "tests.test_stage8a_device_acceptance."
        "Stage8ADeviceAcceptanceTests.test_tcp_reconnect_runs_explicit_connect_for_same_target"
    ),
    ("screenshot_backend", "first_frame"): (
        "tests.test_stage8a_device_acceptance."
        "Stage8ADeviceAcceptanceTests.test_preview_accepts_raw_scrcpy_after_initial_timeout"
    ),
    ("screenshot_backend", "fallback"): (
        "tests.test_stage8a_device_acceptance."
        "Stage8ADeviceAcceptanceTests."
        "test_preview_uses_configured_screenshot_fallback_when_scrcpy_has_no_frame"
    ),
    ("input_backend", "backend_unavailable"): (
        "tests.test_stage8a_device_acceptance."
        "Stage8ADeviceAcceptanceTests.test_minitouch_backend_probe_performs_handshake_without_touch"
    ),
    ("scrcpy", "fallback"): (
        "tests.test_stage8a_device_acceptance."
        "Stage8ADeviceAcceptanceTests."
        "test_preview_uses_configured_screenshot_fallback_when_scrcpy_has_no_frame"
    ),
    ("webui_live_control", "start"): (
        "tests.test_stage8a_security_review."
        "Stage8ASecurityReviewTests.test_local_live_guard_accepts_ipv4_ipv6_and_localhost"
    ),
    ("webui_live_control", "resource_cleanup"): (
        "tests.test_stage8a_device_acceptance."
        "Stage8ADeviceAcceptanceTests.test_minitouch_backend_probe_performs_handshake_without_touch"
    ),
    ("webui_live_control", "no_user_text_leak"): (
        "tests.test_stage8a_security_review."
        "Stage8ASecurityReviewTests.test_acceptance_forbids_clipboard_and_user_text"
    ),
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
            rows.append(
                {
                    "category": category,
                    "scenario": scenario,
                    "semantic_test": SEMANTIC_TESTS[category],
                    "fixture_test": fixture,
                    "evidence_level": "CI_FIXTURE" if fixture else "SEMANTIC_CONTRACT",
                    "limitations": (
                        "No physical backend in CI; real behavior is a separate exact-head "
                        "acceptance artifact."
                    ),
                }
            )
    return rows
