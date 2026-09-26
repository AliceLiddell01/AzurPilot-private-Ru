from __future__ import annotations

from unittest.mock import Mock

from rich.text import Text

from module.application import bot_runtime_client
from module.application.runtime_log_projection import RuntimeLogEvent
from module.webui.widgets import RichLog


def test_profile_client_refreshes_structured_logs_for_rich_log(monkeypatch) -> None:
    section = RuntimeLogEvent(
        timestamp="2026-09-25T10:00:00.000+07:00",
        level=20,
        level_name="INFO",
        message="<<< СЕКЦИЯ >>>",
        kind="section",
        section_level=3,
    )
    second = RuntimeLogEvent(
        timestamp="2026-09-25T10:00:00.100+07:00",
        level=20,
        level_name="INFO",
        message="Вторая строка",
    )
    third = RuntimeLogEvent(
        timestamp="2026-09-25T10:00:00.200+07:00",
        level=20,
        level_name="INFO",
        message="Третья строка",
    )
    signatures = iter(
        (
            (("alpha.log", 1, 100),),
            (("alpha.log", 1, 100),),
            (("alpha.log", 2, 200),),
            (("alpha.log", 3, 300),),
        )
    )
    event_snapshots = iter(
        (
            (section,),
            (section, second),
            (section, second, third),
        )
    )
    read_calls: list[str] = []

    def read_signature(profile: str, **_kwargs: object):
        assert profile == "alpha"
        return next(signatures)

    def read_events(profile: str, **_kwargs: object):
        read_calls.append(profile)
        return next(event_snapshots)

    monkeypatch.setattr(bot_runtime_client, "runtime_log_signature", read_signature)
    monkeypatch.setattr(bot_runtime_client, "read_runtime_log_events", read_events)

    client = bot_runtime_client._ProfileClient("alpha")

    assert client.refresh_renderables() is True
    assert client.renderables == [section]
    assert client.renderables_total == 1
    assert client.refresh_renderables() is False

    log = RichLog.__new__(RichLog)
    rendered: list[object] = []
    log.render = lambda item: rendered.append(item) or "rendered"
    log.reset = Mock()
    log.extend = Mock()
    stream = log.put_log(client)
    next(stream)
    next(stream)
    next(stream)

    assert read_calls == ["alpha", "alpha", "alpha"]
    assert client.renderables == [section, second, third]
    assert client.renderables_total == 3
    assert len(rendered) == 3
    assert isinstance(rendered[0], Text)
    assert rendered[0].plain.endswith("<<< СЕКЦИЯ >>>")
    assert isinstance(rendered[1], Text)
    assert rendered[1].plain.endswith("Вторая строка")
    assert isinstance(rendered[2], Text)
    assert rendered[2].plain.endswith("Третья строка")
    assert "[bold]" not in rendered[0].plain
    assert any("bold" in str(span.style) for span in rendered[0].spans)
    assert [call.args[0] for call in log.extend.call_args_list] == [
        "renderedrendered",
        "rendered",
    ]
    log.reset.assert_not_called()


def test_profile_client_rebases_when_runtime_log_continuity_is_lost(monkeypatch) -> None:
    first = RuntimeLogEvent("", 20, "INFO", "Первая")
    replacement = RuntimeLogEvent("", 20, "INFO", "Новая история")
    signatures = iter(
        (
            (("alpha.log", 1, 100),),
            (("alpha.log", 2, 50),),
        )
    )
    snapshots = iter(((first,), (replacement,)))
    monkeypatch.setattr(
        bot_runtime_client,
        "runtime_log_signature",
        lambda *_args, **_kwargs: next(signatures),
    )
    monkeypatch.setattr(
        bot_runtime_client,
        "read_runtime_log_events",
        lambda *_args, **_kwargs: next(snapshots),
    )

    client = bot_runtime_client._ProfileClient("alpha")

    assert client.refresh_renderables() is True
    assert client.renderables_generation == 0
    assert client.refresh_renderables() is True
    assert client.renderables == [replacement]
    assert client.renderables_generation == 1


def _runtime_event(message: str) -> RuntimeLogEvent:
    return RuntimeLogEvent("", 20, "INFO", message)


def test_projected_delta_keeps_repeated_neighbour_events():
    first = _runtime_event("Одинаковое событие")
    second = _runtime_event("Одинаковое событие")
    project = bot_runtime_client._ProfileClient._projected_delta

    assert project((first,), (first, second)) == (second,)
    assert project((first, first), (first, first, second)) == (second,)
    assert project((first,), (first, first, first)) == (first, first)


def test_projected_delta_contract_covers_append_rotation_and_lost_continuity():
    first = _runtime_event("Первое")
    second = _runtime_event("Второе")
    third = _runtime_event("Третье")
    fourth = _runtime_event("Четвёртое")
    replacement = _runtime_event("Новая история")
    project = bot_runtime_client._ProfileClient._projected_delta

    assert project((), (first, second)) == (first, second)
    assert project((first, second), (first, second)) == ()
    assert project((first, second), (first, second, third)) == (third,)
    # Ротация/compaction обрезает старые события, но новое не теряется.
    assert project((first, second, third, fourth), (third, fourth, replacement)) == (
        replacement,
    )
    assert project((first, second, third), (second, third, fourth)) == (fourth,)
    # Непрерывность доказать нельзя: вызывающая сторона выполняет controlled rebuild.
    assert project((first,), (replacement,)) is None
    assert project((first, second), (first, replacement, second, third)) is None
