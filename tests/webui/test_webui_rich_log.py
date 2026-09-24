from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

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
    source = SimpleNamespace(renderables=["first", "second", "third"], renderables_total=3)
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
