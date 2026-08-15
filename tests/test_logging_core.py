import logging
import tempfile
import threading
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import module.logger as logger_module
from module.logging_core import DiagnosticContextHandler, RepeatedEventSuppressor


class TestLoggingRouting(unittest.TestCase):
    def test_logger_accepts_debug_but_normal_handlers_start_at_info(self):
        self.assertEqual(logging.DEBUG, logger_module.logger.level)
        self.assertFalse(logger_module.logger.propagate)
        self.assertEqual(logging.DEBUG, logger_module.diagnostic_hdlr.level)
        self.assertEqual(logging.INFO, logger_module.console_hdlr.level)

        normal_file_handlers = [
            handler
            for handler in logger_module.logger.handlers
            if isinstance(handler, logger_module.RichTimedRotatingHandler)
        ]
        self.assertTrue(normal_file_handlers)
        self.assertTrue(
            all(handler.level == logging.INFO for handler in normal_file_handlers)
        )

    def test_webui_handler_has_independent_info_threshold(self):
        handlers_before = list(logger_module.logger.handlers)
        callback_records = []
        try:
            logger_module.set_func_logger(callback_records.append)
            web_handlers = [
                handler
                for handler in logger_module.logger.handlers
                if isinstance(handler, logger_module.RichRenderableHandler)
            ]
            self.assertEqual(1, len(web_handlers))
            self.assertEqual(logging.INFO, web_handlers[0].level)
            logger_module.logger.debug("webui debug must stay hidden")
            self.assertEqual([], callback_records)
        finally:
            for handler in logger_module.logger.handlers:
                if handler not in handlers_before:
                    handler.close()
            logger_module.logger.handlers[:] = handlers_before
            logger_module.reset_diagnostic_context()

    def test_reinitializing_same_file_logger_does_not_duplicate_handler(self):
        before = [
            handler
            for handler in logger_module.logger.handlers
            if isinstance(handler, logger_module.RichTimedRotatingHandler)
        ]
        log_file_before = logger_module.logger.log_file
        logger_module.set_file_logger(name=logger_module.pyw_name)
        after = [
            handler
            for handler in logger_module.logger.handlers
            if isinstance(handler, logger_module.RichTimedRotatingHandler)
        ]
        self.assertEqual(len(before), len(after))
        self.assertEqual(log_file_before, logger_module.logger.log_file)

    def test_hr_level_one_and_two_do_not_emit_duplicate_info_record(self):
        for level in (1, 2):
            with (
                patch.object(logger_module.logger, "rule") as rule,
                patch.object(logger_module.logger, "info") as info,
            ):
                logger_module.hr("section", level=level)
            rule.assert_called_once()
            info.assert_not_called()

    def test_public_suppression_api_emits_first_summary_and_changed_state(self):
        logger_module.reset_suppression()
        try:
            with patch.object(logger_module.logger, "log") as log:
                self.assertTrue(
                    logger_module.log_suppressed(
                        logging.INFO,
                        "state unknown",
                        key="state",
                        payload="unknown",
                    )
                )
                self.assertFalse(
                    logger_module.log_suppressed(
                        logging.INFO,
                        "state unknown",
                        key="state",
                        payload="unknown",
                    )
                )
                self.assertTrue(
                    logger_module.log_suppressed(
                        logging.INFO,
                        "state ready",
                        key="state",
                        payload="ready",
                    )
                )
                self.assertEqual(3, log.call_count)
                self.assertIn("повторено 1 раз", log.call_args_list[1].args[1])
        finally:
            logger_module.reset_suppression()


