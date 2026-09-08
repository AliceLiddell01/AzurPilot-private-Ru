import logging
import threading
import unittest
from io import StringIO
from unittest.mock import patch

import module.logger as logger_module
from module.logging_core import DiagnosticContextHandler, RepeatedEventSuppressor


class TestLoggingRouting(unittest.TestCase):
    def setUp(self):
        self._handlers_before = list(logger_module.logger.handlers)
        logger_module.reset_diagnostic_context()

    def tearDown(self):
        for handler in list(logger_module.logger.handlers):
            if handler not in self._handlers_before:
                logger_module.logger.removeHandler(handler)
                handler.close()
        logger_module.logger.handlers[:] = self._handlers_before
        logger_module.reset_diagnostic_context()

    def test_logger_uses_console_and_memory_context_without_file_handler(self):
        logger_module.configure_runtime_logging(name="logging-test")
        self.assertEqual(logging.DEBUG, logger_module.logger.level)
        self.assertFalse(logger_module.logger.propagate)
        self.assertEqual(logging.DEBUG, logger_module.diagnostic_hdlr.level)
        self.assertEqual(logging.INFO, logger_module.console_hdlr.level)
        self.assertFalse(
            any(
                isinstance(handler, logging.FileHandler)
                for handler in logger_module.logger.handlers
                if handler not in self._handlers_before
            )
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
                        logging.INFO, "state unknown", key="state", payload="unknown"
                    )
                )
                self.assertFalse(
                    logger_module.log_suppressed(
                        logging.INFO, "state unknown", key="state", payload="unknown"
                    )
                )
                self.assertTrue(
                    logger_module.log_suppressed(
                        logging.INFO, "state ready", key="state", payload="ready"
                    )
                )
                self.assertEqual(3, log.call_count)
                self.assertIn("повторено 1 раз", log.call_args_list[1].args[1])
        finally:
            logger_module.reset_suppression()


