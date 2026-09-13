import logging
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from alas import AzurLaneAutoScript
from module.guild.logistics import GuildLogistics
from module.logging_context import (
    TaskContextFilter,
    get_task_context,
    install_task_context_filter,
    task_context,
    task_logging_context,
)
from module.logging_core import DiagnosticContextHandler
from module.map_detection.view import View
from module.ocr.ocr import Duration, Ocr
from module.research.ui import ResearchUI


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


class TestTaskLoggingContext(unittest.TestCase):
    def test_default_nested_and_sequential_context_restore(self):
        self.assertIsNone(get_task_context())

        with task_context("Research"):
            self.assertEqual("Research", get_task_context())
            with task_context("Guild"):
                self.assertEqual("Guild", get_task_context())
            self.assertEqual("Research", get_task_context())

        self.assertIsNone(get_task_context())
        with task_context("Commission"):
            self.assertEqual("Commission", get_task_context())
        self.assertIsNone(get_task_context())

    def test_context_value_is_bounded_and_empty_value_becomes_none(self):
        with task_context("x" * 512):
            self.assertEqual(128, len(get_task_context()))
        with task_context("   "):
            self.assertIsNone(get_task_context())
        self.assertIsNone(get_task_context())

    def test_filter_adds_metadata_without_changing_plain_text_formatter(self):
        local_logger = logging.getLogger(f"task-context-{id(self)}")
        local_logger.handlers.clear()
        local_logger.filters.clear()
        local_logger.propagate = False
        local_logger.setLevel(logging.DEBUG)
        handler = _CaptureHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s|%(message)s"))
        local_logger.addHandler(handler)

        context_filter = install_task_context_filter(local_logger)
        self.assertIs(context_filter, install_task_context_filter(local_logger))
        self.assertEqual(1, sum(isinstance(item, TaskContextFilter) for item in local_logger.filters))

        with task_context("Research"):
            local_logger.info("выбран проект")

        self.assertEqual(1, len(handler.records))
        record = handler.records[0]
        self.assertEqual("Research", record.alas_task)
        self.assertEqual("INFO|выбран проект", handler.format(record))

    def test_decorator_restores_context_after_success_and_exception(self):
        class Probe:
            @task_logging_context
            def run(self, command, *, fail=False):
                self.seen = get_task_context()
                if fail:
                    raise RuntimeError("synthetic")
                return "result"

        probe = Probe()
        self.assertEqual("result", probe.run("opsi_daily"))
        self.assertEqual("OpsiDaily", probe.seen)
        self.assertIsNone(get_task_context())

        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            probe.run("research", fail=True)
        self.assertEqual("Research", probe.seen)
        self.assertIsNone(get_task_context())

    def test_alas_run_exposes_task_metadata_only_inside_run(self):
        import module.logger as logger_module

        capture = _CaptureHandler()
        capture.setLevel(logging.DEBUG)
        logger_module.logger.addHandler(capture)
        script = AzurLaneAutoScript.__new__(AzurLaneAutoScript)

        def research():
            logger_module.logger.info("nested task log")

        script.__dict__["research"] = research
        try:
            result = script.run("research", skip_first_screenshot=True)
            logger_module.logger.info("after task")
        finally:
            logger_module.logger.removeHandler(capture)
            capture.close()

        self.assertTrue(result)
        nested = next(record for record in capture.records if record.getMessage() == "nested task log")
        after = next(record for record in capture.records if record.getMessage() == "after task")
        self.assertEqual("Research", nested.alas_task)
        self.assertIsNone(after.alas_task)
        self.assertIsNone(get_task_context())

    def test_diagnostic_clone_keeps_only_safe_task_metadata(self):
        handler = DiagnosticContextHandler(capacity=2)
        local_logger = logging.getLogger(f"task-diagnostic-{id(handler)}")
        local_logger.handlers.clear()
        local_logger.filters.clear()
        local_logger.propagate = False
        local_logger.setLevel(logging.DEBUG)
        install_task_context_filter(local_logger)
        local_logger.addHandler(handler)
        try:
            with task_context("Guild"):
                local_logger.debug("raw state", extra={"large_payload": object()})
            record = handler.snapshot()[0]
        finally:
            handler.close()
            local_logger.handlers.clear()

        self.assertEqual("Guild", record.alas_task)
        self.assertFalse(hasattr(record, "large_payload"))
        self.assertIsNone(record.exc_info)

    def test_diagnostic_boundary_bounds_unfiltered_task_extra(self):
        handler = DiagnosticContextHandler(capacity=2)
        local_logger = logging.getLogger(f"task-untrusted-{id(handler)}")
        local_logger.handlers.clear()
        local_logger.filters.clear()
        local_logger.propagate = False
        local_logger.setLevel(logging.DEBUG)
        local_logger.addHandler(handler)
        try:
            local_logger.debug(
                "raw state",
                extra={"alas_task": "x" * 512, "large_payload": object()},
            )
            record = handler.snapshot()[0]
        finally:
            handler.close()
            local_logger.handlers.clear()

        self.assertEqual("x" * 128, record.alas_task)
        self.assertFalse(hasattr(record, "large_payload"))


