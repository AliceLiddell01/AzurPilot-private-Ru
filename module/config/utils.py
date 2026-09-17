"""Набор утилит для управления конфигурацией.

Предоставляет низкоуровневые вспомогательные функции для системы конфигурации:
- Чтение/запись файлов: безопасное чтение и запись JSON/YAML (атомарная запись)
- Парсинг данных: преобразование типов и разбор значений конфигурации
- Серверное время: вычисление часовых поясов серверов и времени сброса
- Управление путями: пути к файлам конфигурации, ресурсов и i18n
- Случайные ID: генерация уникальных идентификаторов экземпляров конфигурации

Определения констант:
- UI_LOCALE: единственный активный язык WebUI (ru-RU)
- LEGACY_UI_LOCALES: неактивные устаревшие языковые файлы, сохраняемые до Stage 9
- EVENT_NAME_SOURCE / EVENT_NAME_FALLBACK_ORDER: источники имён событий, не зависящие от языка UI
- SERVER_TO_TIMEZONE: соответствие серверов и часовых поясов
"""

# Этот файл содержит общие вспомогательные функции для управления конфигурацией.
# Здесь находятся низкоуровневые операции JSON/YAML, преобразование типов, серверное время и генерация случайных ID.
import json
import random
import string
from datetime import datetime, timedelta, timezone

import yaml

import module.config.server as server_
from deploy.atomic import atomic_read_text, atomic_read_bytes, atomic_write
from module.submodule.utils import *
from module.base.decorator import run_once
from module.config.constants import DEFAULT_TIME
from module.config.time_source import now as current_time, timestamp as current_timestamp
from module.logger import logger

from module.config.locale import (
    EVENT_NAME_FALLBACK_ORDER,
    EVENT_NAME_SOURCE,
    LEGACY_UI_LOCALES,
    UI_LOCALE,
)
from module.config.profile import discover_profile_configs, discover_profile_names
SERVER_TO_TIMEZONE = {
    'en': timedelta(hours=-7),
}
DEFAULT_CONFIG_NAME = 'ap'


# https://stackoverflow.com/questions/8640959/how-can-i-control-what-scalar-form-pyyaml-uses-for-my-data/15423007
def str_presenter(dumper, data):
    if len(data.splitlines()) > 1:  # Для многострочных строк используем блочный стиль
        return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|')
    return dumper.represent_scalar('tag:yaml.org,2002:str', data)


yaml.add_representer(str, str_presenter)
yaml.representer.SafeRepresenter.add_representer(str, str_presenter)


def filepath_args(filename='args', mod_name='alas'):
    if mod_name == 'alas':
        return f'./module/config/argument/{filename}.json'
    else:
        return os.path.join(get_mod_filepath(mod_name), f'./module/config/argument/{filename}.json')


def filepath_argument(filename):
    return f'./module/config/argument/{filename}.yaml'


def filepath_i18n(lang, mod_name='alas'):
    if mod_name == 'alas':
        return os.path.join('./module/config/i18n', f'{lang}.json')
    else:
        return os.path.join(get_mod_filepath(mod_name), './module/config/i18n', f'{lang}.json')


def filepath_config(filename, mod_name='alas'):
    if mod_name == 'alas':
        return os.path.join('./config', f'{filename}.json')
    else:
        return os.path.join('./config', f'{filename}.{mod_name}.json')


def filepath_code():
    return './module/config/config_generated.py'


def read_file(file):
    """
    Прочитать файл в формате .yaml или .json.
    Если файл не существует, возвращает пустой словарь.

    Args:
        file (str): Путь к файлу.

    Returns:
        dict, list: Разобранные данные.
    """
    print(f'Чтение: {file}')
    if file.endswith('.json'):
        content = atomic_read_bytes(file)
        if not content:
            return {}
        return json.loads(content)
    elif file.endswith('.yaml'):
        content = atomic_read_text(file)
        data = list(yaml.safe_load_all(content))
        if len(data) == 1:
            data = data[0]
        if not data:
            data = {}
        return data
    else:
        print(f'Неподдерживаемое расширение файла конфигурации: {file}')
        return {}


