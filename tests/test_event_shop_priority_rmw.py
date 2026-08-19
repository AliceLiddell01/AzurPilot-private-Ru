from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import module.webui.event_shop_priority as priority


def test_priority_writers_share_one_read_modify_write_lock(monkeypatch, tmp_path):
    first_load_entered = Event()
    release_first_load = Event()
    second_load_entered = Event()
    counter_guard = Lock()
    call_count = 0
    original_load = priority.load_event_shop_priority

    def blocking_load(*args, **kwargs):
        nonlocal call_count
        with counter_guard:
            call_count += 1
            current_call = call_count
        if current_call == 1:
            first_load_entered.set()
            assert release_first_load.wait(timeout=5)
        else:
            second_load_entered.set()
        return original_load(*args, **kwargs)

    monkeypatch.setattr(priority, "load_event_shop_priority", blocking_load)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            priority.set_event_shop_priority,
            "test-instance",
            "event-test",
            11,
            0,
            root=tmp_path,
        )
        assert first_load_entered.wait(timeout=5)
        second = executor.submit(
            priority.set_event_shop_priority,
            "test-instance",
            "event-test",
            12,
            1,
            root=tmp_path,
        )
        try:
            assert not second_load_entered.wait(timeout=1.0)
        finally:
            release_first_load.set()

        assert second_load_entered.wait(timeout=5)
        first.result(timeout=5)
        second.result(timeout=5)

    stored = original_load(
        "test-instance",
        "event-test",
        root=tmp_path,
    )
    assert stored["priorities"] == {"11": 0, "12": 1}
