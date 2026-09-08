"""Система журналирования AzurPilot.

Модуль построен на Rich и поддерживает вывод в консоль, потоковую отрисовку
в WebUI и bounded in-memory контекст для incident-ов. Глобальный экземпляр
``logger`` с именем ``alas`` используется всем приложением.

Основные компоненты:
    - ``RichRenderableHandler`` — передаёт отрисованные объекты callback-функции WebUI.
    - ``HTMLConsole`` — Rich Console для HTML/WebUI.
    - ``Highlighter`` — подсветка путей, времени и технических значений.

Вспомогательные функции ``hr()``, ``attr()``, ``attr_align()``,
``error_context()`` и ``exception_context()`` добавляются к глобальному logger
как единая точка журналирования проекта.
"""

import logging
import os
import sys
from typing import Callable, List

from rich.console import Console, ConsoleOptions, ConsoleRenderable, NewLine
from rich.highlighter import RegexHighlighter
from rich.logging import RichHandler
from rich.pretty import Node
from rich.rule import Rule
from rich.style import Style
from rich.theme import Theme
from rich.traceback import Traceback

from module.logging_core import (
    DiagnosticContextHandler,
    RepeatedEventSuppressor,
    _SENSITIVE_NAME_RE,
    sanitize_traceback_text,
)

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')


def empty_function(*args, **kwargs):
    pass


# cnocr настраивает root logger внутри cnocr.utils. Отключаем
# logging.basicConfig, чтобы сообщения не выводились дважды.
logging.basicConfig = empty_function
logging.raiseExceptions = True  # Позволяет увидеть ошибки кодировки в консоли.

# Убираем HTTP-ключевые слова (GET, POST и т. п.), чтобы не подсвечивать их ошибочно.
RichHandler.KEYWORDS = []

def _redact_rich_node(node: Node) -> None:
    node.key_repr = sanitize_traceback_text(node.key_repr)
    node.value_repr = sanitize_traceback_text(node.value_repr)
    if node.children:
        for child in node.children:
            _redact_rich_node(child)


def sanitize_rich_traceback(renderable: Traceback) -> Traceback:
    """Очистить Rich traceback до передачи в WebUI или HTML exporter."""
    for stack in renderable.trace.stacks:
        stack.exc_value = sanitize_traceback_text(stack.exc_value)
        for frame in stack.frames:
            frame.filename = sanitize_traceback_text(frame.filename)
            frame.name = sanitize_traceback_text(frame.name)
            frame.line = sanitize_traceback_text(frame.line)
            if not frame.locals:
                continue
            for name in list(frame.locals):
                if name.startswith("_"):
                    del frame.locals[name]
                    continue
                if _SENSITIVE_NAME_RE.search(name):
                    frame.locals[name] = Node(value_repr="'<скрыто>'")
                    continue
                _redact_rich_node(frame.locals[name])
    return renderable


