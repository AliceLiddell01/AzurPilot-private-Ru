from __future__ import annotations

from unittest.mock import Mock

from rich.text import Text

from module.application import bot_runtime_client
from module.application.runtime_log_projection import RuntimeLogEvent
from module.webui.widgets import RichLog


def test_profile_client_refreshes_structured_logs_for_rich_log(monkeypatch) -> None:
    event = RuntimeLogEvent(
        timestamp="2026-09-25T10:00:00.000+07:00",
        level=20,
        level_name="INFO",
        message="<<< СЕКЦИЯ >>>",
        kind="section",
        section_level=3,
    )
    signatures = iter(
        (
            (("alpha.log", 1, 100),),
            (("alpha.log", 1, 100),),
            (("alpha.log", 2, 200),),
        )
    )
    read_calls: list[str] = []

    def read_signature(profile: str, **_kwargs: object):
        assert profile == "alpha"
        return next(signatures)

    def read_events(profile: str, **_kwargs: object):
        read_calls.append(profile)
        return (event,)

    monkeypatch.setattr(bot_runtime_client, "runtime_log_signature", read_signature)
    monkeypatch.setattr(bot_runtime_client, "read_runtime_log_events", read_events)

    client = bot_runtime_client._ProfileClient("alpha")

    assert client.refresh_renderables() is True
    assert client.renderables == [event]
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

    assert read_calls == ["alpha", "alpha"]
    assert len(rendered) == 1
    assert isinstance(rendered[0], Text)
    assert rendered[0].plain.endswith("<<< СЕКЦИЯ >>>")
    assert "[bold]" not in rendered[0].plain
    assert any("bold" in str(span.style) for span in rendered[0].spans)
