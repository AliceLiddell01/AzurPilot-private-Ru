"""Управление эмуляторами на Windows. Сканирует реестр Windows и файловую систему
для обнаружения путей установки и экземпляров Nox, BlueStacks, LDPlayer, MuMu, MEmu и др."""

import codecs
import os
import re
import typing as t
import winreg
from dataclasses import dataclass

# module/device/platform/emulator_base.py
# module/device/platform/emulator_windows.py
# Используется в Alas Easy Install, не должен импортировать модули Alas.
from module.device.platform.emulator_base import (
    EmulatorBase,
    EmulatorInstanceBase,
    EmulatorManagerBase,
    get_serial_pair,
    remove_duplicated_path,
)
from module.device.platform.utils import cached_property, iter_folder


@dataclass
class RegValue:
    """Структура данных значения реестра."""
    name: str
    value: str
    typ: int


def list_reg(reg) -> t.List[RegValue]:
    """
    Перечисляет все значения в разделе реестра.

    Args:
        reg: Открытый дескриптор раздела реестра.

    Returns:
        list[RegValue]: Список значений реестра.
    """
    rows = []
    index = 0
    try:
        while 1:
            value = RegValue(*winreg.EnumValue(reg, index))
            index += 1
            rows.append(value)
    except OSError:
        pass
    return rows


def list_key(reg) -> t.List[RegValue]:
    """
    Перечисляет имена всех подразделов раздела реестра.

    Args:
        reg: Открытый дескриптор раздела реестра.

    Returns:
        list[RegValue]: Список имён подразделов.
    """
    rows = []
    index = 0
    try:
        while 1:
            value = winreg.EnumKey(reg, index)
            index += 1
            rows.append(value)
    except OSError:
        pass
    return rows


def abspath(path):
    return os.path.abspath(path).replace('\\', '/')


class EmulatorInstance(EmulatorInstanceBase):
    """Экземпляр эмулятора для платформы Windows."""

    @cached_property
    def emulator(self):
        """
        Returns:
            Emulator: Объект эмулятора Windows, соответствующий текущему экземпляру.
        """
        return Emulator(self.path)

    @cached_property
    def adb_serials(self) -> tuple[str, ...]:
        """Вернуть канонический serial и read-only aliases одного инстанса."""

        serials = [self.serial] if self.serial else []
        if self.type in (Emulator.MuMuPlayerX, Emulator.MuMuPlayer12):
            emulator = self.emulator
            if self.name:
                vbox_folder = emulator.abspath(f'../vms/{self.name}')
                for vbox_file in iter_folder(vbox_folder, ext='.nemu'):
                    serials.extend(emulator.vbox_file_to_serials(vbox_file))
        return tuple(dict.fromkeys(serial for serial in serials if serial))


