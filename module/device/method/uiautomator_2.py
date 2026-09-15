# Этот файл реализует взаимодействие с устройством на основе uiautomator2.
# Содержит основные операции управления мобильным устройством: скриншоты, клики, долгие нажатия, свайпы и извлечение иерархии (dump).
import re
import shlex
import time
import typing as t
from dataclasses import dataclass
from functools import wraps
from json.decoder import JSONDecodeError

import uiautomator2 as u2
from adbutils.errors import AdbError
from lxml import etree

from module.base.utils import *
from module.config.server import DICT_PACKAGE_TO_ACTIVITY
from module.device.connection import Connection
from module.device.method.utils import (ImageTruncated, PackageNotInstalled, RETRY_TRIES, handle_adb_error,
                                        handle_unknown_host_service, possible_reasons, retry_sleep)
from module.exception import EmulatorNotRunningError, RequestHumanTakeover
from module.logger import logger


def retry(func):
    @wraps(func)
    def retry_wrapper(self, *args, **kwargs):
        """
        Args:
            self (Uiautomator2):
        """
        init = None
        for _ in range(RETRY_TRIES):
            try:
                if callable(init):
                    time.sleep(retry_sleep(_))
                    init()
                return func(self, *args, **kwargs)
            # Необрабатываемая ошибка
            except RequestHumanTakeover:
                break
            # При остановке adb server
            except ConnectionResetError as e:
                logger.error(str(f'[Устройство — uiautomator2] Ошибка повторной попытки: {e}'))

                def init():
                    self.adb_reconnect()
            # При инициализации uiautomator2 server JSON может быть ещё не готов.
            # json.decoder.JSONDecodeError: Expecting value: line 1 column 2 (char 1)
            except JSONDecodeError as e:
                logger.error(str(f'[Устройство — uiautomator2] Ошибка повторной попытки: {e}'))

                def init():
                    self.install_uiautomator2()
            # AdbError
            except AdbError as e:
                if handle_adb_error(e):
                    def init():
                        self.adb_reconnect()
                elif handle_unknown_host_service(e):
                    def init():
                        self.adb_start_server()
                        self.adb_reconnect()
                else:
                    break
            # RuntimeError: USB device 127.0.0.1:5555 is offline
            except RuntimeError as e:
                if handle_adb_error(e):
                    def init():
                        self.adb_reconnect()
                else:
                    break
            # При `assert c.read string(4) == _OKAY`
            # В эмуляторе не включён ADB
            except AssertionError as e:
                logger.exception(str(f'[Устройство — uiautomator2] Ошибка повторной попытки: {e}'))
                possible_reasons(
                    '[Устройство — ADB] Если используется BlueStacks, LDPlayer или WSA, включите ADB в настройках эмулятора'
                )
                break
            # Пакет не установлен
            except PackageNotInstalled as e:
                logger.error(str(f'[Устройство — uiautomator2] Ошибка повторной попытки: {e}'))

                def init():
                    self.detect_package()
            # Изображение обрезано
            except ImageTruncated as e:
                from module.device.method.utils import handle_image_truncated
                handle_image_truncated(self, e)

                def init():
                    pass
            # Необрабатываемая ошибка — обязательно пробрасываем выше, чтобы запустить перезапуск эмулятора
            except EmulatorNotRunningError:
                raise
            # Неизвестное исключение
            except Exception as e:
                logger.exception(str(f'[Устройство — uiautomator2] Ошибка повторной попытки: {e}'))

                def init():
                    pass

        if func.__name__ in [
            '_app_start_u2_am', '_app_start_u2_monkey',
            'screenshot_uiautomator2',
        ]:
            logger.critical(f'[Устройство — uiautomator2] Не удалось выполнить {func.__name__}() после повторных попыток')
            raise EmulatorNotRunningError

        logger.critical(f'[Устройство — uiautomator2] Не удалось выполнить {func.__name__}() после повторных попыток')
        raise RequestHumanTakeover

    return retry_wrapper


@dataclass
class ProcessInfo:
    pid: int
    ppid: int
    thread_count: int | None
    cmdline: str
    name: str