def write_file(file, data):
    """
    Записать данные в файл формата .yaml или .json.

    Args:
        file (str): Путь к файлу.
        data (dict, list): Данные для записи.
    """
    print(f'Запись: {file}')
    if file.endswith('.json'):
        content = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False, default=str)
        atomic_write(file, content)
    elif file.endswith('.yaml'):
        if isinstance(data, list):
            content = yaml.safe_dump_all(
                data, default_flow_style=False, encoding='utf-8', allow_unicode=True, sort_keys=False)
        else:
            content = yaml.safe_dump(
                data, default_flow_style=False, encoding='utf-8', allow_unicode=True, sort_keys=False)
        atomic_write(file, content)
    else:
        print(f'Неподдерживаемое расширение файла конфигурации: {file}')


def iter_folder(folder, is_dir=False, ext=None):
    """
    Перебрать файлы или подкаталоги в папке.

    Args:
        folder (str): Путь к целевой папке.
        is_dir (bool): При True перебирать только подкаталоги.
        ext (str): Фильтр по расширению файла, например `.yaml`.

    Yields:
        str: Абсолютный путь к файлу.
    """
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


def is_oobe_needed():
    """Вернуть True, когда canonical discovery не нашёл реальных профилей."""
    return not discover_profile_names('./config')


def alas_template():
    """
    Получить имена всех шаблонов экземпляров Alas.

    Returns:
        list[str]: Имена всех шаблонов Alas, кроме `template`.
    """
    out = []
    for file in os.listdir('./config'):
        name, extension = os.path.splitext(file)
        if name == 'template' and extension == '.json':
            out.append(f'{name}-alas')

    out.extend(list_mod_template())

    return out


def alas_instance():
    """Получить canonical registry профилей с legacy default fallback."""
    profiles = discover_profile_configs('./config')
    out = [profile.name for profile in profiles]
    refresh_mod_config_registry(profiles)

    if not len(out):
        out = [DEFAULT_CONFIG_NAME]

    return out


def parse_value(value, data):
    """
    Попытаться преобразовать строку во float, int или datetime.

    Args:
        value (str): Значение для преобразования.
        data (dict): Данные определения параметра, содержащие поле `option` и др.

    Returns:
        Преобразованное значение либо исходное значение при невозможности преобразования.
    """
    def parse_single(value):
        if not isinstance(value, str):
            return value
        if value == '' and not data.get('preserve_empty'):
            return None
        if value == 'true' or value == 'True':
            return True
        if value == 'false' or value == 'False':
            return False
        if '.' in value:
            try:
                return float(value)
            except ValueError:
                pass
        else:
            try:
                return int(value)
            except ValueError:
                pass
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass

        return value

    if data.get('type') == 'checkbox' and isinstance(value, list):
        # Выключенный checkbox PyWebIO возвращает [], включённый — [True].
        return any(bool(v) for v in value)

    if data.get('type') == 'multiselect':
        if value is None or value == '':
            if data.get('strict'):
                raise ValueError('Пустое значение multiselect запрещено')
            return data['value']
        if not isinstance(value, list):
            value = [value]
        value = [parse_single(item) for item in value]
        if data.get('strict') and not value:
            raise ValueError('Пустое значение multiselect запрещено')
        if 'option' in data and any(item not in data['option'] for item in value):
            if data.get('strict'):
                raise ValueError('Multiselect содержит неподдерживаемое значение')
            return data['value']
        return value

    if 'option' in data:
        if value not in data['option']:
            if data.get('strict'):
                raise ValueError('Параметр содержит неподдерживаемое значение')
            return data['value']
    value = parse_single(value)
    return value


def data_to_type(data, **kwargs):
    """
    Определить соответствующий тип элемента управления GUI по определению параметра.

    | Условие                           | Тип      |
    | ---------------------------------- | -------- |
    | Значение типа bool                 | checkbox |
    | У параметра есть список вариантов  | select   |
    | В имени содержится `Filter` (data['arg']) | textarea |
    | Прочие параметры                   | input    |

    Args:
        data (dict): Данные определения параметра.
        kwargs: Дополнительные свойства.

    Returns:
        str: Строка с типом элемента управления GUI.
    """
    kwargs.update(data)
    if isinstance(kwargs['value'], bool):
        return 'checkbox'
    elif 'option' in kwargs and kwargs['option']:
        return 'select'
    elif 'Filter' in kwargs['arg']:
        return 'textarea'
    else:
        return 'input'


