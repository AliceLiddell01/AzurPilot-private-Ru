import module.logger as logger_module


def test_configured_rich_handlers_hide_traceback_locals():
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

    handlers_before = list(logger_module.logger.handlers)
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
        logger_module.reset_diagnostic_context()
