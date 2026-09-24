"""Модуль утилит-декораторов.

Предоставляет декоратор диспетчеризации методов по конфигурации Config.when(),
а также часто используемые декораторы cached_property, timer, function_drop, run_once
для управления поведением выполнения методов.
"""

import random
import re
from functools import wraps
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


class cached_class_property(Generic[T]):
    """Read-only class property cached independently for every subclass."""

    class AliasConflict(ValueError):
        pass

    def __init__(self, func: Callable[..., T]):
        self.__func__ = func
        self.__cache_name__ = f"_{func.__name__.strip('_')}_"
        if self.__cache_name__ == func.__name__:
            raise self.AliasConflict(self.__cache_name__)

    def __get__(self, instance, cls=None) -> T:
        if cls is None:
            cls = type(instance)
        try:
            return vars(cls)[self.__cache_name__]
        except KeyError:
            result = self.__func__(cls)
            setattr(cls, self.__cache_name__, result)
            return result


class Config:
    """Декоратор вызова одноимённых методов с разной реализацией в зависимости от конфигурации.

    Пример структуры func_list:
    func_list = {
        'func1': [
            {'options': {'ENABLE': True}, 'func': 1},
            {'options': {'ENABLE': False}, 'func': 1}
        ]
    }
    """
    func_list = {}

    @classmethod
    def when(cls, **kwargs):
        """
        Args:
            **kwargs: Любые параметры конфигурации из AzurLaneConfig.

        Examples:
            @Config.when(USE_ONE_CLICK_RETIREMENT=True)
            def retire_ships(self, amount=None, rarity=None):
                pass

            @Config.when(USE_ONE_CLICK_RETIREMENT=False)
            def retire_ships(self, amount=None, rarity=None):
                pass
        """
        from module.logger import logger
        options = kwargs

        def decorate(func):
            name = func.__name__
            data = {'options': options, 'func': func}
            if name not in cls.func_list:
                cls.func_list[name] = [data]
            else:
                override = False
                for record in cls.func_list[name]:
                    if record['options'] == data['options']:
                        record['func'] = data['func']
                        override = True
                if not override:
                    cls.func_list[name].append(data)

            @wraps(func)
            def wrapper(self, *args, **kwargs):
                """
                Args:
                    self: Экземпляр ModuleBase.
                    *args: Позиционные аргументы.
                    **kwargs: Именованные аргументы.
                """
                for record in cls.func_list[name]:

                    flag = [value is None or self.config.__getattribute__(key) == value
                            for key, value in record['options'].items()]
                    if not all(flag):
                        continue

                    return record['func'](self, *args, **kwargs)

                logger.warning(f'[Декоратор] Для {name} нет подходящего варианта; используется последняя определённая функция')
                return func(self, *args, **kwargs)

            return wrapper

        return decorate


class cached_property(Generic[T]):
    """Декоратор кэшируемого свойства с поддержкой типизации.

    Источник: https://github.com/pydanny/cached-property
    Исходная реализация: https://github.com/bottlepy/bottle/commit/fa7733e075da0d790d809aa3d2f53071897e6f76

    Значение свойства вычисляется только один раз для каждого экземпляра,
    после чего заменяется обычным атрибутом.
    Удаление свойства сбрасывает кэш.
    """

    def __init__(self, func: Callable[..., T]):
        self.func = func

    def __get__(self, obj, cls) -> T:
        if obj is None:
            return self

        value = obj.__dict__[self.func.__name__] = self.func(obj)
        return value


def del_cached_property(obj, name):
    """Безопасно удалить кэшированное свойство.

    Args:
        obj: Целевой объект.
        name: Имя свойства.
    """
    try:
        del obj.__dict__[name]
    except KeyError:
        pass


def has_cached_property(obj, name):
    """Проверить, закэшировано ли свойство.

    Args:
        obj: Целевой объект.
        name: Имя свойства.

    Returns:
        Возвращает True, если свойство уже закэшировано, иначе False.
    """
    return name in obj.__dict__


def set_cached_property(obj, name, value):
    """Установить значение кэшированного свойства.

    Args:
        obj: Целевой объект.
        name: Имя свойства.
        value: Значение свойства.
    """
    obj.__dict__[name] = value


def function_drop(rate=0.5, default=None):
    """Случайно отбрасывать вызовы функции для имитации зависания эмулятора в тестах.

    Args:
        rate: Вероятность отбрасывания в диапазоне от 0 до 1.
        default: Значение по умолчанию, возвращаемое при отбрасывании.

    Examples:
        @function_drop(0.3)
        def click(self, button, record_check=True):
            pass

        30% вероятность:
        INFO | Dropped: module.device.device.Device.click(REWARD_GOTO_MAIN, record_check=True)
        70% вероятность:
        INFO | Click (1091,  628) @ REWARD_GOTO_MAIN
    """
    from module.logger import logger

    def decorate(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if random.uniform(0, 1) > rate:
                return func(*args, **kwargs)
            else:
                cls = ''
                arguments = [str(arg) for arg in args]
                if len(arguments):
                    matched = re.search('<(.*?) object at', arguments[0])
                    if matched:
                        cls = matched.group(1) + '.'
                        arguments.pop(0)
                arguments += [f'{k}={v}' for k, v in kwargs.items()]
                arguments = ', '.join(arguments)
                logger.info(f'[Декоратор] Вызов отброшен: {cls}{func.__name__}({arguments})')
                return default

        return wrapper

    return decorate


def run_once(f):
    """Гарантировать, что функция выполнится только один раз, сколько бы её ни вызывали.

    Examples:
        @run_once
        def my_function(foo, bar):
            return foo + bar

        while 1:
            my_function()

    Examples:
        def my_function(foo, bar):
            return foo + bar

        action = run_once(my_function)
        while 1:
            action()
    """

    def wrapper(*args, **kwargs):
        if not wrapper.has_run:
            wrapper.has_run = True
            return f(*args, **kwargs)

    wrapper.has_run = False
    return wrapper
