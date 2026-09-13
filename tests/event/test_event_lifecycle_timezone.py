from datetime import datetime, timezone

from module.config.utils import SERVER_TO_TIMEZONE
from module.event_datamine.discovery import EventCandidate, lifecycle
from module.event_datamine.registry import artifact_lifecycle


def _aware_utc_for_en_wall_time(value: datetime) -> datetime:
    server_zone = timezone(SERVER_TO_TIMEZONE["en"])
    return value.replace(tzinfo=server_zone).astimezone(timezone.utc)


def test_discovery_lifecycle_converts_aware_time_to_server_wall_clock():
    candidate = EventCandidate(
        id="en:test",
        server="EN",
        activity_id=1,
        mark=1,
        name="Тестовое событие",
        farm_start="2026-08-13 00:00:00",
        farm_end="2026-08-26 23:59:59",
        shop_end="2026-09-03 23:59:59",
        campaign_activity_ids=(1,),
        related_activity_ids=(1,),
        map_ids=(1,),
    )
    aware_utc = _aware_utc_for_en_wall_time(datetime(2026, 8, 26, 23, 0))

    assert lifecycle(candidate, aware_utc) == "active"
    assert lifecycle(candidate, datetime(2026, 8, 27, 6, 0)) == "redemption"


def test_registry_lifecycle_uses_the_same_server_wall_clock_contract():
    entry = {
        "role": "production",
        "server": "EN",
        "farm_start": "2026-08-13 00:00:00",
        "farm_end": "2026-08-26 23:59:59",
        "shop_end": "2026-09-03 23:59:59",
    }
    aware_utc = _aware_utc_for_en_wall_time(datetime(2026, 8, 26, 23, 0))

    assert artifact_lifecycle(entry, aware_utc) == "active"
    assert artifact_lifecycle(entry, datetime(2026, 8, 27, 6, 0)) == "redemption"
