"""Система журналирования AzurPilot.

Модуль построен на Rich и поддерживает цветной вывод в консоль, ротацию
файловых журналов и потоковую отрисовку в WebUI. Глобальный экземпляр
``logger`` с именем ``alas`` используется всем приложением.

Основные компоненты:
    - ``RichFileHandler`` — обработчик файлового журнала на базе Rich.
    - ``RichRenderableHandler`` — передаёт отрисованные объекты callback-функции WebUI.
    - ``RichTimedRotatingHandler`` — ротация файлов по времени с учётом процессов.
    - ``HTMLConsole`` — Rich Console для HTML/WebUI.
    - ``Highlighter`` — подсветка путей, времени и технических значений.

Вспомогательные функции ``hr()``, ``attr()``, ``attr_align()``,
``error_context()`` и ``exception_context()`` добавляются к глобальному logger
как единая точка журналирования проекта.
"""

import datetime
import io
import json
import logging
import multiprocessing
import os
import re
import shutil
import sys
import tarfile
import threading
import time
import zipfile
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Callable, List

from rich.console import Console, ConsoleOptions, ConsoleRenderable, NewLine
from rich.highlighter import NullHighlighter, RegexHighlighter
from rich.logging import RichHandler
from rich.pretty import Node
from rich.rule import Rule
from rich.style import Style
from rich.theme import Theme
from rich.traceback import Traceback

from module.logging_core import DiagnosticContextHandler, RepeatedEventSuppressor

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

