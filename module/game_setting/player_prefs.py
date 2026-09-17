"""Безопасное обновление локальных настроек PlayerPrefs Azur Lane перед запуском игры."""

import hashlib
import os
import re
import secrets
import shlex
import subprocess
import time
import xml.etree.ElementTree as etree
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from module.exception import RequestHumanTakeover
from module.logger import logger


PACKAGE_PATTERN = re.compile(r'^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$')
PREFS_FILE_PATTERN = re.compile(r'^[A-Za-z0-9_.-]+\.v2\.playerprefs\.xml$')
STANDBY_MODE_KEY_PATTERN = re.compile(r'^STANDBY_MODE_KEY_[0-9]+$')
STORY_SPEED_KEY_PATTERN = re.compile(r'^story_speed_flag[0-9]+$')
METADATA_PATTERN = re.compile(r'^(?P<uid>[0-9]+):(?P<gid>[0-9]+):(?P<mode>[0-7]{3,4})$')
SELINUX_CONTEXT_PATTERN = re.compile(r'^[A-Za-z0-9_:,.-]+$')
LEGACY_TRANSACTION_SUFFIX_PATTERN = re.compile(r'^\.alas-\d{8}-\d{6}-[0-9a-f]{12}\.bak$')
LEGACY_TEMPORARY_TRANSACTION_SUFFIX_PATTERN = re.compile(
    r'^\.alas-\d{8}-\d{6}-[0-9a-f]{12}\.(?:tmp|rollback\.tmp)$'
)
TEMPORARY_TRANSACTION_SUFFIX_PATTERN = re.compile(r'^\.alas-tmp-[0-9a-f]{16}\.tmp$')
STORY_SPEED_VALUE = 9

# Поддерживаем только настройки, подтверждённые текущим Lua-кодом для пяти серверов. Не обобщать запись на основании setting_generated.py.
RECOMMENDED_INT_SETTINGS = {
    'fps_limit': 60,
    'world_flag_story_tips': 1,
    'world_flag_consume_item': 1,
    'world_flag_auto_save_area': 0,
    'story_autoplay_flag': 1,
    'display_ship_get_effect': 0,
    'QUICK_CHANGE_EQUIP': 0,
    'BATTLERESULT_DISPAY_PAINTING': 0,
    'world_sub_auto_call': 0,
}
RECOMMENDED_STRING_SETTINGS = {
    '_WorldBossProgressTipFlag_': '',
}


class PlayerPrefsError(Exception):
    """Базовое исключение транзакций PlayerPrefs."""


class PlayerPrefsUnsupported(PlayerPrefsError):
    """Текущее устройство или формат файла не поддерживают безопасную запись."""


class PlayerPrefsWriteError(PlayerPrefsError):
    """Ошибка записи или проверки после записи."""


@dataclass(frozen=True)
class PlayerPrefsChanges:
    """Сводка изменений за одно обновление XML."""

    static_changed: int
    story_speed_changed: int
    standby_changed: int
    story_speed_keys: tuple[str, ...]
    standby_keys: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return (
            self.static_changed > 0
            or self.story_speed_changed > 0
            or self.standby_changed > 0
        )


@dataclass(frozen=True)
class PlayerPrefsMetadata:
    """Владелец, права доступа и контекст SELinux приватного файла приложения Android."""

    uid: str
    gid: str
    mode: str
    context: str


@dataclass(frozen=True)
class AdbResult:
    """Результат выполнения команды ADB."""

    returncode: int
    stdout: str
    stderr: str


def _is_target_key(name: str | None) -> bool:
    """Определить, входит ли ключ в строго поддерживаемый белый список статических или динамических настроек."""
    return isinstance(name, str) and (
        name in RECOMMENDED_INT_SETTINGS
        or name in RECOMMENDED_STRING_SETTINGS
        or STORY_SPEED_KEY_PATTERN.fullmatch(name) is not None
        or STANDBY_MODE_KEY_PATTERN.fullmatch(name) is not None
    )


