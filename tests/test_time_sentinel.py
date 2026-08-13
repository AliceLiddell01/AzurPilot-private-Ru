from datetime import datetime

from module.config.time_sentinel import (
    DEFAULT_TIME_TEXT,
    LEGACY_DEFAULT_TIME,
    is_default_time,
)
from module.config.utils import DEFAULT_TIME


def test_exact_default_times_are_recognized():
    assert is_default_time(LEGACY_DEFAULT_TIME) is True
    assert is_default_time(DEFAULT_TIME) is True
    assert is_default_time("2020-01-01 00:00:00") is True
    assert is_default_time("2023-01-01T00:00:00") is True
    assert DEFAULT_TIME_TEXT == "2023-01-01 00:00:00"


def test_real_historical_dates_are_not_treated_as_disabled():
    assert is_default_time(datetime(2023, 7, 1, 0, 0)) is False
    assert is_default_time("2020-08-13 00:00:00") is False
    assert is_default_time("2023-01-01 00:00:01") is False
    assert is_default_time("") is False
    assert is_default_time("not-a-date") is False