def data_to_path(data):
    """
    Преобразовать данные параметра в строку пути конфигурации.

    Args:
        data (dict): Словарь, содержащий ключи `func`, `group`, `arg`.

    Returns:
        str: Путь в формате `<func>.<group>.<arg>`.
    """
    return '.'.join([data.get(attr, '') for attr in ['func', 'group', 'arg']])


def path_to_arg(path):
    """
    Преобразовать ключ словаря из .yaml файла в имя параметра конфигурации.

    Args:
        path (str): Например, `Scheduler.ServerUpdate`.

    Returns:
        str: Например, `Scheduler_ServerUpdate`.
    """
    return path.replace('.', '_')


def dict_to_kv(dictionary, allow_none=True):
    """
    Преобразовать словарь в строку формата key=value.

    Args:
        dictionary: Например, `{'path': 'Scheduler.ServerUpdate', 'value': True}`.
        allow_none (bool): Включать ли ключи со значением None.

    Returns:
        str: Например, `path='Scheduler.ServerUpdate', value=True`.
    """
    return ', '.join([f'{k}={repr(v)}' for k, v in dictionary.items() if allow_none or v is not None])


def server_timezone() -> timedelta:
    try:
        return SERVER_TO_TIMEZONE[server_.server]
    except KeyError as exc:
        raise ValueError(f"Неподдерживаемый часовой пояс сервера: {server_.server}") from exc


def server_time_offset() -> timedelta:
    """
    Вычислить смещение локального времени относительно времени сервера.

    Перевод локального времени во время сервера: server_time = local_time + server_time_offset()
    Перевод времени сервера в локальное время: local_time = server_time - server_time_offset()
    """
    return current_time(timezone.utc).astimezone().utcoffset() - server_timezone()


def random_normal_distribution_int(a, b, n=3):
    """
    Сгенерировать случайное целое число с нормальным распределением в интервале (без numpy).

    Использует среднее значение нескольких случайных чисел для аппроксимации нормального распределения.

    Args:
        a (int): Минимальное значение интервала.
        b (int): Максимальное значение интервала.
        n (int): Количество случайных чисел для аппроксимации, по умолчанию 3.

    Returns:
        int: Случайное целое число с нормальным распределением.
    """
    if a < b:
        output = sum([random.randint(a, b) for _ in range(n)]) / n
        return int(round(output))
    else:
        return b


def ensure_time(second, n=3, precision=3):
    """
    Привести входное значение ко времени с поддержкой случайного диапазона.

    Args:
        second (int, float, tuple): Значение времени, например 10, (10, 30), '10, 30'.
        n (int): Количество случайных чисел для симуляции, по умолчанию 3.
        precision (int): Точность десятичных знаков.

    Returns:
        float: Обработанное значение времени.
    """
    if isinstance(second, tuple):
        multiply = 10 ** precision
        return random_normal_distribution_int(second[0] * multiply, second[1] * multiply, n) / multiply
    elif isinstance(second, str):
        if ',' in second:
            lower, upper = second.replace(' ', '').split(',')
            lower, upper = int(lower), int(upper)
            return ensure_time((lower, upper), n=n, precision=precision)
        if '-' in second:
            lower, upper = second.replace(' ', '').split('-')
            lower, upper = int(lower), int(upper)
            return ensure_time((lower, upper), n=n, precision=precision)
        else:
            return int(second)
    else:
        return second


def get_os_next_reset():
    """
    Получить первое число следующего месяца (время сброса Operation Siren).

    Returns:
        datetime.datetime: Локальное время следующего сброса.
    """
    diff = server_time_offset()
    server_now = current_time() - diff
    server_reset = (server_now.replace(day=1) + timedelta(days=32)) \
        .replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    local_reset = server_reset + diff
    return local_reset


