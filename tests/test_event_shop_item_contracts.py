from pathlib import Path

from module.shop_event.item import CounterOcr


ROOT = Path(__file__).resolve().parents[1]
LOGGER_SOURCE = ROOT / "module" / "logger.py"


def test_counter_ocr_parse_contract_is_fail_closed():
    cases = (
        ("/1", [0, 1]),
        ("10/10", [10, 10]),
        ("1/", [0, 0]),
        ("garbage", [0, 0]),
        ("12/10", [0, 10]),
    )
    for raw, expected in cases:
        assert CounterOcr.parse_counter_result(raw) == expected, raw


def test_rich_tracebacks_do_not_dump_frame_locals_to_live_logs():
    source = LOGGER_SOURCE.read_text(encoding="utf-8")

    assert "tracebacks_show_locals=True" not in source
    assert source.count("tracebacks_show_locals=False") >= 3
