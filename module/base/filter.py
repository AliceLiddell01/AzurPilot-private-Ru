"""Модуль системы фильтрации по регулярным выражениям.

Предоставляет класс Filter на основе регулярных выражений для разбора
и сопоставления правил фильтрации игровых предметов (кораблей, снаряжения и т. д.).
Поддерживает предустановленные строки и синтаксис сортировки по приоритету через ">".
"""

from functools import reduce
import re

from module.logger import logger


class Filter:
    def __init__(self, regex, attr, preset=()):
        """
        Args:
            regex: Регулярное выражение для разбора строки фильтрации.
            attr: Список имён атрибутов объекта, строго соответствующий группам захвата regex.
            preset: Встроенный список строк предустановок, возвращаемых напрямую без разбора регулярным выражением.
        """
        if isinstance(regex, str):
            regex = re.compile(regex)
        self.regex = regex
        self.attr = attr
        self.preset = tuple(list(p.lower() for p in preset))
        self.filter_raw = []
        self.filter = []

    def load(self, string):
        """
        Загрузить строку фильтрации, где условия разделены знаком ">".

        Также заменяет различные похожие символы Unicode на стандартный знак ">".
        """
        string = str(string)
        string = re.sub(r'[ \t\r\n]', '', string)
        string = re.sub(r'[＞﹥›˃ᐳ❯]', '>', string)
        self.filter_raw = string.split('>')
        self.filter = [self.parse_filter(f) for f in self.filter_raw]

    def is_preset(self, filter):
        return len(filter) and filter.lower() in self.preset

    def apply(self, objs, func=None):
        """
        Применить условия фильтрации к списку объектов и вернуть совпавшие результаты.

        Args:
            objs: Смешанный список объектов и строк предустановок.
            func: Опциональная дополнительная функция фильтрации; принимает объект, возвращает True для сохранения.

        Returns:
            Список совпавших объектов и строк предустановок, например [object, object, 'reset'].
        """
        out = []
        for raw, filter in zip(self.filter_raw, self.filter):
            if self.is_preset(raw):
                raw = raw.lower()
                if raw not in out:
                    out.append(raw)
            else:
                for index, obj in enumerate(objs):
                    if self.apply_filter_to_obj(obj=obj, filter=filter) and obj not in out:
                        out.append(obj)

        if func is not None:
            objs, out = out, []
            for obj in objs:
                if isinstance(obj, str):
                    out.append(obj)
                elif func(obj):
                    out.append(obj)
                else:
                    # Отбрасываем этот объект
                    pass

        return out

    def applys(self, objs, funcs):
        """
        Последовательно применить несколько функций фильтрации к списку объектов.

        Args:
            objs: Смешанный список объектов и строк предустановок.
            funcs: Список функций фильтрации; каждая принимает объект и возвращает True для сохранения.
                Объект сохраняется только в том случае, если все функции вернули True.

        Returns:
            Список совпавших объектов и строк предустановок, например [object, object, 'reset'].
        """
        return self.apply(objs, func=lambda x: all(func(x)for func in funcs))

    def apply_filter_to_obj(self, obj, filter):
        """
        Проверить, удовлетворяет ли объект условиям фильтра.

        Args:
            obj: Проверяемый объект.
            filter: Список условий фильтра, взаимно однозначно соответствующий `self.attr`.

        Returns:
            Удовлетворяет ли объект условиям фильтра.
        """

        for attr, value in zip(self.attr, filter):
            if not value:
                continue

            obj_val = obj.__getattribute__(attr)
            
            # Разрешаем универсальные предметы, например PlateT3 без конкретного sub_genre
            # Они соответствуют правилам фильтра с конкретным sub_genre
            if attr == 'sub_genre' and obj_val is None:
                continue

            if str(obj_val).lower() != str(value):
                return False

        return True

    def parse_filter(self, string):
        """
        Разобрать строку одиночного условия фильтрации.

        Args:
            string: Строка условия фильтрации.

        Returns:
            Разобранный список значений атрибутов; для некорректного фильтра возвращает ['1nVa1d', None, ...].
        """
        string = string.replace(' ', '').lower()
        result = re.search(self.regex, string)

        if self.is_preset(string):
            return [string]

        if result and len(string) and result.span()[1]:
            return [result.group(index + 1) for index, attr in enumerate(self.attr)]
        else:
            logger.warning(f'[Фильтр] Некорректный фильтр: "{string}". Селектор не соответствует регулярному выражению и не является предустановкой.')
            # Некорректные условия фильтра игнорируются
            # Возвращаем заведомо несовпадающее значение, чтобы гарантированно пропустить условие
            return ['1nVa1d'] + [None] * (len(self.attr) - 1)