class TestResearchLoggingSemantics(unittest.TestCase):
    def test_status_polling_moves_to_debug_without_changing_result(self):
        research = ResearchUI.__new__(ResearchUI)
        status = SimpleNamespace(crop=lambda _offset: SimpleNamespace(area=(0, 0, 1, 1)))
        waiting = Mock()
        running = Mock()
        detail = Mock()
        waiting.match.return_value = False
        running.match.return_value = False
        detail.match.return_value = False

        with (
            patch("module.research.ui.RESEARCH_STATUS", [status] * 5),
            patch("module.research.ui.RESEARCH_SCALING", [1] * 5),
            patch("module.research.ui.TEMPLATE_WAITING", waiting),
            patch("module.research.ui.TEMPLATE_RUNNING", running),
            patch("module.research.ui.TEMPLATE_DETAIL", detail),
            patch("module.research.ui.crop", return_value=np.zeros((1, 1, 3), dtype=np.uint8)),
            patch("module.research.ui.rgb2gray", return_value=np.zeros((1, 1), dtype=np.uint8)),
            patch("module.research.ui.logger.debug") as debug,
            patch("module.research.ui.logger.info") as info,
        ):
            result = research.get_research_status(np.zeros((1, 1, 3), dtype=np.uint8))

        self.assertEqual(["unknown"] * 5, result)
        debug.assert_called_once()
        self.assertIn("Состояние исследования", debug.call_args.args[0])
        info.assert_not_called()


class TestGuildLoggingSemantics(unittest.TestCase):
    def _probe(self):
        guild = GuildLogistics.__new__(GuildLogistics)
        guild.__dict__["device"] = SimpleNamespace(image=object())
        return guild

    def test_supply_state_keeps_boolean_contract_but_uses_debug(self):
        guild = self._probe()
        with (
            patch("module.guild.logistics.get_color", return_value=(255, 0, 0)),
            patch("module.guild.logistics.logger.debug") as debug,
            patch("module.guild.logistics.logger.info") as info,
        ):
            self.assertTrue(guild._guild_logistics_supply_available())
        debug.assert_called_once_with('[Гильдия — логистика] Кнопка снабжения гильдии активна')
        info.assert_not_called()

        with (
            patch("module.guild.logistics.get_color", return_value=(100, 100, 100)),
            patch("module.guild.logistics.logger.debug") as debug,
            patch("module.guild.logistics.logger.info") as info,
        ):
            self.assertFalse(guild._guild_logistics_supply_available())
        debug.assert_called_once_with('[Гильдия — логистика] Кнопка снабжения гильдии неактивна')
        info.assert_not_called()


class _OcrProbe(Ocr):
    def __init__(self):
        super().__init__([(0, 0, 1, 1)], name="PROBE")
        self._model = SimpleNamespace(
            atomic_ocr_for_single_lines=lambda _images, _alphabet: [["1", "2", "3"]]
        )

    @property
    def cnocr(self):
        return self._model

    def pre_process(self, image):
        return image


class TestOcrLoggingSemantics(unittest.TestCase):
    def test_raw_result_and_timing_move_to_debug_without_changing_return(self):
        ocr = _OcrProbe()
        image = [np.zeros((2, 2), dtype=np.uint8)]
        with (
            patch("module.ocr.ocr.crop_to_text", side_effect=lambda value: value),
            patch("module.ocr.ocr.logger.debug") as debug,
            patch("module.ocr.ocr.logger.info") as info,
        ):
            result = ocr.ocr(image, direct_ocr=True)

        self.assertEqual("123", result)
        debug.assert_called_once()
        self.assertIn("PROBE", debug.call_args.args[0])
        self.assertIn("123", debug.call_args.args[0])
        info.assert_not_called()

    def test_sustained_parse_failure_warning_is_preserved(self):
        with patch("module.ocr.ocr.logger.warning") as warning:
            result = Duration.parse_time("not-a-duration")
        self.assertEqual(0, result.total_seconds())
        warning.assert_called_once_with('[OCR] Недопустимая длительность: not-a-duration')


class TestOpsiMapLoggingSemantics(unittest.TestCase):
    @staticmethod
    def _view(mode):
        view = View.__new__(View)
        view.mode = mode
        view.shape = np.array([1, 1])
        view.grids = {
            (0, 0): SimpleNamespace(str="A"),
            (1, 0): SimpleNamespace(str="B"),
            (0, 1): SimpleNamespace(str="C"),
            (1, 1): SimpleNamespace(str="D"),
        }
        return view

    def test_opsi_ascii_snapshot_moves_to_debug(self):
        view = self._view("os")
        with (
            patch("module.map_detection.view.logger.debug") as debug,
            patch("module.map_detection.view.logger.info") as info,
        ):
            view.show()

        self.assertEqual(["A B", "C D"], [call.args[0] for call in debug.call_args_list])
        info.assert_not_called()

    def test_campaign_ascii_snapshot_moves_to_debug(self):
        view = self._view("main")
        with (
            patch("module.map_detection.view.logger.debug") as debug,
            patch("module.map_detection.view.logger.info") as info,
        ):
            view.show()

        self.assertEqual(["A B", "C D"], [call.args[0] for call in debug.call_args_list])
        info.assert_not_called()


if __name__ == "__main__":
    unittest.main()
