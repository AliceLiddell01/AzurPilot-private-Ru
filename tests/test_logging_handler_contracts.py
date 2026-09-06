from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import module.logger as logger_module

_OTEL_ENDPOINT_ENVIRONMENT_KEYS = (
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL",
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_SDK_DISABLED",
)


def test_configured_rich_handlers_hide_traceback_locals(tmp_path, monkeypatch):
    for key in _OTEL_ENDPOINT_ENVIRONMENT_KEYS:
        monkeypatch.delenv(key, raising=False)
    handlers_before = list(logger_module.logger.handlers)
    log_file_before = logger_module.logger.log_file
    diagnostic_log_file_before = logger_module.logger.diagnostic_log_file
    failure_target_before = logger_module.diagnostic_hdlr._failure_target
    with patch.object(
        logger_module.multiprocessing,
        "current_process",
        return_value=SimpleNamespace(name="LoggingTestProcess"),
    ):
        logger_module.set_file_logger(
            name="handler-contract",
            log_dir=Path(tmp_path),
        )
    assert logger_module.console_hdlr.tracebacks_show_locals is False

    file_handlers = [
        handler
        for handler in logger_module.logger.handlers
        if isinstance(handler, logger_module.RichTimedRotatingHandler)
    ]
    assert file_handlers
    assert all(
        handler.richd.tracebacks_show_locals is False for handler in file_handlers
    )

    try:
        logger_module.set_func_logger(lambda _renderable: None)
        web_handlers = [
            handler
            for handler in logger_module.logger.handlers
            if isinstance(handler, logger_module.RichRenderableHandler)
        ]
        assert len(web_handlers) == 1
        assert web_handlers[0].tracebacks_show_locals is False
    finally:
        for handler in list(logger_module.logger.handlers):
            if handler not in handlers_before:
                logger_module.logger.removeHandler(handler)
                handler.close()
        logger_module.logger.handlers[:] = handlers_before
        logger_module.logger.log_file = log_file_before
        logger_module.logger.diagnostic_log_file = diagnostic_log_file_before
        logger_module.diagnostic_hdlr.configure_failure_target(failure_target_before)
        logger_module.reset_diagnostic_context()