@dataclass
class ShellBackgroundResponse:
    success: bool
    pid: int
    description: str


_PS_COMMAND_LINE_COLUMNS = {'ARGS', 'CMD', 'CMDLINE', 'COMMAND'}


def _normalise_process_text(value: object) -> str:
    return str(value).replace('\x00', ' ').strip()


def _parse_process_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _parse_ps_output(output: str) -> list[ProcessInfo]:
    """Разобрать варианты Android ``ps`` с сохранением command line."""
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    if not lines:
        return []

    header_index = None
    header = []
    for index, line in enumerate(lines):
        tokens = line.split()
        upper_tokens = [token.upper() for token in tokens]
        if 'PID' in upper_tokens and 'PPID' in upper_tokens:
            header_index = index
            header = upper_tokens
            break

    if header_index is None:
        return []

    pid_index = header.index('PID')
    ppid_index = header.index('PPID')
    thread_index = header.index('NLWP') if 'NLWP' in header else None
    args_index = next(
        (index for index, value in enumerate(header) if value in _PS_COMMAND_LINE_COLUMNS),
        None,
    )
    name_index = header.index('NAME') if 'NAME' in header else args_index
    processes = []
    for line in lines[header_index + 1:]:
        values = line.split(None, args_index) if args_index is not None else line.split()
        required_indexes = [pid_index, ppid_index]
        if any(index >= len(values) for index in required_indexes):
            continue
        pid = _parse_process_int(values[pid_index])
        ppid = _parse_process_int(values[ppid_index])
        if pid is None or ppid is None:
            continue
        thread_count = (
            _parse_process_int(values[thread_index])
            if thread_index is not None and thread_index < len(values)
            else None
        )
        name = _normalise_process_text(values[name_index]) if name_index is not None and name_index < len(values) else ''
        cmdline = _normalise_process_text(values[args_index]) if args_index is not None and args_index < len(values) else ''
        processes.append(ProcessInfo(pid, ppid, thread_count, cmdline, name))
    return processes


def _process_info_from_http(payload: dict) -> ProcessInfo:
    raw_cmdline = payload.get('cmdline')
    if isinstance(raw_cmdline, (list, tuple)):
        cmdline = _normalise_process_text(' '.join(map(str, raw_cmdline)))
    else:
        cmdline = _normalise_process_text(raw_cmdline) if raw_cmdline is not None else ''
    return ProcessInfo(
        pid=int(payload['pid']),
        ppid=int(payload['ppid']),
        thread_count=_parse_process_int(payload.get('threadCount')),
        cmdline=cmdline,
        name=_normalise_process_text(payload.get('name', '')),
    )


def _parse_batched_cmdlines(output: str) -> dict[str, str]:
    cmdlines = {}
    for line in output.splitlines():
        pid, separator, cmdline = line.partition('|')
        if separator and pid.strip().isdigit():
            cmdlines[pid.strip()] = _normalise_process_text(cmdline)
    return cmdlines


