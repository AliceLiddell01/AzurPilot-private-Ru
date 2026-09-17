"""Модуль кодогенератора.

Предоставляет кодогенератор CodeGenerator с управлением отступами и контекстный менеджер TabWrapper,
используемые для автоматической генерации кода конфигурации Python (например, config_generated.py).
"""

import typing as t


class TabWrapper:
    """Контекстный менеджер управления отступами.

    Увеличивает уровень отступа при входе и уменьшает при выходе,
    а также отвечает за добавление префиксного и суффиксного кода.
    """

    def __init__(self, generator, prefix='', suffix='', newline=True):
        """
        Args:
            generator: Экземпляр генератора кода, которому принадлежит обёртка.
            prefix: Префиксный код, выводимый при входе в контекст.
            suffix: Суффиксный код, выводимый при выходе из контекста.
            newline: Добавлять ли перевод строки после префикса.
        """
        self.generator = generator
        self.prefix = prefix
        self.suffix = suffix
        self.newline = newline

        self.nested = False

    def __enter__(self):
        if not self.nested and self.prefix:
            self.generator.add(self.prefix, newline=self.newline)
        self.generator.tab_count += 1
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.generator.tab_count -= 1
        if self.suffix:
            self.generator.add(self.suffix)

    def __repr__(self):
        return self.prefix

    def set_nested(self, suffix=''):
        self.nested = True
        self.suffix += suffix


