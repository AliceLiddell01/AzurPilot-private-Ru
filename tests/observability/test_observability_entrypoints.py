"""Регрессии явной настройки журналирования на границах процессов."""

import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import alas
import gui
import module.ocr.rpc as ocr_rpc


def test_direct_alas_startup_bootstraps_logging_before_storage() -> None:
    events = []

    with (
        patch.object(
            alas.logger,
            "configure_runtime_logging",
            side_effect=lambda name: events.append(("logging", name)),
        ),
        patch.object(
            alas.logger,
            "hr",
            side_effect=lambda *args, **kwargs: events.append(("startup",)),
        ),
        patch.object(
            alas,
            "bootstrap_runtime_storage",
            side_effect=lambda **kwargs: events.append(("storage", kwargs)),
        ),
        patch.object(alas.logger, "info"),
    ):
        script = alas.AzurLaneAutoScript(config_name="farm_main")

    assert script.config_name == "farm_main"
    assert events[0] == ("logging", "farm_main")
    assert events[1] == ("startup",)
    assert events[2] == ("storage", {"require_ready": True})


def test_gui_spawned_process_bootstraps_process_role_logging() -> None:
    deployment = SimpleNamespace(
        WebuiHost="127.0.0.1",
        WebuiPort=25548,
        WebuiSSLKey=None,
        WebuiSSLCert=None,
    )
    uvicorn_config = Mock(backlog=2048)

    with (
        patch.object(gui, "_configure_gui_logging") as configure_logging,
        patch.object(gui.State, "deploy_config", deployment),
        patch.object(sys, "argv", ["gui.py", "--host", "127.0.0.1", "--port", "23456"]),
        patch("uvicorn.Config", return_value=uvicorn_config),
        patch.object(gui, "_run_uvicorn_server"),
    ):
        gui.func(None)

    configure_logging.assert_called_once_with()


def test_gui_logging_uses_component_without_fake_profile() -> None:
    with patch.object(gui.logger, "configure_runtime_logging") as configure_logging:
        gui._configure_gui_logging()

    configure_logging.assert_called_once_with(
        observability_profile=None,
        observability_component="gui",
    )


def test_gui_supervisor_bootstraps_parent_before_worker_creation() -> None:
    with (
        patch.object(gui, "_configure_gui_logging") as configure_logging,
        patch.object(gui, "_recover_orphaned_workers", return_value=False),
    ):
        gui.run_webui_supervisor()

    configure_logging.assert_called_once_with()


def test_ocr_rpc_server_bootstraps_process_role_before_binding() -> None:
    events = []
    server = Mock()
    run_result = object()
    server.run.side_effect = lambda **kwargs: (
        events.append(("run", kwargs)),
        run_result,
    )[1]

    with (
        patch.object(
            ocr_rpc.logger,
            "configure_runtime_logging",
            side_effect=lambda **kwargs: events.append(("logging", kwargs)),
        ),
        patch.object(ocr_rpc.logger, "info"),
        patch.object(ocr_rpc, "_OcrRpcService") as service_factory,
        patch.object(
            ocr_rpc,
            "_OcrRpcServer",
            side_effect=lambda port, service: (
                events.append(("construct", port, service)),
                server,
            )[1],
        ) as server_factory,
    ):
        result = ocr_rpc.start_ocr_server(port=23457)

    assert events[0] == (
        "logging",
        {
            "name": "ocr-rpc",
            "observability_profile": None,
            "observability_component": "ocr-rpc",
        },
    )
    assert events[1][0:2] == ("construct", 23457)
    assert events[2] == ("run", {"stop_event": None, "ready_event": None})
    assert result is run_result
    service_factory.assert_called_once()
    server_factory.assert_called_once_with(23457, service_factory.return_value)
