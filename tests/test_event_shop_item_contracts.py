from module.shop_event.item import CounterOcr


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
