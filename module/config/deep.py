"""Модуль высокопроизводительного доступа к вложенным словарям.

Предоставляет функции deep_get, deep_set, deep_pop, deep_iter и др.,
предназначенные для быстрого доступа и модификации конфигурационных данных во вложенных словарях и списках.
"""

from collections import deque

# Функции семейства deep_* предназначены для доступа к вложенным словарям.
# Приоритет — высокая производительность, поэтому читаемость кода ниже.
# В обычных тестах производительности затраты располагаются в следующем порядке:
# - Когда key существует
#   try: dict[key] except KeyError << dict.get(key) < if key in dict: dict[key]
# - Когда key не существует
#   if key in dict: dict[key] < dict.get(key) <<< try: dict[key] except KeyError

OP_ADD = 'add'
OP_SET = 'set'
OP_DEL = 'del'


def deep_get(d, keys, default=None):
    """Безопасно получить значение из вложенных словарей и списков.

    Ссылка: https://stackoverflow.com/questions/25833613/safe-method-to-get-value-of-nested-dictionary

    Args:
        d: Целевой словарь.
        keys (list[str] | str): Путь ключей, например ['Scheduler', 'NextRun', 'value'].
            Также поддерживается строка с точками: 'Scheduler.NextRun.value'.
        default: Значение по умолчанию, возвращаемое при отсутствии ключа.

    Returns:
        Значение по указанному пути либо default при отсутствии.
    """
    # 240 + 30 * depth (ns)
    if type(keys) is str:
        keys = keys.split('.')

    try:
        for k in keys:
            d = d[k]
        return d
    # Ключ не существует
    except KeyError:
        return default
    # Индекс вне диапазона
    except IndexError:
        return default
    # keys не итерируется или d не является словарём: индекс списка должен быть int или slice, а не str
    except TypeError:
        return default


def deep_get_with_error(d, keys):
    """Получить значение из вложенных словарей и списков с вызовом KeyError при отсутствии ключа.

    Args:
        d: Целевой словарь.
        keys (list[str] | str): Путь ключей, например ['Scheduler', 'NextRun', 'value'].
            Также поддерживается строка с точками: 'Scheduler.NextRun.value'.

    Returns:
        Значение по указанному пути.

    Raises:
        KeyError: Вызывается, если ключ отсутствует.
    """
    # 240 + 30 * depth (ns)
    if type(keys) is str:
        keys = keys.split('.')

    try:
        for k in keys:
            d = d[k]
        return d
    # Ключ не существует — KeyError пробрасывается напрямую
    # except KeyError:
    #     raise
    # Индекс вне диапазона
    except IndexError:
        raise KeyError
    # keys не итерируется или d не является словарём: индекс списка должен быть int или slice, а не str
    except TypeError:
        raise KeyError


def deep_exist(d, keys):
    """Проверить наличие указанного пути ключей во вложенном словаре или списке.

    Args:
        d: Целевой словарь.
        keys (str | list): Путь ключей, например 'Scheduler.NextRun.value' или в виде списка.

    Returns:
        bool: Существует ли ключ.
    """
    # 240 + 30 * depth (ns)
    if type(keys) is str:
        keys = keys.split('.')

    try:
        for k in keys:
            d = d[k]
        return True
    # Ключ не существует
    except KeyError:
        return False
    # Индекс вне диапазона
    except IndexError:
        return False
    # keys не итерируется или d не является словарём: индекс списка должен быть int или slice, а не str
    except TypeError:
        return False


def deep_set(d, keys, value):
    """Безопасно записать значение во вложенный словарь, повторяя логику обхода пути ключей deep_get().

    Поддерживает только тип dict, списки не поддерживаются.
    """
    # 150 * depth (ns)
    if type(keys) is str:
        keys = keys.split('.')

    first = True
    exist = True
    prev_d = None
    prev_k = None
    prev_k2 = None
    try:
        for k in keys:
            if first:
                prev_d = d
                prev_k = k
                first = False
                continue
            try:
                # Порядок производительности: if key in dict: dict[key] > dict.get > dict.setdefault > try dict[key] except
                if exist and prev_k in d:
                    prev_d = d
                    d = d[prev_k]
                else:
                    exist = False
                    new = {}
                    d[prev_k] = new
                    d = new
            except TypeError:
                # d не является словарём
                exist = False
                d = {}
                prev_d[prev_k2] = {prev_k: d}

            prev_k2 = prev_k
            prev_k = k
            # prev_k2, prev_k = prev_k, k
    # keys не итерируется
    except TypeError:
        return

    # Последний ключ: устанавливаем значение
    try:
        d[prev_k] = value
        return
    # Последний d не является словарём
    except TypeError:
        prev_d[prev_k2] = {prev_k: value}
        return