class CodeGenerator:
    """Генератор исходного кода Python.

    Предоставляет набор методов для построения кода Python с корректными отступами,
    поддерживает генерацию import, переменных, классов, функций, списков, словарей и других структур.
    """

    def __init__(self):
        self.tab_count = 0
        self.lines = []

    def generate(self) -> t.Iterable[str]:
        """Сгенерировать строки кода; подклассы должны переопределять этот метод."""
        yield ''

    def add(self, line, comment=False, newline=True):
        """Добавить строку кода в буфер вывода.

        Args:
            line: Добавляемый текст кода.
            comment: Выводить ли в качестве комментария (с автодобавлением префикса #).
            newline: Добавлять ли символ перевода строки в конце.
        """
        self.lines.append(self._line_with_tabs(line, comment=comment, newline=newline))

    def print(self):
        """Вывести сгенерированный код в консоль."""
        lines = ''.join(self.lines)
        print(lines)

    def write(self, file: str = None):
        """Записать сгенерированный код в файл.

        Args:
            file: Путь к выходному файлу.
        """
        lines = ''.join(self.lines)
        with open(file, 'w', encoding='utf-8', newline='') as f:
            f.write(lines)

    def _line_with_tabs(self, line, comment=False, newline=True):
        """Отформатировать строку кода в соответствии с текущим уровнем отступа.

        Args:
            line: Исходный текст кода.
            comment: Добавлять ли префикс комментария.
            newline: Добавлять ли завершающий символ перевода строки.

        Returns:
            Строка кода с отступом.
        """
        if comment:
            line = '# ' + line
        out = '    ' * self.tab_count + line
        if newline:
            out += '\n'
        return out

    def _repr(self, obj):
        """Преобразовать объект в представление кода.

        Многострочный текст в строках форматируется как docstring.
        Остальные объекты выводятся через repr().

        Args:
            obj: Объект для преобразования.

        Returns:
            Строковое представление объекта в виде кода.
        """
        if isinstance(obj, str):
            if '\n' in obj:
                out = '"""\n'
                with self.tab():
                    for line in obj.strip().split('\n'):
                        line = line.strip()
                        out += self._line_with_tabs(line)
                out += self._line_with_tabs('"""', newline=False)
                return out
        return repr(obj)

    def tab(self):
        """Создать контекстный менеджер отступа.

        Returns:
            Экземпляр TabWrapper для использования в операторе `with`.
        """
        return TabWrapper(self)

    def Empty(self):
        """Добавить пустую строку."""
        self.add('')

    def Import(self, text, empty=2):
        """Добавить блок операторов import.

        Args:
            text: Текст операторов import, разделённых переносами строк.
            empty: Количество пустых строк после блока import, по умолчанию 2.
        """
        for line in text.strip().split('\n'):
            line = line.strip()
            self.add(line)
        for _ in range(empty):
            self.Empty()

    def Value(self, key=None, value=None, type_=None, **kwargs):
        """Добавить инструкцию присваивания переменной.

        Args:
            key: Имя переменной.
            value: Значение переменной.
            type_: Аннотация типа (опционально).
            **kwargs: Дополнительные пары ключ-значение; для каждой генерируется отдельная строка присваивания.
        """
        if key is not None:
            if type_ is not None:
                self.add(f'{key}: {type_} = {self._repr(value)}')
            else:
                self.add(f'{key} = {self._repr(value)}')
        for key, value in kwargs.items():
            self.Value(key, value)

    def Comment(self, text):
        """Добавить блок комментариев.

        Args:
            text: Текст комментария, строки разделены переносами; к каждой строке автоматически добавляется префикс #.
        """
        for line in text.strip().split('\n'):
            line = line.strip()
            self.add(line, comment=True)

    def List(self, key=None):
        """Создать контекст списка.

        Args:
            key: Имя переменной списка. Если None, генерируется анонимный список.

        Returns:
            Экземпляр TabWrapper для генерации кода списка через оператор `with`.
        """
        if key is not None:
            return TabWrapper(self, prefix=str(key) + ' = [', suffix=']')
        else:
            return TabWrapper(self, prefix='[', suffix=']', newline=False)

    def ListItem(self, value):
        """Добавить элемент в список.

        Args:
            value: Значение элемента списка; может быть обычным значением или вложенной структурой TabWrapper.
        """
        if isinstance(value, TabWrapper):
            value.set_nested(suffix=',')
            self.add(f'{self._repr(value)}')
            return value
        else:
            self.add(f'{self._repr(value)},')

    def Dict(self, key=None):
        """Создать контекст словаря.

        Args:
            key: Имя переменной словаря. Если None, генерируется анонимный словарь.

        Returns:
            Экземпляр TabWrapper для генерации кода словаря через оператор `with`.
        """
        if key is not None:
            return TabWrapper(self, prefix=str(key) + ' = {', suffix='}')
        else:
            return TabWrapper(self, prefix='{', suffix='}', newline=False)

    def DictItem(self, key=None, value=None):
        """Добавить пару ключ-значение в словарь.

        Args:
            key: Ключ словаря.
            value: Значение словаря; может быть обычным значением или вложенной структурой TabWrapper.
        """
        if isinstance(value, TabWrapper):
            value.set_nested(suffix=',')
            if key is not None:
                self.add(f'{self._repr(key)}: {self._repr(value)}')
            return value
        else:
            if key is not None:
                self.add(f'{self._repr(key)}: {self._repr(value)},')

    def Object(self, object_class, key=None):
        """Создать контекст инстанцирования объекта.

        Args:
            object_class: Строковое имя класса.
            key: Имя переменной для присваивания. Если None, генерируется анонимный вызов конструктора.

        Returns:
            Экземпляр TabWrapper для генерации кода создания объекта через оператор `with`.
        """
        if key is not None:
            return TabWrapper(self, prefix=f'{key} = {object_class}(', suffix=')')
        else:
            return TabWrapper(self, prefix=f'{object_class}(', suffix=')', newline=False)

    def ObjectAttr(self, key=None, value=None):
        """Добавить параметр-атрибут к объекту.

        Args:
            key: Имя атрибута. Если None, передаётся как позиционный аргумент.
            value: Значение атрибута; может быть обычным значением или вложенной структурой TabWrapper.
        """
        if isinstance(value, TabWrapper):
            value.set_nested(suffix=',')
            if key is None:
                self.add(f'{self._repr(value)}')
            else:
                self.add(f'{key}={self._repr(value)}')
            return value
        else:
            if key is None:
                self.add(f'{self._repr(value)},')
            else:
                self.add(f'{key}={self._repr(value)},')

    def Class(self, name, inherit=None):
        """Создать контекст определения класса.

        Args:
            name: Имя класса.
            inherit: Имя родительского класса (опционально).

        Returns:
            Экземпляр TabWrapper для генерации определения класса через оператор `with`.
        """
        if inherit is not None:
            return TabWrapper(self, prefix=f'class {name}({inherit}):')
        else:
            return TabWrapper(self, prefix=f'class {name}:')

    def Def(self, name, args=''):
        """Создать контекст определения функции.

        Args:
            name: Имя функции.
            args: Строка со списком параметров (опционально).

        Returns:
            Экземпляр TabWrapper для генерации определения функции через оператор `with`.
        """
        return TabWrapper(self, prefix=f'def {name}({args}):')


generator = CodeGenerator()
Import = generator.Import
Value = generator.Value
Comment = generator.Comment
Dict = generator.Dict
DictItem = generator.DictItem