def get_os_reset_remain():
    """
    Получить количество оставшихся дней до следующего сброса Operation Siren.

    Returns:
        int: Количество оставшихся дней.
    """
    next_reset = get_os_next_reset()
    now = current_time()
    logger.attr('Следующий сброс Operation Siren', next_reset)

    remain = int((next_reset - now).total_seconds() // 86400)
    logger.attr('Дней до сброса', remain)
    return remain


def get_server_next_update(daily_trigger):
    """
    Получить локальное время следующего обновления сервера.

    Args:
        daily_trigger (list[str], str): Список ежедневных моментов срабатывания, например ["00:00", "12:00", "18:00"].

    Returns:
        datetime.datetime: Локальное время следующего обновления.
    """
    if isinstance(daily_trigger, str):
        daily_trigger = daily_trigger.replace(' ', '').split(',')

    diff = server_time_offset()
    local_now = current_time()
    trigger = []
    for t in daily_trigger:
        h, m = [int(x) for x in t.split(':')]
        future = local_now.replace(hour=h, minute=m, second=0, microsecond=0) + diff
        s = (future - local_now).total_seconds() % 86400
        future = local_now + timedelta(seconds=s)
        trigger.append(future)
    update = sorted(trigger)[0]
    return update


def get_server_last_update(daily_trigger):
    """
    Получить локальное время предыдущего обновления сервера.

    Args:
        daily_trigger (list[str], str): Список ежедневных моментов срабатывания, например ["00:00", "12:00", "18:00"].

    Returns:
        datetime.datetime: Локальное время предыдущего обновления.
    """
    if isinstance(daily_trigger, str):
        daily_trigger = daily_trigger.replace(' ', '').split(',')

    diff = server_time_offset()
    local_now = current_time()
    trigger = []
    for t in daily_trigger:
        h, m = [int(x) for x in t.split(':')]
        future = local_now.replace(hour=h, minute=m, second=0, microsecond=0) + diff
        s = (future - local_now).total_seconds() % 86400 - 86400
        future = local_now + timedelta(seconds=s)
        trigger.append(future)
    update = sorted(trigger)[-1]
    return update


def nearest_future(future, interval=120):
    """
    Получить ближайший момент времени в будущем.
    Если несколько моментов завершаются в пределах `interval` секунд, возвращает самый поздний из них.

    Args:
        future (list[datetime.datetime]): Список будущих моментов времени.
        interval (int): Интервал объединения в секундах.

    Returns:
        datetime.datetime: Выбранный момент времени.
    """
    future = [datetime.fromisoformat(f) if isinstance(f, str) else f for f in future]
    future = sorted(future)
    next_run = future[0]
    for finish in future:
        if finish - next_run < timedelta(seconds=interval):
            next_run = finish

    return next_run


def get_nearest_weekday_date(target):
    """
    Получить дату ближайшего целевого дня недели, начиная с текущей даты.

    Args:
        target (int): Целевой день недели (0=понедельник, 6=воскресенье).

    Returns:
        datetime.datetime: Локальное время ближайшего целевого дня недели.
    """
    diff = server_time_offset()
    server_now = current_time() - diff

    days_ahead = target - server_now.weekday()
    if days_ahead <= 0:
        # Целевая дата уже прошла — переходим на следующую неделю
        days_ahead += 7
    server_reset = (server_now + timedelta(days=days_ahead)) \
        .replace(hour=0, minute=0, second=0, microsecond=0)

    local_reset = server_reset + diff
    return local_reset


def get_server_weekday():
    """
    Получить текущий день недели по времени сервера.

    Returns:
        int: День недели (0=понедельник, 6=воскресенье).
    """
    diff = server_time_offset()
    server_now = current_time() - diff
    result = server_now.weekday()
    return result


def get_server_monthday():
    """
    Получить текущее число месяца по времени сервера.

    Returns:
        int: Число месяца.
    """
    diff = server_time_offset()
    server_now = current_time() - diff
    result = server_now.day
    return result


def random_id(length=32):
    """
    Сгенерировать случайный идентификатор.

    Args:
        length (int): Длина идентификатора, по умолчанию 32.

    Returns:
        str: Случайный AzurStat ID.
    """
    return ''.join(random.sample(string.ascii_lowercase + string.digits, length))


def to_list(text, length=1):
    """
    Преобразовать текстовую строку в список целых чисел.

    Args:
        text (str): Разделённый запятыми текст чисел, например `1, 2, 3`.
        length (int): При единственном числе развернуть в список указанной длины,
            например text='3', length=5 вернёт `[3, 3, 3, 3, 3]`.

    Returns:
        list[int]: Список целых чисел.
    """
    if text.isdigit():
        return [int(text)] * length
    out = [int(letter.strip()) for letter in text.split(',')]
    return out


def type_to_str(typ):
    """
    Преобразовать произвольный тип или объект в строку.

    Args:
        typ: Тип или объект.

    Returns:
        str: Имя типа, например `int`, `datetime.datetime`.
    """
    if not isinstance(typ, type):
        typ = type(typ).__name__
    return str(typ)


def time_delta(_timedelta):
    """
    Вычислить разницу между двумя моментами времени с разбивкой по годам, месяцам, дням, часам, минутам и секундам.

    Args:
        _timedelta (datetime.timedelta): Разница во времени.

    Returns:
        dict: Словарь с разбивкой времени, содержащий ключи 'Y', 'M', 'D', 'h', 'm', 's'.
    """
    _time_delta = abs(_timedelta.total_seconds())
    d_base = datetime(2010, 1, 1, 0, 0, 0)
    d = datetime(2010, 1, 1, 0, 0, 0)-_timedelta
    _time_dict = {
        'Y': d.year - d_base.year,
        'M': d.month - d_base.month,
        'D': d.day - d_base.day,
        'h': d.hour - d_base.hour,
        'm': d.minute - d_base.minute,
        's': d.second - d_base.second
    }
    # _sec ={
    #     'Y': 365*24*60*60,
    #     'M': 30*24*60*60,
    #     'D': 24*60*60,
    #     'h': 60*60,
    #     'm': 60,
    #     's': 1
    # }
    # for _key in _time_dict:
    #     _time_dict[_key] = int(_time_delta//_sec[_key])
    #     _time_delta = _time_delta%_sec[_key]
    return _time_dict


def readable_time(before: str, value: str) -> str:
    """
    Вычислить разницу между двумя моментами времени и вернуть понятное человеку описание.
    """
    timedata = {
        'value': value,
        'time': '',
        'time_name': 'NoData'
    }
    if not before:
        timedata['value'] = 'None'
        return timedata
    try:
        ti = datetime.fromisoformat(before)
    except ValueError:
        timedata['time_name'] = 'TimeError'
        return timedata
    if ti == DEFAULT_TIME:
        timedata['value'] = 'None'
        return timedata

    diff = current_timestamp() - ti.timestamp()
    if diff < -1:
        timedata['time_name'] = 'TimeError'
    elif diff < 60:
        timedata['time_name'] = 'JustNow'
    elif diff < 5400:
        timedata['time'] = int(diff // 60)
        timedata['time_name'] = 'MinutesAgo'
    elif diff < 129600:
        timedata['time'] = int(diff // 3600)
        timedata['time_name'] = 'HoursAgo'
    elif diff < 1296000:
        timedata['time'] = int(diff // 86400)
        timedata['time_name'] = 'DaysAgo'
    else:
        timedata['time_name'] = 'LongTimeAgo'
    return timedata

@run_once
def is_good_gpu():
    if os.name != 'nt':
        logger.info("[Конфигурация] Текущая система не Windows; GPU не используется")
        return False

    try:
        import subprocess

        res = subprocess.run(['powershell', '-NoProfile', '-Command',
                              'Get-CimInstance Win32_VideoController | ForEach-Object { $_.AdapterRAM }'],
                             capture_output=True, text=True, check=True)
        for line in res.stdout.splitlines():
            line = line.strip()
            if line:
                try:
                    # AdapterRAM указывается в байтах: 1 ГБ = 1073741824 байта
                    if int(line) >= 1073741824:
                        logger.info("[Конфигурация] Обнаружен производительный GPU")
                        return True
                except (ValueError, TypeError):
                    continue
        logger.info("[Конфигурация] Производительный GPU не обнаружен")
        return False
    except Exception:
        logger.warning("[Конфигурация] Не удалось определить производительность GPU")
        return False
    

if __name__ == '__main__':
    get_os_reset_remain()
