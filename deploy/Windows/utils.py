import os
import re
from dataclasses import dataclass
from typing import Callable, Generic, Iterable, TypeVar

T = TypeVar("T")

DEPLOY_CONFIG = './config/deploy.yaml'
DEPLOY_TEMPLATE = './deploy/Windows/template.yaml'


class cached_property(Generic[T]):
    """带类型支持的缓存属性描述符。"""

    def __init__(self, func: Callable[..., T]):
        self.func = func

    def __get__(self, obj, cls) -> T:
        if obj is None:
            return self

        value = obj.__dict__[self.func.__name__] = self.func(obj)
        return value


def iter_folder(folder, is_dir=False, ext=None):
    """遍历目录下的文件或子目录。"""
    for file in os.listdir(folder):
        sub = os.path.join(folder, file)
        if is_dir:
            if os.path.isdir(sub):
                yield sub.replace('\\\\', '/').replace('\\', '/')
        elif ext is not None:
            if not os.path.isdir(sub):
                _, extension = os.path.splitext(file)
                if extension == ext:
                    yield os.path.join(folder, file).replace('\\\\', '/').replace('\\', '/')
        else:
            yield os.path.join(folder, file).replace('\\\\', '/').replace('\\', '/')


def poor_yaml_read(file):
    """读取项目使用的简单标量 YAML；缺失文件返回空配置。"""
    if not os.path.exists(file):
        return {}

    data = {}
    regex = re.compile(r'^(.*?):(.*?)$')
    with open(file, 'r', encoding='utf-8-sig') as stream:
        for line in stream.readlines():
            line = line.strip('\n\r\t ').replace('\\', '/')
            if line.startswith('#'):
                continue
            result = re.match(regex, line)
            if result:
                key, value = result.group(1), result.group(2).strip("\n\r\t' ")
                if value:
                    lowered = value.lower()
                    if lowered == 'null':
                        value = None
                    elif lowered == 'false':
                        value = False
                    elif lowered == 'true':
                        value = True
                    elif value.isdigit():
                        value = int(value)
                    data[key] = value

    return data


def _format_yaml_scalar(value):
    if value is None:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    return str(value)


def poor_yaml_write(
    data,
    file,
    template_file=DEPLOY_TEMPLATE,
    *,
    preserve_existing=False,
    keys=None,
):
    """Write selected scalar keys while preserving an existing user file."""
    source = file if preserve_existing and os.path.exists(file) else template_file
    with open(source, 'r', encoding='utf-8-sig') as stream:
        text = stream.read()

    selected_keys = list(data) if keys is None else list(keys)
    for key in selected_keys:
        if key not in data:
            continue

        value = _format_yaml_scalar(data[key])
        pattern = re.compile(
            rf'^(?P<prefix>\s*{re.escape(str(key))}\s*:).*$',
            re.MULTILINE,
        )
        matches = list(pattern.finditer(text))
        if len(matches) > 1:
            raise ValueError(f'Duplicate deploy config key: {key}')
        if matches:
            text = pattern.sub(
                lambda match: f"{match.group('prefix')} {value}",
                text,
                count=1,
            )
            continue

        if preserve_existing:
            if text and not text.endswith('\n'):
                text += '\n'
            text += f'{key}: {value}\n'

    with open(file, 'w', encoding='utf-8', newline='') as stream:
        stream.write(text)


@dataclass
class DataProcessInfo:
    proc: object
    pid: int

    @cached_property
    def name(self):
        try:
            name = self.proc.name()
        except Exception:
            name = ''
        return name

    @cached_property
    def cmdline(self):
        try:
            cmdline = self.proc.cmdline()
        except Exception:
            cmdline = []
        cmdline = ' '.join(cmdline).replace('\\\\', '/').replace('\\', '/')
        return cmdline

    def __str__(self):
        return f'DataProcessInfo(name="{self.name}", pid={self.pid}, cmdline="{self.cmdline}")'

    __repr__ = __str__


def iter_process() -> Iterable[DataProcessInfo]:
    try:
        import psutil
    except ModuleNotFoundError:
        return

    if psutil.WINDOWS:
        for pid in psutil.pids():
            proc = psutil._psplatform.Process(pid)
            yield DataProcessInfo(proc=proc, pid=proc.pid)
    else:
        for proc in psutil.process_iter():
            yield DataProcessInfo(proc=proc, pid=proc.pid)
