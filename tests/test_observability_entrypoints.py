"""Регрессии явной настройки журналирования на границах процессов."""

import sys
import types
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
            "set_file_logger",
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
    with patch.object(gui.logger, "set_file_logger") as set_file_logger:
        gui._configure_gui_logging()

    set_file_logger.assert_called_once_with(
        name="gui",
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
    server.bind.side_effect = lambda uri: events.append(("bind", uri))
    server.run.side_effect = lambda: events.append(("run",))

    zerorpc = types.ModuleType("zerorpc")
    zerorpc.Server = lambda _implementation: server
    zmq = types.ModuleType("zmq")

    class ZMQError(Exception):
        pass

    zmq.error = SimpleNamespace(ZMQError=ZMQError)
    al_ocr = types.ModuleType("module.ocr.al_ocr")
    al_ocr.AlOcr = type("AlOcr", (), {})
    models = types.ModuleType("module.ocr.models")
    models.OcrModel = type("OcrModel", (), {})

    with (
        patch.object(
            ocr_rpc.logger,
            "set_file_logger",
            side_effect=lambda **kwargs: events.append(("logging", kwargs)),
        ),
        patch.object(ocr_rpc.logger, "info"),
        patch.dict(
            sys.modules,
            {
                "zerorpc": zerorpc,
                "zmq": zmq,
                "module.ocr.al_ocr": al_ocr,
                "module.ocr.models": models,
            },
        ),
    ):
        ocr_rpc.start_ocr_server(port=23457)

    assert events[0] == (
        "logging",
        {
            "name": "ocr-rpc",
            "observability_profile": None,
            "observability_component": "ocr-rpc",
        },
    )
    assert events[1] == ("bind", "tcp://127.0.0.1:23457")
    assert events[2] == ("run",)