def _index_target_entries(root: etree.Element) -> dict[str, etree.Element]:
    """Индексировать элементы настроек для чтения или изменения с запретом одноименных целевых ключей."""
    if root.tag != 'map':
        raise PlayerPrefsUnsupported(f'Неподдерживаемый корневой узел PlayerPrefs: {root.tag!r}')

    entries = {}
    for element in root:
        name = element.get('name')
        if not _is_target_key(name):
            continue
        if name in entries:
            raise PlayerPrefsUnsupported(f'В PlayerPrefs присутствует повторяющийся целевой ключ: {name!r}')
        entries[name] = element
    return entries


def _set_int(root: etree.Element, entries: dict[str, etree.Element], name: str, value: int) -> bool:
    """Записать единичный ключ белого списка в формате int для Android SharedPreferences."""
    expected = str(value)
    element = entries.get(name)
    if element is None:
        element = etree.Element('int', {'name': name, 'value': expected})
        root.append(element)
        entries[name] = element
        return True

    if element.tag != 'int':
        raise PlayerPrefsUnsupported(f'XML-тип целевого ключа {name!r} не int: {element.tag!r}')
    if element.text and element.text.strip():
        raise PlayerPrefsUnsupported(f'Целевой ключ {name!r} содержит текстовое значение, которое нельзя безопасно обработать')
    if element.get('value') == expected:
        return False

    element.set('value', expected)
    return True


def _set_string(root: etree.Element, entries: dict[str, etree.Element], name: str, value: str) -> bool:
    """Записать единичный ключ белого списка в формате string для Android SharedPreferences."""
    element = entries.get(name)
    if element is None:
        element = etree.Element('string', {'name': name})
        element.text = value
        root.append(element)
        entries[name] = element
        return True

    if element.tag != 'string':
        raise PlayerPrefsUnsupported(f'XML-тип целевого ключа {name!r} не string: {element.tag!r}')
    if element.get('value') is not None or len(element):
        raise PlayerPrefsUnsupported(f'XML-содержимое целевого ключа {name!r} нельзя безопасно обработать')
    current = '' if element.text is None else element.text
    if current == value:
        return False

    element.text = value
    return True


def _serialize_xml(root: etree.Element) -> bytes:
    """Сгенерировать XML SharedPreferences в кодировке UTF-8, читаемый Android."""
    etree.indent(root, space='    ')
    return etree.tostring(root, encoding='utf-8', xml_declaration=True, short_empty_elements=True)


def update_player_prefs_xml(content: bytes) -> tuple[bytes, PlayerPrefsChanges]:
    """Обновить XML PlayerPrefs по строгому белому списку с сохранением всех остальных настроек.

    Args:
        content: Исходные байты XML PlayerPrefs.

    Returns:
        Обновленный XML и сводка изменений.

    Raises:
        PlayerPrefsUnsupported: Неизвестный формат XML или наличие целевых ключей, которые нельзя безопасно обработать.
    """
    try:
        root = etree.fromstring(content)
    except etree.ParseError as error:
        raise PlayerPrefsUnsupported(f'Не удалось разобрать XML PlayerPrefs: {error}') from None

    entries = _index_target_entries(root)
    static_changed = 0
    for name, value in RECOMMENDED_INT_SETTINGS.items():
        static_changed += _set_int(root, entries, name, value)

    for name, value in RECOMMENDED_STRING_SETTINGS.items():
        static_changed += _set_string(root, entries, name, value)

    # Скорость сюжета хранится в ключах по ID игрока; обновляем только существующие ключи, не угадываем и не создаём суффиксы аккаунтов.
    story_speed_keys = tuple(sorted(
        name for name in entries if STORY_SPEED_KEY_PATTERN.fullmatch(name)
    ))
    story_speed_changed = 0
    for name in story_speed_keys:
        story_speed_changed += _set_int(root, entries, name, STORY_SPEED_VALUE)

    standby_keys = tuple(sorted(name for name in entries if STANDBY_MODE_KEY_PATTERN.fullmatch(name)))
    standby_changed = 0
    for name in standby_keys:
        standby_changed += _set_int(root, entries, name, 0)

    changes = PlayerPrefsChanges(
        static_changed=static_changed,
        story_speed_changed=story_speed_changed,
        standby_changed=standby_changed,
        story_speed_keys=story_speed_keys,
        standby_keys=standby_keys,
    )
    return _serialize_xml(root), changes