def deep_default(d, keys, value):
    """Безопасно записать значение по умолчанию во вложенный словарь (только если ключ отсутствует), повторяя логику обхода пути ключей deep_get().

    Поддерживает только тип dict, списки не поддерживаются.
    """
    # 150 * depth (ns)
    if type(keys) is str:
        keys = keys.split('.')

    first = True
    exist = True
    prev_d = None
    prev_k = None
    prev_k2 = None
    try:
        for k in keys:
            if first:
                prev_d = d
                prev_k = k
                first = False
                continue
            try:
                # Порядок производительности: if key in dict: dict[key] > dict.get > dict.setdefault > try dict[key] except
                if exist and prev_k in d:
                    prev_d = d
                    d = d[prev_k]
                else:
                    exist = False
                    new = {}
                    d[prev_k] = new
                    d = new
            except TypeError:
                # d не является словарём
                exist = False
                d = {}
                prev_d[prev_k2] = {prev_k: d}

            prev_k2 = prev_k
            prev_k = k
            # prev_k2, prev_k = prev_k, k
    # keys не итерируется
    except TypeError:
        return

    # Последний ключ: устанавливаем значение по умолчанию
    try:
        d.setdefault(prev_k, value)
        return
    # Последний d не является словарём
    except AttributeError:
        prev_d[prev_k2] = {prev_k: value}
        return


def deep_pop(d, keys, default=None):
    """Извлечь со значением (pop) элемент из вложенных словарей и списков."""
    if type(keys) is str:
        keys = keys.split('.')

    try:
        for k in keys[:-1]:
            d = d[k]
        # Не используем pop(k, default), чтобы поддерживать pop у списков
        return d.pop(keys[-1])
    # Ключ не существует
    except KeyError:
        return default
    # keys не итерируется или d не является словарём: индекс списка должен быть int или slice, а не str
    except TypeError:
        return default
    # Индекс keys вне диапазона
    except IndexError:
        return default
    # Последний d не является словарём и не имеет метода pop
    except AttributeError:
        return default


def deep_iter_depth1(data):
    """Эквивалентно data.items(), но без выброса ошибки, если data не является словарём.

    Args:
        data: Данные для обхода.

    Yields:
        Any: Ключ.
        Any: Значение.
    """
    try:
        for k, v in data.items():
            yield k, v
        return
    except AttributeError:
        # data не является словарём
        return


def deep_iter_depth2(data):
    """Обойти пары ключ-значение вложенного словаря на глубину 2; упрощённая версия deep_iter.

    Args:
        data: Вложенный словарь для обхода.

    Yields:
        Any: Ключ первого уровня.
        Any: Ключ второго уровня.
        Any: Значение.
    """
    try:
        for k1, v1 in data.items():
            if type(v1) is dict:
                for k2, v2 in v1.items():
                    yield k1, k2, v2
    except AttributeError:
        # data не является словарём
        return


def deep_iter(data, min_depth=None, depth=3):
    """Обойти пары ключ-значение вложенного словаря.

    Справка по производительности: при depth=3 обход alas.json (530+ строк) занимает ~300 мкс.
    Поддерживает только тип dict.

    Args:
        data: Вложенный словарь для обхода.
        min_depth: Минимальная глубина обхода; уровни выше используются только для построения пути.
        depth: Максимальная глубина обхода.

    Yields:
        list[str]: Путь ключей.
        Any: Значение.
    """
    if min_depth is None:
        min_depth = depth
    assert 1 <= min_depth <= depth

    # Эквивалентно dict.items()
    try:
        if depth == 1:
            for k, v in data.items():
                yield [k], v
            return
        # Обходим первый уровень
        elif min_depth == 1:
            q = deque()
            for k, v in data.items():
                key = [k]
                if type(v) is dict:
                    q.append((key, v))
                else:
                    yield key, v
        # Обходим только целевую глубину
        else:
            q = deque()
            for k, v in data.items():
                key = [k]
                if type(v) is dict:
                    q.append((key, v))
    except AttributeError:
        # data не является словарём
        return

    # Поуровневый обход
    current = 2
    while current <= depth:
        new_q = deque()
        # Максимальная глубина
        if current == depth:
            for key, data in q:
                for k, v in data.items():
                    yield key + [k], v
        # В диапазоне целевых глубин
        elif min_depth <= current < depth:
            for key, data in q:
                for k, v in data.items():
                    subkey = key + [k]
                    if type(v) is dict:
                        new_q.append((subkey, v))
                    else:
                        yield subkey, v
        # Минимальная глубина ещё не достигнута
        else:
            for key, data in q:
                for k, v in data.items():
                    subkey = key + [k]
                    if type(v) is dict:
                        new_q.append((subkey, v))
        q = new_q
        current += 1


