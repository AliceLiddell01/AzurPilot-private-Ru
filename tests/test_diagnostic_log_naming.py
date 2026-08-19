import datetime
import unittest
from pathlib import Path
from unittest.mock import patch

import module.logger as logger_module


class _FixedDate(datetime.date):
    @classmethod
    def today(cls):
        return cls(2026, 8, 20)


class TestDiagnosticLogNaming(unittest.TestCase):
    def test_diagnostic_log_name_uses_current_date(self):
        original_path = logger_module.logger.diagnostic_log_file
        try:
            with (
                patch.object(logger_module.datetime, "date", _FixedDate),
                patch.object(logger_module.diagnostic_hdlr, "configure_output") as configure_output,
            ):
                logger_module._configure_diagnostic_logger("alas")

            expected = Path("./log/diagnostic/2026-08-20_alas.txt")
            configure_output.assert_called_once_with(expected, logger_module.file_formatter)
            self.assertEqual(expected.resolve(), Path(logger_module.logger.diagnostic_log_file))
        finally:
            logger_module.logger.diagnostic_log_file = original_path


if __name__ == "__main__":
    unittest.main()
