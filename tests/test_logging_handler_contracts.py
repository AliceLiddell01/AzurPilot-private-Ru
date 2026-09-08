import logging

import module.logger as logger_module


def test_configured_rich_handlers_hide_traceback_locals(monkeypatch):
    for key in (
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "OTEL_SDK_DISABLED",
    ):
        monkeypatch.delenv(key, raising=False)
    handlers_before = list(logger_module.logger.handlers)
    try:
        logger_module.configure_runtime_logging(name="handler-contract")
        assert logger_module.console_hdlr.tracebacks_show_locals is False
        assert not any(
            isinstance(handler, logging.FileHandler)
            for handler in logger_module.logger.handlers
            if handler not in handlers_before
        )

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
        logger_module.reset_diagnostic_context()