class TestRepeatedEventSuppressor(unittest.TestCase):
    def test_first_repeat_summary_and_payload_change(self):
        suppressor = RepeatedEventSuppressor(max_keys=4, default_window=10)
        first = suppressor.observe(
            "state", payload="unknown", level=logging.INFO,
            message="state=unknown", now=1,
        )
        repeat1 = suppressor.observe(
            "state", payload="unknown", level=logging.INFO,
            message="state=unknown", now=2,
        )
        repeat2 = suppressor.observe(
            "state", payload="unknown", level=logging.INFO,
            message="state=unknown", now=3,
        )
        changed = suppressor.observe(
            "state", payload="ready", level=logging.INFO,
            message="state=ready", now=4,
        )

        self.assertTrue(first.emit)
        self.assertFalse(repeat1.emit)
        self.assertFalse(repeat2.emit)
        self.assertTrue(changed.emit)
        self.assertEqual(2, changed.summary_count)
        self.assertEqual("state=unknown", changed.summary_message)

    def test_severity_escalation_and_error_are_never_suppressed(self):
        suppressor = RepeatedEventSuppressor(default_window=60)
        self.assertTrue(
            suppressor.observe(
                "x", payload=1, level=logging.INFO, message="x", now=1
            ).emit
        )
        self.assertFalse(
            suppressor.observe(
                "x", payload=1, level=logging.INFO, message="x", now=2
            ).emit
        )
        warning = suppressor.observe(
            "x", payload=1, level=logging.WARNING, message="x warning", now=3
        )
        self.assertTrue(warning.emit)
        self.assertEqual(1, warning.summary_count)
        self.assertTrue(
            suppressor.observe(
                "x", payload=1, level=logging.ERROR, message="x error", now=4
            ).emit
        )
        self.assertTrue(
            suppressor.observe(
                "x", payload=1, level=logging.CRITICAL,
                message="x critical", now=5,
            ).emit
        )

    def test_window_expiry_emits_and_summarizes(self):
        suppressor = RepeatedEventSuppressor(default_window=5)
        suppressor.observe("x", payload=1, level=20, message="x", now=0)
        suppressor.observe("x", payload=1, level=20, message="x", now=1)
        decision = suppressor.observe(
            "x", payload=1, level=20, message="x", now=5
        )
        self.assertTrue(decision.emit)
        self.assertEqual(1, decision.summary_count)

    def test_finish_returns_summary_and_clears_series(self):
        suppressor = RepeatedEventSuppressor(default_window=60)
        suppressor.observe(
            "x", payload=1, level=logging.WARNING, message="x", now=1
        )
        suppressor.observe(
            "x", payload=1, level=logging.WARNING, message="x", now=2
        )
        decision = suppressor.finish("x", now=3)
        self.assertFalse(decision.emit)
        self.assertEqual(1, decision.summary_count)
        self.assertEqual(logging.WARNING, decision.summary_level)
        self.assertEqual(0, len(suppressor))

    def test_state_is_bounded_and_resettable(self):
        suppressor = RepeatedEventSuppressor(max_keys=2)
        suppressor.observe("a", payload=1, level=20, message="a", now=1)
        suppressor.observe("b", payload=1, level=20, message="b", now=1)
        suppressor.observe("c", payload=1, level=20, message="c", now=1)
        self.assertEqual(2, len(suppressor))
        suppressor.reset("b")
        self.assertEqual(1, len(suppressor))
        suppressor.reset()
        self.assertEqual(0, len(suppressor))

    def test_concurrent_observe_keeps_bounded_state(self):
        suppressor = RepeatedEventSuppressor(max_keys=8)
        errors = []

        def worker(offset):
            try:
                for index in range(100):
                    suppressor.observe(
                        (offset + index) % 16,
                        payload=index % 3,
                        level=logging.INFO,
                        message="value",
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors)
        self.assertLessEqual(len(suppressor), 8)


class TestDiagnosticContextHandler(unittest.TestCase):
    @staticmethod
    def make_logger(handler, normal_stream):
        logger = logging.getLogger(f"diag-test-{id(handler)}")
        logger.handlers.clear()
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        normal = logging.StreamHandler(normal_stream)
        normal.setLevel(logging.INFO)
        normal.setFormatter(logging.Formatter("%(levelname)s|%(message)s"))
        logger.addHandler(normal)
        logger.addHandler(handler)
        return logger

    def test_debug_is_bounded_and_error_flushes_only_diagnostic_context(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "diagnostic.log"
            handler = DiagnosticContextHandler(
                capacity=2,
                sanitizer=lambda value: str(value).replace("secret", "***"),
            )
            formatter = logging.Formatter("%(levelname)s|%(message)s")
            handler.setFormatter(formatter)
            handler.configure_output(output, formatter)
            normal_stream = StringIO()
            logger = self.make_logger(handler, normal_stream)

            logger.debug("old")
            logger.debug("secret two")
            logger.debug("three")
            self.assertEqual(
                ["*** two", "three"],
                [record.getMessage() for record in handler.snapshot()],
            )
            self.assertEqual("", normal_stream.getvalue())

            logger.error("boom")
            self.assertEqual(
                ["*** two", "three"],
                [record.getMessage() for record in handler.snapshot(last_failure=True)],
            )
            self.assertEqual(1, normal_stream.getvalue().count("ERROR|boom"))
            diagnostic = output.read_text(encoding="utf-8")
            self.assertNotIn("old", diagnostic)
            self.assertIn("*** two", diagnostic)
            self.assertIn("three", diagnostic)
            self.assertIn("Контекст перед ERROR: boom", diagnostic)
            handler.close()

    def test_info_does_not_enter_diagnostic_buffer(self):
        handler = DiagnosticContextHandler(capacity=4)
        logger = self.make_logger(handler, StringIO())
        logger.info("normal")
        self.assertEqual((), handler.snapshot())
        handler.close()

    def test_success_and_close_do_not_dump_buffer(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "diagnostic.log"
            handler = DiagnosticContextHandler(capacity=4)
            formatter = logging.Formatter("%(message)s")
            handler.configure_output(output, formatter)
            logger = self.make_logger(handler, StringIO())
            logger.debug("quiet detail")
            handler.close()
            self.assertFalse(output.exists())

    def test_reset_clears_current_and_last_failure_context(self):
        handler = DiagnosticContextHandler(capacity=2)
        logger = self.make_logger(handler, StringIO())
        logger.debug("one")
        logger.error("boom")
        self.assertEqual(
            ["one"],
            [record.getMessage() for record in handler.snapshot(last_failure=True)],
        )
        logger.debug("two")
        handler.reset()
        self.assertEqual((), handler.snapshot())
        self.assertEqual((), handler.snapshot(last_failure=True))
        handler.close()


if __name__ == "__main__":
    unittest.main()
