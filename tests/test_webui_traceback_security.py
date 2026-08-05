from __future__ import annotations

import logging
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from module.logger import HTMLConsole, RichRenderableHandler, sanitize_traceback_text
from module.webui import utils


class _TagCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.ids: list[str] = []
        self.text_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.append(str(attributes["id"]))

    def handle_data(self, data: str) -> None:
        self.text_chunks.append(data)

    @property
    def text(self) -> str:
        return "".join(self.text_chunks)


def _collect_html(value: str) -> _TagCollector:
    collector = _TagCollector()
    collector.feed(value)
    return collector


class BrokenRepr:
    def __repr__(self) -> str:
        raise RuntimeError("repr недоступен")


def _recursive_exception(x: int, password: str = "do-not-leak") -> None:
    credential_payload = "".join(("private", "-token"))
    normal_value = "видимое значение"
    long_local = {
        "items": list(range(30)),
        "long": "широкое значение " * 30,
        "broken": BrokenRepr(),
        "normal": normal_value,
    }
    if x > 0:
        _recursive_exception(x - 1, password)
        return
    raise Exception(
        "quq <script id='stage7-owned'>bad()</script> "
        f"Authorization: Bearer {credential_payload} \x1b[31mRED\u202e"
    )


def _render_recursive(*, dark_theme: bool) -> str:
    try:
        _recursive_exception(3)
    except Exception as exc:
        traceback = exc.__traceback__
        while traceback and traceback.tb_frame.f_code.co_name != "_recursive_exception":
            traceback = traceback.tb_next
        return utils.render_webui_traceback(
            (type(exc), exc, traceback),
            dark_theme=dark_theme,
        )
    raise AssertionError("Тестовое исключение не возникло")


class WebUITracebackRenderingTest(unittest.TestCase):
    def test_recursive_exception_is_structured_and_redacted(self) -> None:
        rendered = _render_recursive(dark_theme=True)
        collector = _collect_html(rendered)
        self.assertEqual(rendered.count("rich-traceback-container"), 1)
        self.assertIn('class="rich-traceback-code"', rendered)
        self.assertIn("Exception: quq", collector.text)
        self.assertIn("видимое значение", collector.text)
        self.assertIn("<скрыто>", collector.text)
        self.assertNotIn("do-not-leak", collector.text)
        self.assertNotIn("private-token", collector.text)
        self.assertNotIn("\x1b", collector.text)
        self.assertNotIn("\u202e", collector.text)
        self.assertIn("<PROJECT_ROOT>", collector.text)
        self.assertNotIn(str(Path.cwd().resolve()), collector.text)

    def test_exception_payload_cannot_create_dom_nodes(self) -> None:
        rendered = _render_recursive(dark_theme=False)
        collector = _collect_html(rendered)
        self.assertNotIn("script", collector.tags)
        self.assertNotIn("stage7-owned", collector.ids)
        self.assertIn("<script id='stage7-owned'>bad()</script>", collector.text)

    def test_renderer_requires_active_exception(self) -> None:
        with self.assertRaisesRegex(ValueError, "активное исключение"):
            utils.render_webui_traceback((None, None, None), dark_theme=True)

    def test_rich_handler_redacts_before_callback(self) -> None:
        renderables = []
        handler = RichRenderableHandler(
            func=renderables.append,
            console=HTMLConsole(width=100, color_system="truecolor"),
            show_path=False,
            show_time=False,
            show_level=True,
            rich_tracebacks=True,
            tracebacks_show_locals=True,
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        try:
            _recursive_exception(1)
        except Exception:
            record = logging.LogRecord(
                "stage7", logging.ERROR, __file__, 1, "Ошибка fixture", (), sys.exc_info()
            )
            handler.handle(record)
        self.assertEqual(len(renderables), 1)
        console = Console(no_color=True, width=100)
        with console.capture() as capture:
            console.print(renderables[0])
        text = capture.get()
        self.assertIn("Ошибка fixture", text)
        self.assertIn("Exception: quq", text)
        self.assertIn("<скрыто>", text)
        self.assertNotIn("do-not-leak", text)
        self.assertNotIn("private-token", text)
        self.assertEqual(record.levelno, logging.ERROR)

    def test_error_modal_uses_one_safe_traceback(self) -> None:
        popup_calls = []
        javascript_calls = []

        with (
            patch.object(utils.logger, "exception") as log_exception,
            patch.object(utils, "put_html", side_effect=lambda value: value),
            patch.object(utils, "popup", side_effect=lambda *a, **kw: popup_calls.append((a, kw))),
            patch.object(utils, "run_js", side_effect=lambda script, **kw: javascript_calls.append((script, kw))),
        ):
            try:
                _recursive_exception(2)
            except Exception:
                utils.on_task_exception(None)

        log_exception.assert_called_once_with("[WebUI] В приложении произошла внутренняя ошибка")
        self.assertEqual(len(popup_calls), 1)
        modal_html = popup_calls[0][1]["content"]
        self.assertEqual(modal_html.count("rich-traceback-container"), 1)
        self.assertNotIn("private-token", modal_html)
        self.assertEqual(len(javascript_calls), 1)
        self.assertNotIn("private-token", javascript_calls[0][1]["traceback_msg"])

    def test_css_preserves_terminal_grid_and_local_overflow(self) -> None:
        root = Path(__file__).resolve().parents[1]
        base_css = (root / "assets/gui/css/alas.css").read_text(encoding="utf-8")
        advanced_css = (root / "assets/gui/css/advanced-material-alas.css").read_text(encoding="utf-8")
        self.assertIn(":not(.rich-traceback-container *)", base_css)
        self.assertIn(":not(.rich-traceback-container *)", advanced_css)
        for declaration in (
            "font-variant-ligatures: none",
            "white-space: pre",
            "overflow-x: auto",
            "width: max-content",
            "max-width: 100%",
            "box-sizing: border-box",
        ):
            with self.subTest(declaration=declaration):
                self.assertIn(declaration, base_css)

    def test_plain_redaction_keeps_diagnostic_shape(self) -> None:
        raw = (
            "https://user:password@example.test/a?token=secret\n"
            "Authorization: Bearer private-token\n"
            "ValueError: quq"
        )
        sanitized = sanitize_traceback_text(raw)
        self.assertEqual(sanitized.count("\n"), raw.count("\n"))
        self.assertIn("https://***@example.test/a?token=***", sanitized)
        self.assertIn("Authorization:***", sanitized)
        self.assertIn("ValueError: quq", sanitized)
        self.assertNotIn("private-token", sanitized)

    def test_dark_and_light_fixtures_preserve_safe_semantics(self) -> None:
        fixture_dir = Path(__file__).parent / "fixtures/webui_traceback"
        for theme in ("light", "dark"):
            with self.subTest(theme=theme):
                fixture = (fixture_dir / f"fixture-{theme}.html").read_text(encoding="utf-8")
                collector = _collect_html(fixture)
                self.assertEqual(fixture.count("rich-traceback-container"), 1)
                self.assertNotIn("script", collector.tags)
                self.assertNotIn("stage7-owned", collector.ids)
                self.assertIn("Exception: quq", collector.text)
                self.assertIn("<script id='stage7-owned'>bad()</script>", collector.text)
                self.assertIn("видимое значение", collector.text)
                self.assertIn("<скрыто>", collector.text)
                self.assertNotIn("private-token", collector.text)
                self.assertNotIn("do-not-leak", collector.text)
                self.assertIn("../../../assets/gui/css/alas.css", fixture)
                self.assertNotIn(str(Path.cwd().resolve()), collector.text)


if __name__ == "__main__":
    unittest.main()