class Emulator(EmulatorBase):
    """Распознавание типов и перечисление экземпляров эмуляторов на платформе Windows."""

    @classmethod
    def path_to_type(cls, path: str) -> str:
        """
        Определяет тип эмулятора по пути к .exe-файлу (без учёта регистра).

        Args:
            path: Путь к .exe-файлу.

        Returns:
            str: Тип эмулятора, например Emulator.NoxPlayer; пустая строка, если не является эмулятором.
        """
        folder, exe = os.path.split(path)
        folder, dir1 = os.path.split(folder)
        folder, dir2 = os.path.split(folder)
        exe = exe.lower()
        dir1 = dir1.lower()
        dir2 = dir2.lower()
        if exe == 'nox.exe':
            if dir2 == 'nox':
                return cls.NoxPlayer
            elif dir2 == 'nox64':
                return cls.NoxPlayer64
            else:
                return cls.NoxPlayer
        if exe in ['bluestacks.exe', 'bluestacksgp.exe']:
            if dir1 in ['bluestacks', 'bluestacks_cn', 'bluestackscn']:
                return cls.BlueStacks4
            elif dir1 in ['bluestacks_nxt', 'bluestacks_nxt_cn']:
                return cls.BlueStacks5
            else:
                return cls.BlueStacks4
        if exe == 'hd-player.exe':
            if dir1 in ['bluestacks', 'bluestacks_cn']:
                return cls.BlueStacks4
            elif dir1 in ['bluestacks_nxt', 'bluestacks_nxt_cn']:
                return cls.BlueStacks5
            else:
                return cls.BlueStacks5
        if exe == 'dnplayer.exe':
            if dir1 == 'ldplayer':
                return cls.LDPlayer3
            elif dir1 == 'ldplayer4':
                return cls.LDPlayer4
            elif dir1 == 'ldplayer9':
                return cls.LDPlayer9
            elif dir1 == 'ldplayer14':
                return cls.LDPlayer14
            else:
                return cls.LDPlayer3
        if exe == 'nemuplayer.exe':
            if dir2 == 'nemu':
                return cls.MuMuPlayer
            elif dir2 == 'nemu9':
                return cls.MuMuPlayerX
            else:
                return cls.MuMuPlayer
        if exe in ['mumuplayer.exe', 'mumunxmain.exe']:
            return cls.MuMuPlayer12
        if exe == 'memu.exe':
            return cls.MEmuPlayer

        return ''

    @staticmethod
    def multi_to_single(exe: str):
        """
        Преобразует путь диспетчера мультиэкземпляров в путь исполняемого файла одиночного экземпляра.

        Args:
            exe (str): Путь к исполняемому файлу эмулятора.

        Yields:
            str: Путь к исполняемому файлу эмулятора.
        """
        if 'HD-MultiInstanceManager.exe' in exe:
            yield exe.replace('HD-MultiInstanceManager.exe', 'HD-Player.exe')
            yield exe.replace('HD-MultiInstanceManager.exe', 'Bluestacks.exe')
        elif 'MultiPlayerManager.exe' in exe:
            yield exe.replace('MultiPlayerManager.exe', 'Nox.exe')
        elif 'dnmultiplayer.exe' in exe:
            yield exe.replace('dnmultiplayer.exe', 'dnplayer.exe')
        elif 'NemuMultiPlayer.exe' in exe:
            yield exe.replace('NemuMultiPlayer.exe', 'NemuPlayer.exe')
        elif 'MuMuMultiPlayer.exe' in exe:
            yield exe.replace('MuMuMultiPlayer.exe', 'MuMuPlayer.exe')
        elif 'MuMuManager.exe' in exe:
            yield exe.replace('MuMuManager.exe', 'MuMuPlayer.exe')
            yield exe.replace('MuMuManager.exe', 'MuMuNxMain.exe')
        elif 'MEmuConsole.exe' in exe:
            yield exe.replace('MEmuConsole.exe', 'MEmu.exe')
        else:
            yield exe

    @staticmethod
    def single_to_console(exe: str):
        """
        Преобразует путь исполняемого файла одиночного экземпляра в путь инструмента командной строки.

        Args:
            exe (str): Путь к исполняемому файлу эмулятора.

        Returns:
            str: Путь к консольной утилите эмулятора.
        """
        if 'MuMuPlayer.exe' in exe:
            return exe.replace('MuMuPlayer.exe', 'MuMuManager.exe')
        # MuMuPlayer12 5.0
        elif 'MuMuNxMain.exe' in exe:
            return exe.replace('MuMuNxMain.exe', 'MuMuManager.exe')
        elif 'LDPlayer.exe' in exe:
            return exe.replace('LDPlayer.exe', 'ldconsole.exe')
        elif 'dnplayer.exe' in exe:
            return exe.replace('dnplayer.exe', 'ldconsole.exe')
        elif 'Bluestacks.exe' in exe:
            return exe.replace('Bluestacks.exe', 'bsconsole.exe')
        elif 'MEmu.exe' in exe:
            return exe.replace('MEmu.exe', 'memuc.exe')
        else:
            return exe

    @staticmethod
    def vbox_file_to_serial(file: str) -> str:
        """
        Разбирает серийный номер ADB из конфигурационного файла vbox.

        Args:
            file: Путь к файлу конфигурации vbox.

        Returns:
            str: Серийный номер, например `127.0.0.1:5555`; пустая строка, если не найден.
        """
        serials = Emulator.vbox_file_to_serials(file)
        return serials[0] if serials else ''

    @staticmethod
    def vbox_file_to_serials(file: str) -> tuple[str, ...]:
        """Прочитать все ADB aliases из одного vbox/nemu forwarding-файла."""

        regex = re.compile('<*?hostport="(.*?)".*?guestport="5555"/>')
        serials = []
        try:
            with open(file, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    res = regex.search(line)
                    if not res:
                        continue
                    serial = f'127.0.0.1:{res.group(1)}'
                    if serial not in serials:
                        serials.append(serial)
                    _, emu_serial = get_serial_pair(serial)
                    if emu_serial and emu_serial not in serials:
                        serials.append(emu_serial)
        except FileNotFoundError:
            pass
        return tuple(serials)

    def iter_instances(self):
        """
        Перебирает все обнаруженные экземпляры текущего эмулятора.

        Yields:
            EmulatorInstance: Экземпляр эмулятора.
        """
        if self == Emulator.NoxPlayerFamily:
            # ./BignoxVMS/{name}/{name}.vbox
            for folder in self.list_folder('./BignoxVMS', is_dir=True):
                for file in iter_folder(folder, ext='.vbox'):
                    serial = Emulator.vbox_file_to_serial(file)
                    if serial:
                        yield EmulatorInstance(
                            serial=serial,
                            name=os.path.basename(folder),
                            path=self.path,
                        )
        elif self == Emulator.BlueStacks5:
            # Получаем UserDefinedDir — расположение данных BlueStacks
            folder = None
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\BlueStacks_nxt") as reg:
                    folder = winreg.QueryValueEx(reg, 'UserDefinedDir')[0]
            except FileNotFoundError:
                pass
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\BlueStacks_nxt_cn") as reg:
                    folder = winreg.QueryValueEx(reg, 'UserDefinedDir')[0]
            except FileNotFoundError:
                pass
            if not folder:
                return
            # Читаем {UserDefinedDir}/bluestacks.conf
            try:
                with open(self.abspath('./bluestacks.conf', folder), encoding='utf-8') as f:
                    content = f.read()
            except FileNotFoundError:
                return
            # bst.instance.Nougat64.adb_port="5555"
            emulators = re.findall(r'bst.instance.(\w+).status.adb_port="(\d+)"', content)
            for emulator in emulators:
                yield EmulatorInstance(
                    serial=f'127.0.0.1:{emulator[1]}',
                    name=emulator[0],
                    path=self.path,
                )
        elif self == Emulator.BlueStacks4:
            # ../Engine/Android
            regex = re.compile(r'^Android')
            for folder in self.list_folder('./Engine/ProgramData/Engine', is_dir=True):
                folder = os.path.basename(folder)
                res = regex.match(folder)
                if not res:
                    continue
                # Серийный номер BlueStacks 4 не статичен: он увеличивается при каждом запуске эмулятора
                # Предполагаем единый адрес 127.0.0.1:5555
                yield EmulatorInstance(
                    serial=f'127.0.0.1:5555',
                    name=folder,
                    path=self.path
                )
        elif self == Emulator.LDPlayerFamily:
            # ./vms/leidian0
            regex = re.compile(r'^leidian(\d+)$')
            for folder in self.list_folder('./vms', is_dir=True):
                folder = os.path.basename(folder)
                res = regex.match(folder)
                if not res:
                    continue
                # В файлах .vbox эмулятора LDPlayer нет конфигурации проброса портов
                # Порты увеличиваются автоматически: 5555, 5557, 5559 и т. д.
                port = int(res.group(1)) * 2 + 5555
                yield EmulatorInstance(
                    serial=f'127.0.0.1:{port}',
                    name=folder,
                    path=self.path
                )
        elif self == Emulator.MuMuPlayer:
            # В MuMu 6 нет мультиинстанса, фиксирован порт 7555
            yield EmulatorInstance(
                serial='127.0.0.1:7555',
                name='',
                path=self.path,
            )
        elif self == Emulator.MuMuPlayerX:
            # vms/nemu-12.0-x64-default
            for folder in self.list_folder('../vms', is_dir=True):
                for file in iter_folder(folder, ext='.nemu'):
                    serial = Emulator.vbox_file_to_serial(file)
                    if serial:
                        yield EmulatorInstance(
                            serial=serial,
                            name=os.path.basename(folder),
                            path=self.path,
                        )
        elif self == Emulator.MuMuPlayer12:
            # vms/MuMuPlayer-12.0-0
            for folder in self.list_folder('../vms', is_dir=True):
                for file in iter_folder(folder, ext='.nemu'):
                    serial = Emulator.vbox_file_to_serial(file)
                    name = os.path.basename(folder)
                    if serial:
                        yield EmulatorInstance(
                            serial=serial,
                            name=name,
                            path=self.path,
                        )
                    # Адаптация для MuMu12 v4.0.4: у инстанса по умолчанию нет записей проброса портов в vbox
                    else:
                        instance = EmulatorInstance(
                            serial=serial,
                            name=name,
                            path=self.path,
                        )
                        if instance.MuMuPlayer12_id is not None:
                            instance.serial = f'127.0.0.1:{16384 + 32 * instance.MuMuPlayer12_id}'
                            yield instance
        elif self == Emulator.MEmuPlayer:
            # ./MemuHyperv VMs/{name}/{name}.memu
            for folder in self.list_folder('./MemuHyperv VMs', is_dir=True):
                for file in iter_folder(folder, ext='.memu'):
                    serial = Emulator.vbox_file_to_serial(file)
                    if serial:
                        yield EmulatorInstance(
                            serial=serial,
                            name=os.path.basename(folder),
                            path=self.path,
                        )

    def iter_adb_binaries(self) -> t.Iterable[str]:
        """
        Перебирает пути к исполняемым файлам adb, найденным в текущем эмуляторе.

        Yields:
            str: Абсолютный путь к исполняемому файлу adb.
        """
        if self == Emulator.NoxPlayerFamily:
            exe = self.abspath('./nox_adb.exe')
            if os.path.exists(exe):
                yield exe
        if self == Emulator.MuMuPlayerFamily:
            # Из MuMu9\emulator\nemu9\EmulatorShell
            # в MuMu9\emulator\nemu9\vmonitor\bin\adb_server.exe
            exe = self.abspath('../vmonitor/bin/adb_server.exe')
            if os.path.exists(exe):
                yield exe

        # Во всех эмуляторах есть adb.exe
        exe = self.abspath('./adb.exe')
        if os.path.exists(exe):
            yield exe


class EmulatorManager(EmulatorManagerBase):
    """Менеджер эмуляторов на платформе Windows, обнаруживающий установленные эмуляторы через реестр и процессы."""

    @staticmethod
    def iter_user_assist():
        """
        Получает список недавно запущенных программ из раздела реестра UserAssist.
        Ссылка: https://github.com/forensicmatt/MonitorUserAssist

        Yields:
            str: Путь к исполняемому файлу эмулятора, может содержать дубликаты.
        """
        path = r'Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist'
        # {XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}\xxx.exe
        regex_hash = re.compile(r'{.*}')
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as reg:
                folders = list_key(reg)
        except FileNotFoundError:
            return

        for folder in folders:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f'{path}\\{folder}\\Count') as reg:
                    for key in list_reg(reg):
                        key = codecs.decode(key.name, 'rot-13')
                        # Пропускаем записи с хэшем
                        if regex_hash.search(key):
                            continue
                        for file in Emulator.multi_to_single(key):
                            yield file
            except FileNotFoundError:
                # FileNotFoundError: [WinError 2] Не удается найти указанный файл.
                # Возможно, случайный каталог без подкаталога "Count"
                continue

    @staticmethod
    def iter_mui_cache():
        """
        Перебирает исполняемые файлы эмуляторов, ранее запускавшиеся, из раздела реестра MuiCache.
        Ссылка: http://what-when-how.com/windows-forensic-analysis/registry-analysis-windows-forensic-analysis-part-8/

        Yields:
            str: Путь к исполняемому файлу эмулятора, может содержать дубликаты.
        """
        path = r'Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\MuiCache'
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as reg:
                rows = list_reg(reg)
        except FileNotFoundError:
            return

        regex = re.compile(r'(^.*\.exe)\.')
        for row in rows:
            res = regex.search(row.name)
            if not res:
                continue
            for file in Emulator.multi_to_single(res.group(1)):
                yield file

    @staticmethod
    def get_install_dir_from_reg(path, key):
        """
        Получает каталог установки из реестра.

        Args:
            path (str): Путь в реестре, например f'SOFTWARE\\leidian\\ldplayer'.
            key (str): Имя параметра реестра, например 'InstallDir'.

        Returns:
            str: Каталог установки или None, если не найден.
        """
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as reg:
                root = winreg.QueryValueEx(reg, key)[0]
                return root
        except FileNotFoundError:
            pass
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as reg:
                root = winreg.QueryValueEx(reg, key)[0]
                return root
        except FileNotFoundError:
            pass

        return None

    @staticmethod
    def iter_uninstall_registry():
        """
        Перебирает пути к программам деинсталляции эмуляторов из реестра.

        Yields:
            str: Путь к исполняемому файлу программы деинсталляции.
        """
        known_uninstall_registry_path = [
            r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
            r'Software\Microsoft\Windows\CurrentVersion\Uninstall'
        ]
        known_emulator_registry_name = [
            'Nox',
            'Nox64',
            'BlueStacks',
            'BlueStacks_nxt',
            'BlueStacks_cn',
            'BlueStacks_nxt_cn',
            'LDPlayer',
            'LDPlayer4',
            'LDPlayer9',
            'leidian',
            'leidian4',
            'leidian9',
            'Nemu',
            'Nemu9',
            'MuMuPlayer',
            'MuMuPlayer-12.0',
            'MuMu Player 12.0',
            'MEmu',
        ]
        for path in known_uninstall_registry_path:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as reg:
                    software_list = list_key(reg)
            except FileNotFoundError:
                continue
            for software in software_list:
                if software not in known_emulator_registry_name:
                    continue
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f'{path}\\{software}') as software_reg:
                        uninstall = winreg.QueryValueEx(software_reg, 'UninstallString')[0]
                except FileNotFoundError:
                    continue
                if not uninstall:
                    continue
                # Формат UninstallString вида:
                # C:\Program Files\BlueStacks_nxt\BlueStacksUninstaller.exe -tmp
                # "E:\ProgramFiles\Microvirt\MEmu\uninstall\uninstall.exe" -u
                # Извлекаем путь в кавычках ""
                res = re.search('"(.*?)"', uninstall)
                uninstall = res.group(1) if res else uninstall
                yield uninstall

    @staticmethod
    def iter_running_emulator():
        """
        Перебирает пути к исполняемым файлам запущенных эмуляторов.

        Yields:
            str: Путь к исполняемому файлу эмулятора, может содержать дубликаты.
        """
        try:
            import psutil
        except ModuleNotFoundError:
            return
        # Так как это разовый вызов, обращаемся напрямую к psutil._psplatform.Process,
        # чтобы избежать накладных расходов на вызов psutil.Process.is_running().
        # Этот метод занимает всего около 0.017 с.
        for pid in psutil.pids():
            proc = psutil._psplatform.Process(pid)
            try:
                exe = proc.cmdline()
                exe = exe[0].replace(r'\\', '/').replace('\\', '/')
            except (psutil.AccessDenied, psutil.NoSuchProcess, IndexError, OSError):
                # psutil.AccessDenied
                # NoSuchProcess: процесс больше не существует (pid=xxx)
                # OSError: [WinError 87] Неверный параметр.: '(originated from ReadProcessMemory)'
                continue

            if Emulator.is_emulator(exe):
                yield exe

    @cached_property
    def all_emulators(self) -> t.List[Emulator]:
        """
        Возвращает все эмуляторы, установленные на текущем компьютере.

        Returns:
            list[Emulator]: Список эмуляторов.
        """
        exe = set([])

        # MuiCache
        for file in EmulatorManager.iter_mui_cache():
            if Emulator.is_emulator(file) and os.path.exists(file):
                exe.add(file)

        # UserAssist
        for file in EmulatorManager.iter_user_assist():
            if Emulator.is_emulator(file) and os.path.exists(file):
                exe.add(file)

        # Путь установки эмулятора LDPlayer
        for path in [
            r'SOFTWARE\leidian\ldplayer',
            r'SOFTWARE\leidian\ldplayer9',
            r'SOFTWARE\leidian\ldplayer14',
        ]:
            ld = self.get_install_dir_from_reg(path, 'InstallDir')
            if ld:
                ld = abspath(os.path.join(ld, './dnplayer.exe'))
                if Emulator.is_emulator(ld) and os.path.exists(ld):
                    exe.add(ld)

        # Путь установки эмулятора MuMu
        # Путь установки MuMu12 может находиться в реестре деинсталляции,
        # извлекаем каталог установки из InstallLocation или DisplayIcon
        _uninstall_reg_paths = [
            r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
            r'Software\Microsoft\Windows\CurrentVersion\Uninstall'
        ]
        for uninstall_reg_name in ['MuMuPlayer-12.0', 'MuMu Player 12.0']:
            for reg_path in _uninstall_reg_paths:
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f'{reg_path}\\{uninstall_reg_name}') as reg:
                        # Пробуем получить каталог установки из InstallLocation
                        try:
                            install_loc = winreg.QueryValueEx(reg, 'InstallLocation')[0]
                            if install_loc:
                                mumu_dir = abspath(install_loc)
                                for folder in ['', 'shell', 'shell/EmulatorShell', 'shell/nx_main']:
                                    search_dir = abspath(os.path.join(mumu_dir, folder))
                                    for file in iter_folder(search_dir, ext='.exe'):
                                        if Emulator.is_emulator(file) and os.path.exists(file):
                                            exe.add(file)
                        except FileNotFoundError:
                            pass
                        # Пробуем получить путь к исполняемому файлу из DisplayIcon
                        try:
                            display_icon = winreg.QueryValueEx(reg, 'DisplayIcon')[0]
                            if display_icon:
                                icon_path = abspath(display_icon.replace('"', '').split(',')[0])
                                # Ищем на один уровень выше пути иконки
                                parent_dir = os.path.dirname(icon_path)
                                for file in iter_folder(parent_dir, ext='.exe'):
                                    if Emulator.is_emulator(file) and os.path.exists(file):
                                        exe.add(file)
                                # Также ищем в подкаталоге shell
                                shell_dir = abspath(os.path.join(parent_dir, 'shell'))
                                for file in iter_folder(shell_dir, ext='.exe'):
                                    if Emulator.is_emulator(file) and os.path.exists(file):
                                        exe.add(file)
                        except FileNotFoundError:
                            pass
                except FileNotFoundError:
                    continue

        # Реестр деинсталляции
        for uninstall in EmulatorManager.iter_uninstall_registry():
            # Поиск исполняемого файла эмулятора из каталога деинсталлятора
            for file in iter_folder(abspath(os.path.dirname(uninstall)), ext='.exe'):
                if Emulator.is_emulator(file) and os.path.exists(file):
                    exe.add(file)
            # Поиск из родительского каталога
            for file in iter_folder(abspath(os.path.join(os.path.dirname(uninstall), '../')), ext='.exe'):
                if Emulator.is_emulator(file) and os.path.exists(file):
                    exe.add(file)
            # Специальный каталог MuMu
            for folder in ['EmulatorShell', 'nx_main']:
                for file in iter_folder(abspath(os.path.join(os.path.dirname(uninstall), folder)), ext='.exe'):
                    if Emulator.is_emulator(file) and os.path.exists(file):
                        exe.add(file)

        # Запущенные эмуляторы
        for file in EmulatorManager.iter_running_emulator():
            if os.path.exists(file):
                exe.add(file)

        # Удаление дубликатов
        exe = [Emulator(path).path for path in exe if Emulator.is_emulator(path)]
        exe = [Emulator(path) for path in remove_duplicated_path(exe)]
        return exe

    @cached_property
    def all_emulator_instances(self) -> t.List[EmulatorInstance]:
        """
        Возвращает все экземпляры эмуляторов, установленные на текущем компьютере.

        Returns:
            list[EmulatorInstance]: Список экземпляров эмуляторов.
        """
        instances = []
        for emulator in self.all_emulators:
            instances += list(emulator.iter_instances())

        instances: t.List[EmulatorInstance] = sorted(instances, key=lambda x: str(x))
        return instances


if __name__ == '__main__':
    self = EmulatorManager()
    for emu in self.all_emulator_instances:
        print(emu)