_SENSITIVE_NAME_RE = re.compile(
    r"(?i)(?:authorization|credential|access[_-]?token|api[_-]?key|token|password|passwd|secret|cookie|session|private[_-]?key)"
)
_URL_USERINFO_RE = re.compile(
    r"(?P<scheme>\b[a-z][a-z0-9+.-]*://)[^/\s@]+@", re.IGNORECASE
)
_SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:access[_-]?token|api[_-]?key|token|password|passwd|secret)=)[^&#\s]+"
)
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(authorization|access[_-]?token|api[_-]?key|token|password|passwd|secret)"
    r"\s*([:=])\s*(?:bearer\s+)?[^\s,;]+"
)
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_UNSAFE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_BIDI_CONTROL_RE = re.compile(r"[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def _build_traceback_path_aliases():
    """Безопасно вычислить локальные path-aliases, не рискуя импортом logger."""
    aliases = []
    for resolver, alias in (
        (lambda: Path(__file__).resolve().parent.parent, "<PROJECT_ROOT>"),
        (lambda: Path.home().resolve(), "<USER_HOME>"),
    ):
        try:
            local_path = str(resolver())
        except (OSError, RuntimeError):
            continue
        if local_path:
            aliases.append((re.compile(re.escape(local_path), re.IGNORECASE), alias))
    return tuple(aliases)


_TRACEBACK_PATH_ALIASES = _build_traceback_path_aliases()


def sanitize_traceback_text(value) -> str:
    """Скрыть типовые секреты и управляющие последовательности в traceback."""
    text = str(value or "")
    text = _ANSI_ESCAPE_RE.sub("", text)
    text = _UNSAFE_CONTROL_RE.sub("", text)
    text = _BIDI_CONTROL_RE.sub("", text)
    text = _URL_USERINFO_RE.sub(r"\g<scheme>***@", text)
    text = _SENSITIVE_QUERY_RE.sub(r"\1***", text)
    text = _SENSITIVE_ASSIGNMENT_RE.sub(r"\1\2***", text)
    for path_pattern, alias in _TRACEBACK_PATH_ALIASES:
        text = path_pattern.sub(alias, text)
    return text


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


class RichFileHandler(RichHandler):
    # Отдельный тип нужен, чтобы отличать файловый Rich-обработчик от остальных.
    pass


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


class RichTimedRotatingHandler(TimedRotatingFileHandler):
    ZIPMAP = {
        "gzip": "gz",
        "gz" : "gz",
        "bz2" : "bz2",
        "xz": "xz",
        "zip": "zip",
    }
    def __init__(self, pname:str, *args, **kwargs) -> None:
        count, bak_method, zip_method = self._read_file_logger_config(pname)
        TimedRotatingFileHandler.__init__(self, backupCount=count,* args, **kwargs)
        self.console = Console(file=io.StringIO(), no_color=True, highlight=False, width=119)
        self.richd = RichHandler(
            console=self.console,
            show_path=False,
            show_time=False,
            show_level=False,
            rich_tracebacks=True,
            tracebacks_show_locals=False,
            tracebacks_extra_lines=3,
            highlighter=NullHighlighter(),
        )
        # Используем единый формат для файловых журналов.
        self.richd.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s.%(msecs)03d | %(levelname)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        # Совместимость с интерфейсом alas.save_error_log().
        self.log_file = None
        # Поля используются методом expire().
        self.pname = pname
        self.bak = bak_method.lower()
        self.compression = zip_method.lower()

        # Переопределяем начальный rolloverAt и поток Rich Console.
        self.rolloverAt = time.time()
        self.doRollover()

        # Закрываем лишний файловый поток базового обработчика.
        self.stream.close()
        self.stream = None
    
    def _read_file_logger_config(self, process_name):
        cfg_name = "alas" if process_name == "gui" else process_name
        config_file = Path("./config").joinpath(f"{cfg_name}.json")
        if config_file.exists():
            try:
                with config_file.open("r", encoding="utf-8") as f:
                    config = json.load(f)
                    log_config = config.get("General", {}).get("Log", {})
                    count = log_config.get("LogKeepCount", 7)
                    bak_method = log_config.get("LogBackUpMethod", "copy")
                    zip_method = log_config.get("ZipMethod", "bz2")
            except Exception as e:
                logging.exception(e)
                count = 7
                bak_method = "copy"
                zip_method = "bz2"
        else:
            count = 7
            bak_method = "zip" if process_name == "gui" else "copy"
            zip_method = "bz2"
        return count, bak_method, zip_method

    def getFilesToDelete(self) -> List[Path]:
        """Определить старые файлы журнала, подлежащие удалению при ротации."""
        dirName, baseName = os.path.split(self.baseFilename)
        fileNames = os.listdir(dirName)
        result = []
        suffix = "_" + baseName
        plen = len(suffix)
        for fileName in fileNames:
            if fileName[-plen:] == suffix:
                prefix = fileName[:-plen]
                if self.extMatch.match(prefix):
                    result.append(Path(dirName).joinpath(fileName).resolve())
        if len(result) < self.backupCount:
            result = []
        else:
            result.sort()
            result = result[: len(result) - self.backupCount]
        return result

    def doRollover(self) -> None:
        """Выполнить ротацию журнала и переключить файловый поток Rich."""
        if self.richd.console:
            self.richd.console.file.close()
            self.richd.console.file = None

        currentTime = int(time.time())
        dstNow = time.localtime(currentTime)[-1]
        t = self.rolloverAt
        if self.utc:
            timeTuple = time.gmtime(t)
        else:
            timeTuple = time.localtime(t)
            dstThen = timeTuple[-1]
            if dstNow != dstThen:
                if dstNow:
                    addend = 3600
                else:
                    addend = -3600
                timeTuple = time.localtime(t + addend)

        path = Path(self.baseFilename)
        # 2021-08-01 + _ + alas.txt -> "2021-08-01_alas.txt".
        newPath = path.with_name(
            time.strftime(self.suffix, timeTuple) + "_" + path.name
        )
        self.richd.console.file = open(newPath, "a", encoding="utf-8")

        if self.backupCount > 0:
            files = self.getFilesToDelete()
            if files:
                threading.Thread(target=self.expire, args=(files,), daemon=True).start()

        newRolloverAt = self.computeRollover(currentTime)
        while newRolloverAt <= currentTime:
            newRolloverAt = newRolloverAt + self.interval
        # При переходе через границу летнего времени для полуночной/недельной
        # ротации компенсируем изменение смещения.
        if (self.when == "MIDNIGHT" or self.when.startswith("W")) and not self.utc:
            dstAtRollover = time.localtime(newRolloverAt)[-1]
            if dstNow != dstAtRollover:
                if not dstNow:
                    addend = -3600
                else:
                    addend = 3600
                newRolloverAt += addend
        self.rolloverAt = newRolloverAt

        self.log_file = str(newPath.resolve())

    def expire(self, files: List[Path]) -> None:
        """Удалить или архивировать просроченные файлы журнала.

        Примеры:
            2021-08-01_alas.txt...2021-08-07_alas.txt -> bak/2021-08-01~2021-08-07_alas.tar.bz2
            2021-08-01_gui.txt -> bak/2021-08-01_gui.zip
            2021-08-01_gui.txt (copy) -> bak/2021-08-01_gui.txt
        """
        basePath = Path(self.baseFilename)
        bakPath = basePath.parent / "bak"
        bakPath.mkdir(parents=True, exist_ok=True)
        if self.bak == "delete":
            for file in files:
                file.unlink()
            return
        elif self.bak == "copy":
            for file in files:
                dst = bakPath.joinpath(file.name)
                if not dst.exists():
                    shutil.copy2(file, dst)
                file.unlink()
            return
        try:
            dates = [file.stem.split("_")[0] for file in files]
            name = (
                min(dates) + "~" + max(dates) + "_" + basePath.name
                if len(dates) > 1
                else files[0].name
            )
            ext = self.ZIPMAP[self.compression]
            if ext == "zip":
                zipFile = bakPath.joinpath(name).with_suffix(".zip")
                with zipfile.ZipFile(zipFile, "w", zipfile.ZIP_DEFLATED) as zipf:
                    for file in files:
                        zipf.write(file, arcname=file.name)
            else:
                zipFile = bakPath.joinpath(name).with_suffix(".tar." + ext)
                with tarfile.open(zipFile, "w:" + ext) as tar:
                    for file in files:
                        tar.add(file, arcname=file.name)
            # Исходные журналы удаляем только после успешного закрытия архива.
            # Если daemon-поток завершится во время записи, исходные файлы останутся.
            for file in files:
                file.unlink()
        except Exception as e:
            logger.exception(e)

    def print(self, *objects: ConsoleRenderable, **kwargs) -> None:
        Console.print(self.console, *objects, **kwargs)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self.shouldRollover(record):
                self.doRollover()
            RichHandler.emit(self.richd, record)
        except Exception:
            RichHandler.handleError(self.richd, record)


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
    "web.none": Style(color="magenta"),
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
file_formatter = logging.Formatter(
    fmt='%(asctime)s.%(msecs)03d | %(levelname)s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
console_formatter = logging.Formatter(
    fmt='%(asctime)s.%(msecs)03d │ %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
web_formatter = logging.Formatter(
    fmt='%(asctime)s.%(msecs)03d │ %(message)s', datefmt='%H:%M:%S')

diagnostic_hdlr = DiagnosticContextHandler(
    capacity=200,
    sanitizer=sanitize_traceback_text,
)
diagnostic_hdlr.setFormatter(file_formatter)
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

# Файловый обработчик журнала.
pyw_name = os.path.splitext(os.path.basename(sys.argv[0]))[0]


def _configure_diagnostic_logger(name):
    diagnostic_file = Path('./log/diagnostic').joinpath(
        f'{datetime.date.today()}_{name}.txt'
    )
    diagnostic_hdlr.configure_output(diagnostic_file, file_formatter)
    logger.diagnostic_log_file = str(diagnostic_file.resolve())


def _set_file_logger(name=pyw_name):
    if '_' in name:
        name = name.split('_', 1)[0]
    _configure_diagnostic_logger(name)
    log_file = f'./log/{datetime.date.today()}_{name}.txt'
    try:
        file = logging.FileHandler(log_file, encoding='utf-8')
    except FileNotFoundError:
        os.mkdir('./log')
        file = logging.FileHandler(log_file, encoding='utf-8')
    file.setLevel(logging.DEBUG if logger_debug else logging.INFO)
    file.setFormatter(file_formatter)

    logger.handlers = [h for h in logger.handlers if not isinstance(
        h, (logging.FileHandler, RichFileHandler))]
    logger.addHandler(file)
    diagnostic_hdlr.configure_failure_target(file)
    logger.log_file = log_file


def set_file_logger(name=pyw_name):
    if "_" in name:
        name = name.split("_", 1)[0]
    # В Windows возможны процессы ``SyncManager-N:N``, ``MainProcess``,
    # ``Process-N`` и ``gui``; в Linux отдельного SyncManager обычно нет.
    if os.name == "nt":
        # Эти служебные процессы Windows не должны создавать отдельные журналы.
        processes = ["SyncManager-", "MainProcess", "Process-"]
        pname = multiprocessing.current_process().name.replace(":", "_")
        # Каждый процесс должен настраивать файловый logger не более одного раза.
        if any(isinstance(hdlr, RichTimedRotatingHandler) for hdlr in logger.handlers):
            return
    else:
        processes = []
        pname = name
        for hdlr in logger.handlers:
            if isinstance(hdlr, RichTimedRotatingHandler):
                # Каждый процесс должен настраивать файловый logger не более одного раза.
                if hdlr.pname == name:
                    return
                else:
                    logger.handlers = [h for h in logger.handlers if not isinstance(
                        h, (logging.FileHandler, RichTimedRotatingHandler, RichFileHandler))]
    
    log_dir = Path("./log")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir.joinpath(f"{pname}.txt" if name == "gui" else f"{name}.txt")
    if any(p in log_file.name for p in processes):
        return

    _configure_diagnostic_logger(pname if name == "gui" else name)
    hdlr = RichTimedRotatingHandler(
        pname=name,
        filename=str(log_file),
        when="midnight",
        interval=1,
        encoding="utf-8",
    )
    hdlr.setLevel(logging.DEBUG if logger_debug else logging.INFO)

    logger.addHandler(hdlr)
    diagnostic_hdlr.configure_failure_target(hdlr)
    logger.log_file = hdlr.log_file
    try:
        if log_file.exists():
            log_file.unlink()
    except Exception:
        pass



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
        elif isinstance(hdlr, RichTimedRotatingHandler):
            hdlr.print(*objects, **kwargs)


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
logger.set_file_logger = set_file_logger
logger.set_func_logger = set_func_logger
logger.rule = rule
logger.print = print
logger.log_suppressed = log_suppressed
logger.finish_suppressed = finish_suppressed
logger.reset_suppression = reset_suppression
logger.get_diagnostic_context = get_diagnostic_context
logger.reset_diagnostic_context = reset_diagnostic_context
logger.log_file: str
logger.diagnostic_log_file: str

logger.set_file_logger()
logger.hr('Запуск', level=0)