class TestRepeatedEventSuppressor(unittest.TestCase):
    def test_first_repeat_summary_and_payload_change(self):
        suppressor = RepeatedEventSuppressor(max_keys=4, default_window=10)
        first = suppressor.observe("state", payload="unknown", level=20, message="state=unknown", now=1)
        repeat1 = suppressor.observe("state", payload="unknown", level=20, message="state=unknown", now=2)
        repeat2 = suppressor.observe("state", payload="unknown", level=20, message="state=unknown", now=3)
        changed = suppressor.observe("state", payload="ready", level=20, message="state=ready", now=4)
        self.assertTrue(first.emit)
        self.assertFalse(repeat1.emit)
        self.assertFalse(repeat2.emit)
        self.assertTrue(changed.emit)
        self.assertEqual(2, changed.summary_count)
        self.assertEqual("state=unknown", changed.summary_message)

    def test_ambiguous_payload_equality_does_not_escape(self):
        class AmbiguousEquality:
            def __eq__(self, other):
                return self

            def __bool__(self):
                raise ValueError("ambiguous truth value")

        suppressor = RepeatedEventSuppressor(default_window=60)
        self.assertTrue(suppressor.observe("array-like", payload=AmbiguousEquality(), level=20, message="first", now=1).emit)
        self.assertTrue(suppressor.observe("array-like", payload=AmbiguousEquality(), level=20, message="second", now=2).emit)

    def test_severity_escalation_and_error_are_never_suppressed(self):
        suppressor = RepeatedEventSuppressor(default_window=60)
        self.assertTrue(suppressor.observe("x", payload=1, level=20, message="x", now=1).emit)
        self.assertFalse(suppressor.observe("x", payload=1, level=20, message="x", now=2).emit)
        warning = suppressor.observe("x", payload=1, level=logging.WARNING, message="x warning", now=3)
        self.assertTrue(warning.emit)
        self.assertEqual(1, warning.summary_count)
        self.assertTrue(suppressor.observe("x", payload=1, level=logging.ERROR, message="x error", now=4).emit)
        self.assertTrue(suppressor.observe("x", payload=1, level=logging.CRITICAL, message="x critical", now=5).emit)

    def test_repeated_error_without_escalation_is_never_suppressed(self):
        suppressor = RepeatedEventSuppressor(default_window=60)
        self.assertTrue(suppressor.observe("y", payload=1, level=logging.ERROR, message="y", now=1).emit)
        self.assertTrue(suppressor.observe("y", payload=1, level=logging.ERROR, message="y", now=2).emit)

    def test_window_expiry_emits_and_summarizes(self):
        suppressor = RepeatedEventSuppressor(default_window=5)
        suppressor.observe("x", payload=1, level=20, message="x", now=0)
        suppressor.observe("x", payload=1, level=20, message="x", now=1)
        decision = suppressor.observe("x", payload=1, level=20, message="x", now=5)
        self.assertTrue(decision.emit)
        self.assertEqual(1, decision.summary_count)

    def test_finish_returns_summary_and_clears_series(self):
        suppressor = RepeatedEventSuppressor(default_window=60)
        suppressor.observe("x", payload=1, level=logging.WARNING, message="x", now=1)
        suppressor.observe("x", payload=1, level=logging.WARNING, message="x", now=2)
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
                    suppressor.observe((offset + index) % 16, payload=index % 3, level=20, message="value")
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
        test_logger = logging.getLogger(f"diag-test-{id(handler)}")
        test_logger.handlers.clear()
        test_logger.propagate = False
        test_logger.setLevel(logging.DEBUG)
        normal = logging.StreamHandler(normal_stream)
        normal.setLevel(logging.INFO)
        normal.setFormatter(logging.Formatter("%(levelname)s|%(message)s"))
        test_logger.addHandler(normal)
        test_logger.addHandler(handler)
        return test_logger

    def test_all_levels_are_bounded_and_error_snapshots_context_without_file(self):
        handler = DiagnosticContextHandler(
            capacity=4,
            max_bytes=64,
            sanitizer=lambda value: str(value).replace("secret", "***"),
        )
        try:
            normal_stream = StringIO()
            test_logger = self.make_logger(handler, normal_stream)
            test_logger.debug("debug")
            test_logger.info("secret info")
            test_logger.warning("warning")
            test_logger.error("boom")
            self.assertEqual(
                ["debug", "*** info", "warning", "boom"],
                [record.getMessage() for record in handler.snapshot(last_failure=True)],
            )
            self.assertEqual((), handler.snapshot())
            self.assertNotIn("secret", " ".join(record.getMessage() for record in handler.snapshot(last_failure=True)))
            self.assertFalse(any(isinstance(h, logging.FileHandler) for h in test_logger.handlers))
        finally:
            handler.close()

    def test_buffer_clone_does_not_retain_arbitrary_extra_objects(self):
        handler = DiagnosticContextHandler(capacity=2)
        try:
            test_logger = self.make_logger(handler, StringIO())
            test_logger.debug("detail", extra={"large_payload": object()})
            record = handler.snapshot()[0]
            self.assertFalse(hasattr(record, "large_payload"))
            self.assertIsNone(record.exc_info)
            self.assertIsNone(record.stack_info)
        finally:
            handler.close()

    def test_sanitizer_failure_does_not_escape_logging_call(self):
        handler = DiagnosticContextHandler(capacity=2, sanitizer=lambda value: 1 / 0)
        try:
            test_logger = self.make_logger(handler, StringIO())
            with patch.object(logging, "raiseExceptions", False):
                test_logger.debug("detail")
            self.assertEqual((), handler.snapshot())
        finally:
            handler.close()

    def test_second_error_starts_a_new_failure_snapshot(self):
        handler = DiagnosticContextHandler(capacity=2)
        try:
            test_logger = self.make_logger(handler, StringIO())
            test_logger.debug("one")
            test_logger.error("first")
            test_logger.debug("two")
            test_logger.critical("second")
            self.assertEqual(
                ["two", "second"],
                [record.getMessage() for record in handler.snapshot(last_failure=True)],
            )
        finally:
            handler.close()

    def test_capacity_snapshot_keeps_triggering_error(self):
        handler = DiagnosticContextHandler(capacity=2)
        try:
            test_logger = self.make_logger(handler, StringIO())
            test_logger.info("old")
            test_logger.info("latest")
            test_logger.error("boom")
            self.assertEqual(
                ["latest", "boom"],
                [record.getMessage() for record in handler.snapshot(last_failure=True)],
            )
        finally:
            handler.close()

    def test_reset_clears_current_and_last_failure_context(self):
        handler = DiagnosticContextHandler(capacity=2)
        try:
            test_logger = self.make_logger(handler, StringIO())
            test_logger.info("one")
            test_logger.error("boom")
            handler.reset()
            self.assertEqual((), handler.snapshot())
            self.assertEqual((), handler.snapshot(last_failure=True))
        finally:
            handler.close()


if __name__ == "__main__":
    unittest.main()