class Uiautomator2(Connection):
    @retry
    def screenshot_uiautomator2(self):
        image = self.u2.screenshot(format='pillow')
        if image is None:
            raise ImageTruncated('Пустые данные изображения от uiautomator2')
        if hasattr(image, 'convert'):
            image = image.convert('RGB')
        image = np.asarray(image).copy()
        if image.ndim != 3 or image.shape[2] != 3 or image.size == 0:
            raise ImageTruncated('Пустое изображение от uiautomator2')
        return image

    @retry
    def click_uiautomator2(self, x, y):
        self.u2.click(x, y)

    @retry
    def long_click_uiautomator2(self, x, y, duration=(1, 1.2)):
        self.u2.long_click(x, y, duration=duration)

    @retry
    def swipe_uiautomator2(self, p1, p2, duration=0.1):
        self.u2.swipe(*p1, *p2, duration=duration)

    @retry
    def _drag_along(self, path):
        """沿路径滑动。

        Args:
            path (list): (x, y, sleep)

        Examples:
            al.drag_along([
                (403, 421, 0.2),
                (821, 326, 0.1),
                (821, 326-10, 0.1),
                (821, 326+10, 0.1),
                (821, 326, 0),
            ])
            等价于:
            al.device.touch.down(403, 421)
            time.sleep(0.2)
            al.device.touch.move(821, 326)
            time.sleep(0.1)
            al.device.touch.move(821, 326-10)
            time.sleep(0.1)
            al.device.touch.move(821, 326+10)
            time.sleep(0.1)
            al.device.touch.up(821, 326)
        """
        length = len(path)
        for index, data in enumerate(path):
            x, y, second = data
            if index == 0:
                self.u2.touch.down(x, y)
                logger.info(point2str(x, y) + ' нажатие')
            elif index - length == -1:
                self.u2.touch.up(x, y)
                logger.info(point2str(x, y) + ' отпускание')
            else:
                self.u2.touch.move(x, y)
                logger.info(point2str(x, y) + ' перемещение')
            self.sleep(second)

    def drag_uiautomator2(self, p1, p2, segments=1, shake=(0, 15), point_random=(-10, -10, 10, 10),
                          shake_random=(-5, -5, 5, 5), swipe_duration=0.25, shake_duration=0.1):
        r"""Перетащить объект с небольшим встряхиванием; схема:
                     /\
        +-----------+  +  +
                        \/
        Простого свайпа или перетаскивания недостаточно, потому что оно содержит
        только две точки.
        Дополнительные точки делают движение более похожим на реальный свайп.

        Args:
            p1 (tuple): Начальная точка (x, y).
            p2 (tuple): Конечная точка (x, y).
            segments (int): Число отрезков пути.
            shake (tuple): Встряхивание после достижения конечной точки.
            point_random: Случайное смещение начальной и конечной точек.
            shake_random: Случайное смещение точек встряхивания.
            swipe_duration: Интервал между точками пути.
            shake_duration: Интервал между точками встряхивания.
        """
        p1 = np.array(p1) - random_rectangle_point(point_random)
        p2 = np.array(p2) - random_rectangle_point(point_random)
        path = [(x, y, swipe_duration) for x, y in random_line_segments(p1, p2, n=segments, random_range=point_random)]
        path += [
            (*p2 + shake + random_rectangle_point(shake_random), shake_duration),
            (*p2 - shake - random_rectangle_point(shake_random), shake_duration),
            (*p2, shake_duration)
        ]
        path = [(int(x), int(y), d) for x, y, d in path]
        self._drag_along(path)

    @retry
    def app_current_uiautomator2(self):
        """
        Returns:
            str: 包名。
        """
        result = self.u2.app_current()
        return result['package']

    @retry
    def _app_start_u2_monkey(self, package_name=None, allow_failure=False):
        """
        Args:
            package_name (str):
            allow_failure (bool):

        Returns:
            bool: 是否成功启动

        Raises:
            PackageNotInstalled:
        """
        if not package_name:
            package_name = self.package
        result = self.u2.shell([
            'monkey', '-p', package_name, '-c',
            'android.intent.category.LAUNCHER', '--pct-syskeys', '0', '1'
        ])
        if 'No activities found' in result.output:
            # ** No activities found to run, monkey aborted.
            if allow_failure:
                return False
            else:
                logger.error(result)
                raise PackageNotInstalled(package_name)
        elif 'inaccessible' in result.output:
            # /system/bin/sh: monkey: inaccessible or not found
            return False
        else:
            # Events injected: 1
            # ## Network stats: elapsed time=4ms (0ms mobile, 0ms wifi, 4ms not connected)
            return True

    @retry
    def _app_start_u2_am(self, package_name=None, activity_name=None, allow_failure=False):
        """
        Args:
            package_name (str):
            activity_name (str):
            allow_failure (bool):

        Returns:
            bool: 是否成功启动

        Raises:
            PackageNotInstalled:
        """
        if not package_name:
            package_name = self.package
        if not activity_name:
            try:
                info = self.u2.app_info(package_name)
            except u2.AppNotFoundError as e:
                if allow_failure:
                    return False
                logger.error(str(f'[Устройство — uiautomator2] Ошибка запуска приложения через uiautomator2: {e}'))
                raise PackageNotInstalled(package_name) from e
            except u2.DeviceError as e:
                if allow_failure:
                    return False
                # BaseError('package "111" not found')
                elif 'not found' in str(e):
                    logger.error(str(f'[Устройство — uiautomator2] Ошибка запуска приложения через uiautomator2: {e}'))
                    raise PackageNotInstalled(package_name)
                # Неизвестная ошибка
                else:
                    raise
            activity_name = info['mainActivity']

        cmd = ['am', 'start', '-a', 'android.intent.action.MAIN', '-c',
               'android.intent.category.LAUNCHER', '-n', f'{package_name}/{activity_name}']
        if self.is_local_network_device and self.is_waydroid:
            cmd += ['--windowingMode', '4']
        ret = self.u2.shell(cmd)
        # Недопустимая activity
        # Starting: Intent { act=android.intent.action.MAIN cat=[android.intent.category.LAUNCHER] cmp=... }
        # Error type 3
        # Error: Activity class {.../...} does not exist.
        if 'Error: Activity class' in ret.output:
            if allow_failure:
                return False
            else:
                logger.error(ret)
                return False
        # Уже запущено
        # Warning: Activity not started, intent has been delivered to currently running top-most instance.
        if 'Warning: Activity not started' in ret.output:
            logger.info('Activity приложения запущена')
            return True
        # Starting: Intent { act=android.intent.action.MAIN cat=[android.intent.category.LAUNCHER] cmp=com.YoStarEN.AzurLane/com.manjuu.azurlane.MainActivity }
        # java.lang.SecurityException: Permission Denial: starting Intent { act=android.intent.action.MAIN cat=[android.intent.category.LAUNCHER] flg=0x10000000 cmp=com.YoStarEN.AzurLane/com.manjuu.azurlane.MainActivity } from null (pid=5140, uid=2000) not exported from uid 10064
        #         at android.os.Parcel.readException(Parcel.java:1692)
        #         at android.os.Parcel.readException(Parcel.java:1645)
        #         at android.app.ActivityManagerProxy.startActivityAsUser(ActivityManagerNative.java:3152)
        #         at com.android.commands.am.Am.runStart(Am.java:643)
        #         at com.android.commands.am.Am.onRun(Am.java:394)
        #         at com.android.internal.os.BaseCommand.run(BaseCommand.java:51)
        #         at com.android.commands.am.Am.main(Am.java:124)
        #         at com.android.internal.os.RuntimeInit.nativeFinishInit(Native Method)
        #         at com.android.internal.os.RuntimeInit.main(RuntimeInit.java:290)
        if 'Permission Denial' in ret.output:
            if allow_failure:
                return False
            else:
                logger.error(ret)
                logger.error('Отказ в разрешении при запуске приложения; вероятно, указана недопустимая Activity')
                return False
        # Успешно
        # Starting: Intent...
        return True

    # Не используем декоратор @retry, поскольку _app_start_adb_am и _app_start_adb_monkey уже имеют @retry
    # @retry
    def app_start_uiautomator2(self, package_name=None, activity_name=None, allow_failure=False):
        """
        Args:
            package_name (str):
                为 None 时从配置中获取
            activity_name (str):
                为 None 时从 DICT_PACKAGE_TO_ACTIVITY 获取
                仍为 None 时通过 monkey 启动
                monkey 失败时，获取 activity 名称并通过 am 启动
            allow_failure (bool):
                为 True 时不抛出 PackageNotInstalled，只返回 False

        Returns:
            bool: 是否成功启动

        Raises:
            PackageNotInstalled:
        """
        if not package_name:
            package_name = self.package
        if not activity_name:
            activity_name = DICT_PACKAGE_TO_ACTIVITY.get(package_name)

        if activity_name:
            if self._app_start_u2_am(package_name, activity_name, allow_failure):
                return True
        if self._app_start_u2_monkey(package_name, allow_failure):
            return True
        if self._app_start_u2_am(package_name, activity_name, allow_failure):
            return True

        logger.error('[Устройство — uiautomator2] Все попытки завершились неудачно')
        return False

    @retry
    def app_stop_uiautomator2(self, package_name=None):
        if not package_name:
            package_name = self.package
        self.u2.app_stop(package_name)

    @retry
    def dump_hierarchy_uiautomator2(self) -> etree._Element:
        content = self.u2.dump_hierarchy(compressed=False)
        # print(content)
        hierarchy = etree.fromstring(content.encode('utf-8'))
        return hierarchy

    def uninstall_uiautomator2(self):
        logger.info('[Устройство — uiautomator2] Удаление uiautomator2')
        self.adb_shell(["rm", "/data/local/tmp/u2.jar"])

    @retry
    def resolution_uiautomator2(self, cal_rotation=True) -> t.Tuple[int, int]:
        """
        获取设备的有效分辨率，优先通过 ADB wm size 获取（支持 wm size override），
        回退到 uiautomator2 /info 接口。

        当用户通过 `adb shell wm size 720x1280` 设置了覆盖分辨率时，
        uiautomator2 /info 接口仍返回物理分辨率（如 1080x2400），
        而 ADB wm size 能正确报告 Override size。

        Returns:
            (width, height)
        """
        # Сначала используем ADB wm size, включая override-разрешение
        lines = []
        result = ''
        try:
            result = self.adb_shell(['wm', 'size'])
            lines = result.strip().split('\n')
        except Exception:
            logger.warning('[Устройство — uiautomator2] Не удалось выполнить `adb shell wm size`; используется `/info` uiautomator2')

        if lines:
            w, h = None, None
            import re
            size_pattern = re.compile(r'^(\d+)x(\d+)$')

            # Сначала читаем Override size — значение, заданное пользователем через wm size
            for line in lines:
                line = line.strip()
                if 'Override size:' in line:
                    try:
                        size_str = line.split(':', 1)[1].strip()
                        m = size_pattern.match(size_str)
                        if m:
                            w, h = int(m.group(1)), int(m.group(2))
                            break
                    except (ValueError, IndexError):
                        continue

            if w is None:
                # Override отсутствует: пробуем строку Physical size или строку с чистым разрешением
                for line in lines:
                    line = line.strip()
                    try:
                        if 'Physical size:' in line:
                            size_str = line.split(':', 1)[1].strip()
                            m = size_pattern.match(size_str)
                            if m:
                                w, h = int(m.group(1)), int(m.group(2))
                                break
                        elif size_pattern.match(line):
                            # Старые версии Android выводят только "1080x2400"
                            m = size_pattern.match(line)
                            if m:
                                w, h = int(m.group(1)), int(m.group(2))
                                break
                    except (ValueError, IndexError):
                        continue

            if w is not None and h is not None:
                if cal_rotation:
                    rotation = self.get_orientation()
                    if (w > h) != (rotation % 2 == 1):
                        w, h = h, w
                return w, h

            logger.warning(
                f'Не удалось определить разрешение из вывода `ADB wm size`; используется `/info` uiautomator2. Исходный вывод: {result!r}'
            )

        # Резервный путь через публичное свойство info uiautomator2 3.x.
        info = self.u2.info
        display = info.get('display', info)
        if 'width' in display and 'height' in display:
            w, h = display['width'], display['height']
        else:
            w, h = info['displayWidth'], info['displayHeight']
        if cal_rotation:
            rotation = self.get_orientation()
            if (w > h) != (rotation % 2 == 1):
                w, h = h, w
        return w, h

    def resolution_check_uiautomator2(self):
        """
        Alas 不主动检查分辨率，而是检查截图的宽高。
        但某些截图方法不提供设备分辨率，因此在此处进行检查。

        Returns:
            (width, height)

        Raises:
            RequestHumanTakeover: 分辨率不是 1280x720 时抛出
        """
        width, height = self.resolution_uiautomator2()
        logger.attr('Размер экрана', f'{width}x{height}')
        if width == 1280 and height == 720:
            return (width, height)
        if width == 720 and height == 1280:
            return (width, height)

        logger.critical(f'[Устройство — uiautomator2] Обнаружено разрешение {width}x{height}; требуется 1280x720')
        logger.critical('[Устройство — uiautomator2] Установите разрешение 1280x720')
        raise RequestHumanTakeover

    @retry
    def proc_list_uiautomator2(self) -> t.List[ProcessInfo]:
        """
        Получить сведения о текущих процессах.
        """
        if self.is_over_http:
            resp = self.u2.http.get("/proc/list", timeout=10)
            resp.raise_for_status()
            return [_process_info_from_http(proc) for proc in resp.json()]

        try:
            detailed_output = self.adb_shell([
                'ps', '-A', '-o', 'PID,PPID,NAME,CMDLINE'
            ])
        except (AdbError, RuntimeError):
            detailed_output = ''
        processes = _parse_ps_output(detailed_output)
        if not processes:
            processes = _parse_ps_output(self.adb_shell(['ps', '-A']))

        missing = [process for process in processes if not process.cmdline]
        if not missing:
            return processes

        pid_list = ' '.join(str(process.pid) for process in missing)
        script = (
            f'for p in {pid_list}; do '
            'printf "%s|" "$p"; '
            'tr "\\000" " " < "/proc/$p/cmdline" 2>/dev/null; '
            'printf "\\n"; '
            'done'
        )
        try:
            output = self.adb_shell(['sh', '-c', script], rstrip=False)
        except Exception:
            output = ''
        cmdlines = _parse_batched_cmdlines(output)

        for process in missing:
            cmdline = cmdlines.get(str(process.pid))
            if cmdline is None:
                try:
                    cmdline = self.adb_shell(
                        ['cat', f'/proc/{process.pid}/cmdline'],
                        rstrip=False,
                    )
                except Exception:
                    cmdline = ''
                cmdline = _normalise_process_text(cmdline)
            process.cmdline = cmdline or ''
        return processes

    @retry
    def u2_shell_background(self, cmdline, timeout=10) -> ShellBackgroundResponse:
        """
        Временно запустить argv программы в фоне.

        Shell/background/redirection принадлежат runner'у. В ``cmdline``
        передаются только аргументы программы.
        """
        if not isinstance(cmdline, (list, tuple)):
            raise TypeError('u2_shell_background принимает только argv списка или кортежа')
        command_args = [str(argument) for argument in cmdline]
        if not command_args:
            raise ValueError('u2_shell_background не принимает пустой argv')
        command = shlex.join(command_args)

        if self.is_over_http:
            data = dict(command=command, timeout=str(timeout))
            ret = self.u2.http.post("/shell/background", data=data, timeout=timeout + 10)
            ret.raise_for_status()
            resp = ret.json()
            try:
                pid = int(resp.get('pid', 0))
            except (TypeError, ValueError, AttributeError):
                pid = 0
            return ShellBackgroundResponse(
                success=bool(resp.get('success', False)) and pid > 0,
                pid=pid,
                description=str(resp.get('description', '')),
            )

        output = self.adb_shell(
            ['sh', '-c', f'{command} >/dev/null 2>&1 & echo $!'],
            timeout=timeout,
        ).strip()
        pid_match = re.search(r'(?m)^(\d+)\s*$', output)
        pid = int(pid_match.group(1)) if pid_match else 0
        return ShellBackgroundResponse(pid > 0, pid, output)

    def u2_set_fastinput_ime(self, enable: bool):
        self.u2.set_fastinput_ime(enable)

    def u2_current_ime(self):
        current = self.u2.current_ime()
        if isinstance(current, tuple):
            return current
        shown = 'mInputShown=true' in self.adb_shell(['dumpsys', 'input_method'])
        return current, shown

    def u2_send_keys(self, text: str, clear: bool=False):
        self.u2.send_keys(text=text, clear=clear)

    # См.: https://uiautomator2.readthedocs.io/en/latest/api.html#uiautomator2.Session.send_action
    def u2_send_action(self, code):
        self.u2.send_action(code=code)

    def u2_clear_text(self):
        self.u2.clear_text()

    @property
    def clipboard(self):
        return self.u2.clipboard
    
    def set_clipboard(self, text, label=None):
        return self.u2.set_clipboard(text=text, label=label)