def verify_player_prefs_xml(
        content: bytes,
        standby_keys: tuple[str, ...],
        story_speed_keys: tuple[str, ...] = (),
) -> None:
    """Проверить, что все целевые настройки были записаны с ожидаемыми значениями."""
    try:
        root = etree.fromstring(content)
    except etree.ParseError as error:
        raise PlayerPrefsWriteError(f'Не удалось разобрать прочитанный обратно XML PlayerPrefs: {error}') from None

    entries = _index_target_entries(root)
    for name, value in RECOMMENDED_INT_SETTINGS.items():
        element = entries.get(name)
        if element is None or element.tag != 'int' or element.get('value') != str(value):
            raise PlayerPrefsWriteError(f'Прочитанное обратно значение целевого ключа {name!r} неверно')

    for name, value in RECOMMENDED_STRING_SETTINGS.items():
        element = entries.get(name)
        actual = '' if element is None or element.text is None else element.text
        if element is None or element.tag != 'string' or actual != value:
            raise PlayerPrefsWriteError(f'Прочитанное обратно значение целевого ключа {name!r} неверно')

    for name in story_speed_keys:
        element = entries.get(name)
        if element is None or element.tag != 'int' or element.get('value') != str(STORY_SPEED_VALUE):
            raise PlayerPrefsWriteError('Прочитанное обратно значение настройки скорости автопроигрывания сюжета неверно')

    for name in standby_keys:
        element = entries.get(name)
        if element is None or element.tag != 'int' or element.get('value') != '0':
            raise PlayerPrefsWriteError('Прочитанное обратно значение настройки режима ожидания неверно')


