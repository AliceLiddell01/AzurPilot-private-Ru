import logging

import module.logger as logger_module


def test_runtime_logging_does_not_create_diagnostic_file_namespace():
    handlers_before = list(logger_module.logger.handlers)
    logger_module.configure_runtime_logging(name="alas")
    try:
        assert logger_module.diagnostic_hdlr in logger_module.logger.handlers
        assert not any(
            isinstance(handler, logging.FileHandler)
            for handler in logger_module.logger.handlers
            if handler not in handlers_before
        )
    finally:
        logger_module.logger.handlers[:] = handlers_before
        logger_module.reset_diagnostic_context()