class RichRenderableHandler(RichHandler):
    """Передавать отрисованный объект журнала в callback-функцию."""

    def __init__(self, *args, func: Callable[[ConsoleRenderable], None] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._func = func

    def emit(self, record: logging.LogRecord) -> None:
        message = self.format(record)
        traceback = None
        if (
                self.rich_tracebacks
                and record.exc_info
                and record.exc_info != (None, None, None)
        ):
            exc_type, exc_value, exc_traceback = record.exc_info
            assert exc_type is not None
            assert exc_value is not None
            traceback = Traceback.from_exception(
                exc_type,
                exc_value,
                exc_traceback,
                width=self.tracebacks_width,
                extra_lines=self.tracebacks_extra_lines,
                theme=self.tracebacks_theme,
                word_wrap=self.tracebacks_word_wrap,
                show_locals=self.tracebacks_show_locals,
                locals_max_length=self.locals_max_length,
                locals_max_string=self.locals_max_string,
            )
            sanitize_rich_traceback(traceback)
            message = record.getMessage()
            if self.formatter:
                record.message = record.getMessage()
                formatter = self.formatter
                if hasattr(formatter, "usesTime") and formatter.usesTime():
                    record.asctime = formatter.formatTime(
                        record, formatter.datefmt)
                message = formatter.formatMessage(record)

        message_renderable = self.render_message(record, message)
        log_renderable = self.render(
            record=record, traceback=traceback, message_renderable=message_renderable
        )

        # Передаём готовый Rich-объект непосредственно callback-функции.
        self._func(log_renderable)

    def handle(self, record: logging.LogRecord) -> bool:
        if not self._func:
            return True
        super().handle(record)


class HTMLConsole(Console):
    """Rich Console с принудительно включёнными возможностями для Web-вывода.

    Часть возможностей пока не используется.
    """

    @property
    def options(self) -> ConsoleOptions:
        return ConsoleOptions(
            max_height=self.size.height,
            size=self.size,
            legacy_windows=False,
            min_width=1,
            max_width=self.width,
            encoding='utf-8',
            is_terminal=False,
        )


class Highlighter(RegexHighlighter):
    base_style = 'web.'
    highlights = [
        (r'(?P<time>([0-1]{1}\d{1}|[2]{1}[0-3]{1})(?::)?'
         r'([0-5]{1}\d{1})(?::)?([0-5]{1}\d{1})(.\d+\b))'),
        r"(?P<brace>[\{\[\(\)\]\}])",
        r"\b(?P<bool_true>True)\b|\b(?P<bool_false>False)\b|\b(?P<none>None)\b",
        r"(?P<path>(([A-Za-z]\:)|.)?\B([\/\\][\w\.\-\_\+]+)*[\/\\])(?P<filename>[\w\.\-\_\+]*)?",
    ]


WEB_THEME = Theme({
    "web.brace": Style(bold=True),
    "web.bool_true": Style(color="bright_green", italic=True),
    "web.bool_false": Style(color="bright_red", italic=True),
    "web.none": Style(color="magenta", italic=True),
    "web.path": Style(color="magenta"),
    "web.filename": Style(color="bright_magenta"),
    "web.str": Style(color="green", italic=False, bold=False),
    "web.time": Style(color="cyan"),
    "rule.text": Style(bold=True),
})

# Центральный logger принимает DEBUG, а пользовательские sinks фильтруют его сами.
logger_debug = False
logger = logging.getLogger('alas')
logger.setLevel(logging.DEBUG)
logger.propagate = False
console_formatter = logging.Formatter(
    fmt='%(asctime)s.%(msecs)03d │ %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
web_formatter = logging.Formatter(
    fmt='%(asctime)s.%(msecs)03d │ %(message)s', datefmt='%H:%M:%S')

diagnostic_hdlr = DiagnosticContextHandler(
    capacity=200,
    sanitizer=sanitize_traceback_text,
)
logger.addHandler(diagnostic_hdlr)

# Консольный обработчик стандартного logging оставлен в истории как заменённый Rich.
stdout_console = console = Console()
console_hdlr = RichHandler(
    show_path=False,
    show_time=False,
    rich_tracebacks=True,
    tracebacks_show_locals=False,
    tracebacks_extra_lines=3,
)
console_hdlr.setLevel(logging.DEBUG if logger_debug else logging.INFO)
console_hdlr.setFormatter(console_formatter)
logger.addHandler(console_hdlr)

# Гарантируем запуск из корня AzurPilot.
os.chdir(os.path.join(os.path.dirname(__file__), '../'))

# Имя процесса используется только как default для application observability.
pyw_name = os.path.splitext(os.path.basename(sys.argv[0]))[0]


def _configure_application_observability(profile, *, component=None):
    """Явно подключить удалённое логирование после настройки runtime logger."""
    try:
        from module.observability import configure_application_observability

        configure_application_observability(
            logger,
            default_profile=profile,
            default_component=component,
        )
    except Exception as exc:
        # Ошибка необязательной телеметрии не должна менять поведение игрового logger.
        try:
            sys.stderr.write(
                '[AzurPilot] Не удалось подключить удалённый журнал; '
                'работа WebUI/консоли продолжится, bounded incident-контекст '
                f'останется доступен ({type(exc).__name__}).\n'
            )
        except Exception:
            pass


def configure_runtime_logging(
    name=pyw_name,
    *,
    observability_profile=None,
    observability_component=None,
):
    if observability_profile is None and observability_component is None:
        observability_profile = name
    # Обычный runtime не создаёт локальный файл: console/WebUI и bounded
    # in-memory incident context остаются доступными независимо от OTLP.
    _configure_application_observability(
        observability_profile,
        component=observability_component,
    )



def set_func_logger(func):
    console = HTMLConsole(
        force_terminal=False,
        force_interactive=False,
        width=80,
        color_system='truecolor',
        markup=False,
        safe_box=False,
        highlighter=Highlighter(),
        theme=WEB_THEME
    )
    hdlr = RichRenderableHandler(
        func=func,
        console=console,
        show_path=False,
        show_time=False,
        show_level=True,
        rich_tracebacks=True,
        tracebacks_show_locals=False,
        tracebacks_extra_lines=2,
        highlighter=Highlighter(),
    )
    hdlr.setLevel(logging.DEBUG if logger_debug else logging.INFO)
    hdlr.setFormatter(web_formatter)
    logger.handlers = [h for h in logger.handlers if not isinstance(
        h, RichRenderableHandler)]
    logger.addHandler(hdlr)


def _get_renderables(
        self: Console, *objects, sep=" ", end="\n", justify=None, emoji=None, markup=None, highlight=None,
) -> List[ConsoleRenderable]:
    """Получить список Rich-объектов для последующей отрисовки.

    Реализация соответствует сборке объектов в ``rich.console.Console.print()``.
    """
    if not objects:
        objects = (NewLine(),)

    render_hooks = self._render_hooks[:]
    with self:
        renderables = self._collect_renderables(
            objects,
            sep,
            end,
            justify=justify,
            emoji=emoji,
            markup=markup,
            highlight=highlight,
        )
        for hook in render_hooks:
            renderables = hook.process_renderables(renderables)
    return renderables


def print(*objects: ConsoleRenderable, **kwargs):
    for hdlr in logger.handlers:
        if isinstance(hdlr, RichRenderableHandler):
            for renderable in _get_renderables(hdlr.console, *objects, **kwargs):
                hdlr._func(renderable)
        elif isinstance(hdlr, RichHandler):
            hdlr.console.print(*objects)


def rule(title="", *, characters="─", style="rule.line", end="\n", align="center"):
    rule = Rule(title=title, characters=characters,
                style=style, end=end, align=align)
    print(rule)


def hr(title, level=3):
    title = str(title).upper()
    if level == 1:
        logger.rule(title, characters='═')
    if level == 2:
        logger.rule(title, characters='─')
    if level == 3:
        logger.info(f"[bold]<<< {title} >>>[/bold]", extra={"markup": True})
    if level == 0:
        logger.rule(characters='═')
        logger.rule(title, characters=' ')
        logger.rule(characters='═')


def attr(name, text):
    logger.info('[%s] %s' % (str(name), str(text)))


def attr_align(name, text, front='', align=22):
    name = str(name).rjust(align)
    if front:
        name = front + name[len(front):]
    logger.info('%s: %s' % (name, str(text)))


_SUPPRESSION_PAYLOAD_DEFAULT = object()
_event_suppressor = RepeatedEventSuppressor(max_keys=256, default_window=5.0)


def _emit_suppression_summary(decision):
    if decision.summary_count <= 0:
        return
    logger.log(
        decision.summary_level,
        '[Повторы] %s — повторено %d раз за %.1f с' % (
            decision.summary_message,
            decision.summary_count,
            decision.summary_duration,
        ),
    )


def log_suppressed(level, message, *, key=None, payload=_SUPPRESSION_PAYLOAD_DEFAULT, window=None):
    """Записать событие через bounded suppression-контракт."""
    message = str(message)
    if key is None:
        key = message
    if payload is _SUPPRESSION_PAYLOAD_DEFAULT:
        payload = message
    decision = _event_suppressor.observe(
        key,
        payload=payload,
        level=level,
        message=message,
        window=window,
    )
    _emit_suppression_summary(decision)
    if decision.emit:
        logger.log(level, message)
    return decision.emit


def finish_suppressed(key):
    """Завершить серию повторов и при необходимости вывести summary."""
    decision = _event_suppressor.finish(key)
    _emit_suppression_summary(decision)
    return decision.summary_count


def reset_suppression(key=None):
    _event_suppressor.reset(key)


def get_diagnostic_context(*, last_failure=False):
    """Вернуть безопасные сообщения текущего или последнего failure-контекста."""
    return tuple(
        record.getMessage()
        for record in diagnostic_hdlr.snapshot(last_failure=last_failure)
    )


def reset_diagnostic_context():
    diagnostic_hdlr.reset()


def show():
    logger.info('INFO')
    logger.warning('WARNING')
    logger.debug('DEBUG')
    logger.error('ERROR')
    logger.critical('CRITICAL')
    logger.hr('hr0', 0)
    logger.hr('hr1', 1)
    logger.hr('hr2', 2)
    logger.hr('hr3', 3)
    logger.info(r'Скобки { [ ( ) ] }')
    logger.info(r'True, False, None')
    logger.info(r'E:/path\\to/alas/alas.exe, /root/alas/, ./relative/path/log.txt')
    local_var1 = 'This is local variable'
    # Строка перед тестовым исключением.
    raise Exception("Exception")


def error_context(title, reason, impact, action, exc=None, level=logging.ERROR, with_traceback=None):
    """Вывести унифицированную ошибку с причиной, влиянием и рекомендацией.

    При ``with_traceback=None`` сохраняется прежнее поведение: если передан
    объект исключения, выводится полная трассировка.
    """
    message = '\n'.join([
        f'[Ошибка] {title}',
        f'Причина: {reason}',
        f'Влияние: {impact}',
        f'Рекомендация: {action}',
    ])
    if exc is not None:
        message += f'\nИсключение: {type(exc).__name__}: {exc}'
    if with_traceback is None:
        with_traceback = exc is not None
    logger.log(level, message, exc_info=with_traceback)


def exception_context(title, exc, impact, action, level=logging.ERROR):
    """Вывести неизвестное исключение в едином формате, сохранив трассировку."""
    error_context(
        title=title,
        reason=f'Программа вызвала исключение {type(exc).__name__}; точную причину определите по трассировке ниже.',
        impact=impact,
        action=action,
        exc=exc,
        level=level,
    )


logger.error_context = error_context
logger.exception_context = exception_context
logger.hr = hr
logger.attr = attr
logger.attr_align = attr_align
logger.configure_runtime_logging = configure_runtime_logging
logger.set_func_logger = set_func_logger
logger.rule = rule
logger.print = print
logger.log_suppressed = log_suppressed
logger.finish_suppressed = finish_suppressed
logger.reset_suppression = reset_suppression
logger.get_diagnostic_context = get_diagnostic_context
logger.reset_diagnostic_context = reset_diagnostic_context