@contextmanager
def _device_lock(serial: str, package: str, timeout: float = 10) -> None:
    """Межпроцессная блокировка на основе serial и имени пакета во избежание одновременной замены одного файла несколькими экземплярами."""
    key = hashlib.sha256(f'{serial}\0{package}'.encode('utf-8')).hexdigest()[:16]
    lock_file = Path('cache') / f'game-settings-{key}.lock'
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    with lock_file.open('a+b') as handle:
        handle.seek(0)
        if not handle.read(1):
            handle.seek(0)
            handle.write(b'0')
            handle.flush()

        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if time.monotonic() >= deadline:
                    raise PlayerPrefsUnsupported('Тайм-аут ожидания транзакции настроек игры в другом экземпляре') from None
                time.sleep(0.1)

        try:
            yield
        finally:
            if os.name == 'nt':
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class PlayerPrefsManager:
    """Атомарное обновление файла PlayerPrefs Azur Lane через root ADB."""

    def __init__(self, device, wait_for_stop: bool = False):
        self.device = device
        self.wait_for_stop = wait_for_stop
        self.package = str(device.package)
        self._root_enabled_by_transaction = False
        self._use_su = False

    def _run_adb(
            self,
            args: list[str],
            *,
            timeout: float = 15,
            check: bool = True,
            error_type: type[PlayerPrefsError] = PlayerPrefsUnsupported,
    ) -> AdbResult:
        """Выполнить команду хостового ADB с проверкой кода возврата."""
        command = [str(self.device.adb_binary), '-s', str(self.device.serial), *map(str, args)]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise error_type(f'Не удалось выполнить команду ADB: {args[0]}') from None

        result = AdbResult(
            returncode=completed.returncode,
            stdout=completed.stdout.decode('utf-8', errors='replace').strip(),
            stderr=completed.stderr.decode('utf-8', errors='replace').strip(),
        )
        if check and result.returncode != 0:
            raise error_type(f'Команда ADB завершилась с ошибкой: {args[0]}')
        return result

    def _run_adb_bytes(
            self,
            args: list[str],
            *,
            input_data: bytes | None = None,
            timeout: float = 15,
            error_type: type[PlayerPrefsError] = PlayerPrefsUnsupported,
    ) -> bytes:
        """Передать бинарные данные через ADB без записи PlayerPrefs в локальные файлы."""
        command = [str(self.device.adb_binary), '-s', str(self.device.serial), *map(str, args)]
        try:
            completed = subprocess.run(
                command,
                input=input_data,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise error_type(f'Не удалось передать бинарные данные через ADB: {args[0]}') from None
        if completed.returncode != 0:
            raise error_type(f'Не удалось передать бинарные данные через ADB: {args[0]}')
        return completed.stdout

    def _shell(
            self,
            args: list[str],
            *,
            timeout: float = 15,
            check: bool = True,
            error_type: type[PlayerPrefsError] = PlayerPrefsUnsupported,
    ) -> AdbResult:
        if self._use_su:
            args = ['su', '-c', shlex.join(map(str, args))]
        return self._run_adb(
            ['shell', *args],
            timeout=timeout,
            check=check,
            error_type=error_type,
        )

    def _ensure_root(self) -> bool:
        """Убедиться, что adbd работает под root, или выполнить откат к доступной команде ``su -c``."""
        current = self._shell(['id'], check=False)
        if 'uid=0(root)' in current.stdout:
            return True

        self._run_adb(['root'], check=False)
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            current = self._shell(['id'], check=False)
            if 'uid=0(root)' in current.stdout:
                self._root_enabled_by_transaction = True
                return True
            time.sleep(0.25)

        su = self._run_adb(['shell', 'su', '-c', 'id'], check=False)
        if 'uid=0(root)' in su.stdout:
            self._use_su = True
            return True
        return False

    def _restore_root_state(self) -> None:
        """Восстановить non-root состояние adbd, только если привилегии повышались в этой транзакции, сохраняя исходное состояние пользователя."""
        if not self._root_enabled_by_transaction:
            return
        try:
            self._run_adb(['unroot'], check=False)
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                current = self._shell(['id'], check=False)
                if 'uid=0(root)' not in current.stdout:
                    return
                time.sleep(0.25)
        except PlayerPrefsError:
            pass
        logger.warning('[GameSettings] Не удалось восстановить исходное состояние adbd без root')

    def _game_is_stopped(self) -> bool | None:
        """Убедиться, что пакет и его дочерние процессы не запущены; вернуть None, если проверить невозможно."""
        pidof = self._shell(['pidof', self.package], check=False)
        if pidof.stdout:
            return False

        processes = self._shell(['ps', '-A', '-o', 'NAME'], check=False)
        if processes.returncode != 0:
            return None
        for process in processes.stdout.splitlines():
            process = process.strip()
            if process == self.package or process.startswith(f'{self.package}:'):
                return False
        return True

    def _wait_until_game_stopped(self) -> bool:
        """Кратковременный опрос выхода приложения при перезапуске; на остальных путях запуска выполняется только однократная проверка."""
        deadline = time.monotonic() + (8 if self.wait_for_stop else 0)
        while True:
            stopped = self._game_is_stopped()
            if stopped is not False:
                return stopped is True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)

    def _prefs_path(self) -> str:
        """Найти файл Unity PlayerPrefs, отвергая догадки при несоответствии имени файла."""
        if not PACKAGE_PATTERN.fullmatch(self.package):
            raise PlayerPrefsUnsupported('Небезопасный формат имени пакета игры')

        directory = f'/data/user/0/{self.package}/shared_prefs'
        expected = f'{directory}/{self.package}.v2.playerprefs.xml'
        if self._shell(['test', '-f', expected], check=False).returncode == 0:
            return expected

        files = self._shell(['ls', '-1', directory], check=False)
        if files.returncode != 0:
            raise PlayerPrefsUnsupported('Каталог PlayerPrefs игры не найден')
        candidates = [
            name for name in files.stdout.splitlines()
            if PREFS_FILE_PATTERN.fullmatch(name)
        ]
        if len(candidates) != 1:
            raise PlayerPrefsUnsupported('Не удалось однозначно определить файл PlayerPrefs игры')
        return f'{directory}/{candidates[0]}'

    def _ensure_no_atomic_backup(self, prefs: str) -> None:
        """Предотвратить перезапись основного файла незавершенной атомарной записью Android при следующем запуске."""
        result = self._shell(['test', '-e', f'{prefs}.bak'], check=False)
        if result.returncode == 0:
            raise PlayerPrefsUnsupported('Обнаружена незавершённая атомарная запись настроек приложения; перезапись запрещена')
        if result.returncode != 1:
            raise PlayerPrefsUnsupported('Не удалось подтвердить состояние атомарной записи настроек приложения')

    def _metadata(
            self,
            remote: str,
            error_type: type[PlayerPrefsError] = PlayerPrefsUnsupported,
    ) -> PlayerPrefsMetadata:
        metadata = self._shell(['stat', '-c', '%u:%g:%a', remote], error_type=error_type).stdout
        match = METADATA_PATTERN.fullmatch(metadata)
        if match is None:
            raise error_type('Не удалось прочитать права доступа файла PlayerPrefs')

        label = self._shell(['ls', '-Zd', remote], error_type=error_type).stdout.split(maxsplit=1)
        if not label or not SELINUX_CONTEXT_PATTERN.fullmatch(label[0]):
            raise error_type('Не удалось прочитать SELinux-контекст файла PlayerPrefs')

        return PlayerPrefsMetadata(
            uid=match.group('uid'),
            gid=match.group('gid'),
            mode=match.group('mode'),
            context=label[0],
        )

    def _restore_metadata(self, remote: str, metadata: PlayerPrefsMetadata) -> None:
        self._shell(
            ['chown', f'{metadata.uid}:{metadata.gid}', remote],
            error_type=PlayerPrefsWriteError,
        )
        self._shell(['chmod', metadata.mode, remote], error_type=PlayerPrefsWriteError)
        self._shell(['chcon', metadata.context, remote], error_type=PlayerPrefsWriteError)
        if self._metadata(remote, error_type=PlayerPrefsWriteError) != metadata:
            raise PlayerPrefsWriteError('Проверка метаданных временного файла PlayerPrefs не пройдена')

    def _read_remote_bytes(self, remote: str, error_type: type[PlayerPrefsError]) -> bytes:
        """Прочитать файл напрямую в память без создания локальной копии."""
        args = ['exec-out', 'cat', remote]
        if self._use_su:
            args = ['exec-out', 'su', '-c', shlex.join(['cat', remote])]
        return self._run_adb_bytes(args, timeout=30, error_type=error_type)

    def _write_remote_bytes(
            self,
            remote: str,
            content: bytes,
            error_type: type[PlayerPrefsError],
    ) -> None:
        """Записать данные из памяти во временный файл в том же каталоге для атомарной замены."""
        command = ['exec-in', 'sh', '-c', f'cat > {remote}']
        if self._use_su:
            command = ['exec-in', 'su', '-c', shlex.join(['sh', '-c', f'cat > {remote}'])]
        self._run_adb_bytes(
            command,
            input_data=content,
            timeout=30,
            error_type=error_type,
        )

    def _cleanup_stale_transaction_files(self, prefs: str) -> None:
        """Очистить устаревшие копии предыдущих версий модуля и временные файлы прерванных транзакций без логирования имен файлов."""
        if self._game_is_stopped() is not True:
            raise PlayerPrefsUnsupported('Процесс игры запустился до очистки чувствительных временных данных; запись отменена')

        directory, filename = prefs.rsplit('/', maxsplit=1)
        files = self._shell(['ls', '-1', directory], error_type=PlayerPrefsUnsupported).stdout.splitlines()
        prefix = f'{filename}.alas-'
        stale_files = []
        for name in files:
            if not name.startswith(prefix):
                continue
            suffix = name[len(filename):]
            if (
                    LEGACY_TRANSACTION_SUFFIX_PATTERN.fullmatch(suffix)
                    or LEGACY_TEMPORARY_TRANSACTION_SUFFIX_PATTERN.fullmatch(suffix)
                    or TEMPORARY_TRANSACTION_SUFFIX_PATTERN.fullmatch(suffix)
            ):
                stale_files.append(f'{directory}/{name}')

        for remote in stale_files:
            if self._game_is_stopped() is not True:
                raise PlayerPrefsUnsupported('Процесс игры запустился во время очистки чувствительных временных данных; запись отменена')
            self._shell(['rm', '-f', remote], error_type=PlayerPrefsUnsupported)

    def _restore_original(
            self,
            target: str,
            metadata: PlayerPrefsMetadata,
            original: bytes,
            temporary: str,
    ) -> bool:
        """Восстановить целевой файл только из исходного текста в памяти и подтвердить полное совпадение содержимого с оригиналом."""
        if self._game_is_stopped() is not True:
            return False
        try:
            self._write_remote_bytes(temporary, original, PlayerPrefsWriteError)
            self._restore_metadata(temporary, metadata)
            if self._read_remote_bytes(temporary, PlayerPrefsWriteError) != original:
                return False
            if self._game_is_stopped() is not True:
                return False
            self._shell(['mv', temporary, target], error_type=PlayerPrefsWriteError)
            return (
                self._read_remote_bytes(target, PlayerPrefsWriteError) == original
                and self._metadata(target, error_type=PlayerPrefsWriteError) == metadata
            )
        except PlayerPrefsError:
            return False

    def _apply_locked(self) -> bool:
        if not self._wait_until_game_stopped():
            raise PlayerPrefsUnsupported('Процесс игры всё ещё запущен; запись пропущена')
        if not self._ensure_root():
            raise PlayerPrefsUnsupported('ADB не получил права root')
        if not self._wait_until_game_stopped():
            raise PlayerPrefsUnsupported('Процесс игры запустился во время повышения привилегий; запись отменена')

        prefs = self._prefs_path()
        self._cleanup_stale_transaction_files(prefs)
        self._ensure_no_atomic_backup(prefs)
        metadata = self._metadata(prefs)
        temporary = f'{prefs}.alas-tmp-{secrets.token_hex(8)}.tmp'
        replace_attempted = False
        original = b''

        try:
            original = self._read_remote_bytes(prefs, PlayerPrefsUnsupported)
            modified, changes = update_player_prefs_xml(original)
            if not changes.changed:
                logger.info('[GameSettings] Рекомендуемые локальные настройки игры уже применены; запись не требуется')
                return True

            if not self._wait_until_game_stopped():
                raise PlayerPrefsUnsupported('Процесс игры запустился перед записью; запись отменена')

            self._write_remote_bytes(temporary, modified, PlayerPrefsWriteError)
            self._restore_metadata(temporary, metadata)
            if self._read_remote_bytes(temporary, PlayerPrefsWriteError) != modified:
                raise PlayerPrefsWriteError('Проверка содержимого временной записи PlayerPrefs не пройдена')

            if not self._wait_until_game_stopped():
                raise PlayerPrefsUnsupported('Процесс игры запустился перед заменой файла; запись отменена')
            replace_attempted = True
            self._shell(['mv', temporary, prefs], error_type=PlayerPrefsWriteError)
            applied = self._read_remote_bytes(prefs, PlayerPrefsWriteError)
            verify_player_prefs_xml(
                applied,
                changes.standby_keys,
                changes.story_speed_keys,
            )
            if self._metadata(prefs, error_type=PlayerPrefsWriteError) != metadata:
                raise PlayerPrefsWriteError('Метаданные файла PlayerPrefs после записи неверны')
        except PlayerPrefsError:
            if replace_attempted:
                restored = self._restore_original(prefs, metadata, original, temporary)
                if not restored:
                    logger.critical('[GameSettings] Запись локальных настроек не удалась, и восстановление исходного состояния из памяти также не удалось; запуск игры заблокирован')
                    raise RequestHumanTakeover from None
                logger.warning('[GameSettings] Запись локальных настроек не удалась; исходное состояние восстановлено')
            else:
                logger.warning('[GameSettings] Запись локальных настроек не завершена; исходный файл не заменён')
            return False
        finally:
            try:
                self._shell(['rm', '-f', temporary], check=False)
            except PlayerPrefsError:
                logger.warning('[GameSettings] Не удалось очистить временные данные этой записи')

        logger.info(
            '[GameSettings] Записано %s статических настроек, %s настроек скорости сюжета и %s настроек режима ожидания',
            changes.static_changed,
            changes.story_speed_changed,
            changes.standby_changed,
        )
        return True

    def apply(self) -> bool:
        """Безопасно применить рекомендуемые настройки; невозможность безопасного выполнения не препятствует обычному запуску."""
        if getattr(self.device, 'is_over_http', False):
            logger.warning('[GameSettings] HTTP-устройство не поддерживает автоматическую настройку локальных параметров игры; пропуск')
            return False
        try:
            with _device_lock(str(self.device.serial), self.package):
                return self._apply_locked()
        except PlayerPrefsUnsupported:
            logger.warning('[GameSettings] Автоматическая настройка локальных параметров игры пропущена: проверка безопасности не пройдена')
            return False
        finally:
            self._restore_root_state()


def apply_recommended_game_settings(device, wait_for_stop: bool = False) -> bool:
    """Единая точка входа во время выполнения для применения рекомендуемых настроек к текущему устройству."""
    return PlayerPrefsManager(device, wait_for_stop=wait_for_stop).apply()
