from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from rich.console import Group
from rich.rule import Rule
from rich.text import Text

from module.application.runtime_log_projection import RuntimeLogEvent
from module.webui.widgets import RichLog


def _log() -> RichLog:
    log = RichLog.__new__(RichLog)
    log.render = lambda value: f"<{value}>"
    log.extend = Mock()
    log.reset = Mock()
    return log


def test_rich_log_renders_only_new_items_from_append_only_source() -> None:
    source = SimpleNamespace(renderables=[], renderables_total=0)
    log = _log()
    stream = log.put_log(source)
    next(stream)

    source.renderables.extend(("first", "second"))
    source.renderables_total = 2
    next(stream)
    source.renderables.append("third")
    source.renderables_total = 3
    next(stream)

    assert [call.args[0] for call in log.extend.call_args_list] == [
        "<first><second>",
        "<third>",
    ]
    log.reset.assert_not_called()


def test_rich_log_rebuilds_after_append_only_source_truncation() -> None:
    source = SimpleNamespace(
        renderables=["first", "second", "third"], renderables_total=3
    )
    log = _log()
    stream = log.put_log(source)
    next(stream)
    next(stream)

    source.renderables[:] = ["third", "fourth", "fifth"]
    source.renderables_total = 5
    next(stream)

    log.reset.assert_called_once_with()
    assert [call.args[0] for call in log.extend.call_args_list] == [
        "<first><second><third>",
        "<third><fourth><fifth>",
    ]


def test_runtime_section_events_render_as_rich_rules() -> None:
    section = RuntimeLogEvent(
        timestamp="2026-09-25T10:00:00.000+07:00",
        level=20,
        level_name="INFO",
        message="СЕКЦИЯ",
        kind="section",
        section_level=1,
    )

    rendered = RichLog._runtime_log_renderable(section)

    assert isinstance(rendered, Rule)
    assert rendered.characters == "═"
    assert rendered.title == "СЕКЦИЯ"


def test_runtime_section_level_two_uses_thin_rule() -> None:
    section = RuntimeLogEvent(
        timestamp="",
        level=20,
        level_name="INFO",
        message="ПОДРАЗДЕЛ",
        kind="section",
        section_level=2,
    )

    rendered = RichLog._runtime_log_renderable(section)

    assert isinstance(rendered, Rule)
    assert rendered.characters == "─"
    assert rendered.title == "ПОДРАЗДЕЛ"


def test_rich_log_put_log_renders_structured_runtime_events() -> None:
    section = RuntimeLogEvent(
        timestamp="",
        level=20,
        level_name="INFO",
        message="СЕКЦИЯ",
        kind="section",
        section_level=1,
    )
    source = SimpleNamespace(
        renderables=[section],
        refresh_renderables=lambda: True,
    )
    log = _log()
    rendered: list[object] = []
    log.render = lambda value: rendered.append(value) or "rendered"
    stream = log.put_log(source)
    next(stream)
    next(stream)

    assert len(rendered) == 1
    assert isinstance(rendered[0], Rule)
    log.extend.assert_called_once_with("rendered")


def test_runtime_section_level_three_is_bold_and_keeps_traceback() -> None:
    event = RuntimeLogEvent(
        timestamp="2026-09-25T10:00:00.000+07:00",
        level=20,
        level_name="INFO",
        message="<<< СЕКЦИЯ >>>",
        kind="section",
        section_level=3,
        traceback="ValueError: ошибка",
    )

    rendered = RichLog._runtime_log_renderable(event)

    assert isinstance(rendered, Text)
    assert rendered.plain.endswith("<<< СЕКЦИЯ >>>\nValueError: ошибка")
    assert any("bold" in str(span.style) for span in rendered.spans)
    assert any(span.style == "dim" for span in rendered.spans)
    assert "[bold]" not in rendered.plain


def test_runtime_warning_and_error_levels_have_distinct_colors() -> None:
    warning = RuntimeLogEvent(
        timestamp="2026-09-25T10:00:00.000+07:00",
        level=30,
        level_name="WARNING",
        message="Предупреждение",
    )
    error = RuntimeLogEvent(
        timestamp="2026-09-25T10:00:01.000+07:00",
        level=40,
        level_name="ERROR",
        message="Ошибка",
    )

    warning_line = RichLog._runtime_log_renderable(warning)
    error_line = RichLog._runtime_log_renderable(error)

    assert isinstance(warning_line, Text)
    assert isinstance(error_line, Text)
    assert any(span.style == "yellow" for span in warning_line.spans)
    assert any(span.style == "red" for span in error_line.spans)


def test_level_zero_runtime_section_keeps_all_three_rules() -> None:
    event = RuntimeLogEvent(
        timestamp="",
        level=20,
        level_name="INFO",
        message="ЗАГОЛОВОК",
        kind="section",
        section_level=0,
    )

    rendered = RichLog._runtime_log_renderable(event)

    assert isinstance(rendered, Group)
    assert len(rendered.renderables) == 3
    assert [rule.characters for rule in rendered.renderables] == ["═", " ", "═"]
    assert rendered.renderables[1].title == "ЗАГОЛОВОК"
