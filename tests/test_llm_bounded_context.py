import module.logger as logger_module


def test_llm_diagnostic_context_is_bounded_and_memory_only():
    logger_module.reset_diagnostic_context()
    try:
        for index in range(300):
            logger_module.logger.info(f"line-{index:04d}")
        context = logger_module.get_diagnostic_context()
        assert len(context) == 200
        assert context[0] == "line-0100"
        assert context[-1] == "line-0299"
        assert not hasattr(logger_module.logger, "log_file")
    finally:
        logger_module.reset_diagnostic_context()
