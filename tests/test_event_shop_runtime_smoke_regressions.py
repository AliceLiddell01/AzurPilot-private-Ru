from pathlib import Path

from module.shop_event.item import CounterOcr


ROOT = Path(__file__).resolve().parents[1]
LOGGER_SOURCE = ROOT / "module" / "logger.py"


def test_counter_ocr_missing_current_is_fail_closed():
    assert CounterOcr.parse_counter_result("/1") == [0, 1]


def test_counter_ocr_valid_value_is_preserved():
    assert CounterOcr.parse_counter_result("10/10") == [10, 10]


def test_counter_ocr_invalid_total_is_fully_blocked():
    assert CounterOcr.parse_counter_result("1/") == [0, 0]
    assert CounterOcr.parse_counter_result("garbage") == [0, 0]


def test_counter_ocr_impossible_current_is_blocked():
    assert CounterOcr.parse_counter_result("12/10") == [0, 10]


def test_rich_tracebacks_do_not_dump_frame_locals_to_live_logs():
    source = LOGGER_SOURCE.read_text(encoding="utf-8")

    assert "tracebacks_show_locals=True" not in source
    assert source.count("tracebacks_show_locals=False") >= 3