def deep_values(data, min_depth=None, depth=3):
    """Обойти все значения во вложенном словаре.

    Справка по производительности: при depth=3 обход alas.json (530+ строк) занимает ~300 мкс.
    Поддерживает только тип dict.

    Args:
        data: Вложенный словарь для обхода.
        min_depth: Минимальная глубина обхода.
        depth: Максимальная глубина обхода.

    Yields:
        Any: Значение.
    """
    if min_depth is None:
        min_depth = depth
    assert 1 <= min_depth <= depth

    # Эквивалентно dict.values()
    try:
        if depth == 1:
            for v in data.values():
                yield v
            return
        # Обходим первый уровень
        elif min_depth == 1:
            q = deque()
            for v in data.values():
                if type(v) is dict:
                    q.append(v)
                else:
                    yield v
        # Обходим только целевую глубину
        else:
            q = deque()
            for v in data.values():
                if type(v) is dict:
                    q.append(v)
    except AttributeError:
        # data не является словарём
        return

    # Поуровневый обход
    current = 2
    while current <= depth:
        new_q = deque()
        # Максимальная глубина
        if current == depth:
            for data in q:
                for v in data.values():
                    yield v
        # В диапазоне целевых глубин
        elif min_depth <= current < depth:
            for data in q:
                for v in data.values():
                    if type(v) is dict:
                        new_q.append(v)
                    else:
                        yield v
        # Минимальная глубина ещё не достигнута
        else:
            for data in q:
                for v in data.values():
                    if type(v) is dict:
                        new_q.append(v)
        q = new_q
        current += 1


def deep_iter_diff(before, after):
    """Обойти различия между двумя словарями.

    Сравнение двух глубоко вложенных словарей выполняется очень быстро; время пропорционально количеству различий.

    Args:
        before: Словарь до изменений.
        after: Словарь после изменений.

    Yields:
        list[str]: Путь ключей.
        Any: Значение в before, либо None при отсутствии.
        Any: Значение в after, либо None при отсутствии.
    """
    if before == after:
        return
    if type(before) is not dict or type(after) is not dict:
        yield [], before, after
        return

    queue = deque([([], before, after)])
    while True:
        new_queue = deque()
        for path, d1, d2 in queue:
            keys1 = set(d1.keys())
            keys2 = set(d2.keys())
            for key in keys1.union(keys2):
                try:
                    val2 = d2[key]
                except KeyError:
                    # Безопасно обращаемся к d1[key], поскольку key взят из объединения обоих словарей
                    # Если его нет в d2, он обязательно есть в d1
                    yield path + [key], d1[key], None
                    continue
                try:
                    val1 = d1[key]
                except KeyError:
                    yield path + [key], None, val2
                    continue
                # Сначала сравниваем словари — это быстро
                if val1 != val2:
                    if type(val1) is dict and type(val2) is dict:
                        new_queue.append((path + [key], val1, val2))
                    else:
                        yield path + [key], val1, val2
        queue = new_queue
        if not queue:
            break


def deep_iter_patch(before, after):
    """Обойти события патча от before к after, аналогично генерации json-patch.

    Сравнение двух глубоко вложенных словарей выполняется очень быстро; время пропорционально количеству различий.

    Args:
        before: Словарь до изменений.
        after: Словарь после изменений.

    Yields:
        str: Тип операции: OP_ADD, OP_SET или OP_DEL.
        list[str]: Путь ключей.
        Any: Значение в after, либо None при событии OP_DEL.
    """
    if before == after:
        return
    if type(before) is not dict or type(after) is not dict:
        yield OP_SET, [], after
        return

    queue = deque([([], before, after)])
    while True:
        new_queue = deque()
        for path, d1, d2 in queue:
            keys1 = set(d1.keys())
            keys2 = set(d2.keys())
            for key in keys1.union(keys2):
                try:
                    val2 = d2[key]
                except KeyError:
                    yield OP_DEL, path + [key], None
                    continue
                try:
                    val1 = d1[key]
                except KeyError:
                    yield OP_ADD, path + [key], val2
                    continue
                # Сначала сравниваем словари — это быстро
                if val1 != val2:
                    if type(val1) is dict and type(val2) is dict:
                        new_queue.append((path + [key], val1, val2))
                    else:
                        yield OP_SET, path + [key], val2
        queue = new_queue
        if not queue:
            break
