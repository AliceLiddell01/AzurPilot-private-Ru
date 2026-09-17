"""Экстрактор игровых настроек: разбирает параметры PlayerPrefs из исходного кода Lua Azur Lane.
Сопоставляет вызовы GetInt/GetFloat/GetString через регулярные выражения,
автоматически извлекая имена ключей, типы и значения по умолчанию."""

import os
import re
from dataclasses import dataclass

from tqdm import tqdm

from module.base.decorator import cached_property
from module.device.method.utils import removeprefix

# Регулярное выражение для сопоставления вызовов PlayerPrefs
REGEX_SETTING = re.compile(r'PlayerPrefs.Get(\w{1,10})\((.*)\)')
# Регулярное выражение для извлечения имени ключа настройки
REGEX_SETTING_KEY = re.compile(r'"(.*?)"')


def _strip_code(string):
    """Извлечь фрагмент кода, соответствующий внешним круглым скобкам в строке.

    Args:
        string: Строка кода, содержащая скобки.

    Yields:
        Символы внутри скобок до достижения внешней закрывающей скобки.
    """
    nested = 0
    for word in string:
        if word == '(':
            nested += 1
        if word == ')':
            # Завершаем при достижении внешней закрывающей скобки
            if nested == 1:
                yield word
                return
            nested -= 1
        yield word


def strip_code(string):
    """Объединить вывод генератора _strip_code в единую строку."""
    return ''.join(list(_strip_code(string)))


@dataclass
class Field:
    """Определение поля игровой настройки для хранения прочитанного параметра PlayerPrefs.

    Attributes:
        formatter: Функция преобразования типа значения (int/str/float).
        default: Значение по умолчанию.
        regex: Регулярное выражение для сопоставления имени ключа настройки.
    """
    formatter: callable
    default: ''
    regex: str


@dataclass
class LuaSetting:
    """Параметр PlayerPrefs, извлеченный из Lua-скрипта.

    Attributes:
        raw: Исходная строка кода.
        typ: Тип значения ("Int", "String", "Float").
        code: Фрагмент кода с именем ключа и значением по умолчанию.
        duplicate: Является ли элемент дубликатом.
    """
    raw: str
    typ: str  # "Int", "String", "Float"
    code: str  # Например, "AUTOFIGHT_BATTERY_SAVEMODE, 0" или "world_help_progress".

    duplicate = False

    @cached_property
    def default(self):
        """Разобрать значение по умолчанию для параметра.

        Returns:
            Значение по умолчанию int/str/float в зависимости от типа, либо None при ошибке разбора.
        """
        if ',' in self.code:
            name, default = self.code.split(',', 1)
            default = default.strip(' ",')
            if self.typ == 'Int':
                try:
                    return int(default)
                except ValueError:
                    return 0
            if self.typ == 'String':
                return repr(default)
            if self.typ == 'Float':
                try:
                    return float(default)
                except ValueError:
                    return 0.
        else:
            if self.typ == 'Int':
                return 0
            if self.typ == 'String':
                return repr('')
            if self.typ == 'Float':
                return 0.
        return None

    @cached_property
    def key(self):
        """Извлечь имя ключа настройки, заменяя специальные символы на подчеркивание.

        Returns:
            Очищенная строка имени ключа или пустая строка, если извлечь не удалось.
        """
        if ',' in self.code:
            code = self.code.rsplit(',', 1)[0].strip(' ')
        else:
            code = self.code.strip(' ')

        res = REGEX_SETTING_KEY.search(code)
        if res:
            return res.group(1).replace('.', '_').replace('%', '_').replace('-', '_').replace(':', '_').strip('_')
        else:
            return ''

    @cached_property
    def formatter(self):
        """Получить имя функции форматирования для соответствующего типа значения.

        Returns:
            'int', 'str' или 'float'.
        """
        if self.typ == 'Int':
            return 'int'
        if self.typ == 'String':
            return 'str'
        if self.typ == 'Float':
            return 'float'
        return 'str'

    @cached_property
    def regex(self):
        """Сгенерировать регулярное выражение для сопоставления имени ключа настройки.

        Returns:
            Строковое представление (repr) регулярного выражения.
        """
        if ',' in self.code:
            code = self.code.rsplit(',', 1)[0].strip(' ')
        else:
            code = self.code.strip(' ')

        pieces = code.split('..')

        def iter_piece():
            for piece in pieces:
                res = REGEX_SETTING_KEY.search(piece)
                if res:
                    yield res.group(1)
                else:
                    yield '(.*)'

        return repr(''.join(list(iter_piece())))

    @cached_property
    def generated(self):
        """Сгенерировать строки кода Python для этого параметра настройки.

        Returns:
            Список строк кода с комментариями и оператором присваивания.
        """
        if self.key == '':
            return [
                f'# {self.raw}',
                'pass  # Неизвестно'
            ]
        if self.duplicate:
            return [
                f'# {self.raw}',
                'pass  # Повтор'
            ]

        return [
            f'# {self.raw}',
            f'{self.key} = Field(formatter={self.formatter}, default={self.default}, regex={self.regex})'
        ]


class SettingExtractor:
    """Извлекает настройки PlayerPrefs из скриптов Lua и генерирует файл определений Python."""

    @staticmethod
    def iter_setting_from_file(file):
        """Извлечь все настройки PlayerPrefs из одного файла Lua.

        Args:
            file: Путь к файлу Lua.

        Yields:
            Объекты LuaSetting, каждый соответствует одному вызову PlayerPrefs.
        """
        with open(file, mode='r', encoding='utf8') as f:
            data = list(f.readlines())

        for row in data:
            row = row.strip()
            res = REGEX_SETTING.search(row)
            if res:
                row = strip_code(res.group(0))
                res = REGEX_SETTING.search(row)
                if res:
                    yield LuaSetting(raw=row, typ=res.group(1), code=res.group(2))

    @staticmethod
    def iter_file_from_folder(folder):
        """Рекурсивно обойти все файлы в каталоге.

        Args:
            folder: Путь к целевой директории.

        Yields:
            Полный путь к файлу.
        """
        for path, folders, files in os.walk(folder):
            for file in files:
                file = f'{path}/{file}'
                yield file

    def iter_generated_lines(self, folder):
        """Сгенерировать все строки кода для файла настроек.

        Args:
            folder: Путь к каталогу с Lua-скриптами.

        Yields:
            Строки кода Python, включая импорты, определение класса и присваивание полей.
        """
        dic_settings = set()
        yield 'from module.game_setting.setting_extractor import Field'
        yield ''
        yield '# Автоматически сгенерировано module/game_setting/setting_extractor.py'
        yield '# Не изменяйте вручную.'
        yield ''
        yield ''
        yield 'class GameSettingsGenerated:'
        files = list(self.iter_file_from_folder(folder))
        for file in tqdm(files):
            settings = list(self.iter_setting_from_file(file))
            if not settings:
                continue
            yield ''
            f = removeprefix(file, folder).replace("\\", "/")
            yield f'    # {f}'
            for setting in settings:
                if setting.key in dic_settings:
                    setting.duplicate = True
                dic_settings.add(setting.key)
                for line in setting.generated:
                    yield f'    {line}'

    def generate(self, folder, output='./module/game_setting/setting_generated.py'):
        """Сгенерировать файл определений настроек Python.

        Args:
            folder: Путь к каталогу с Lua-скриптами.
            output: Путь к выходному файлу, по умолчанию setting_generated.py.
        """
        lines = [l + '\n' for l in self.iter_generated_lines(folder)]
        with open(output, mode='w', encoding='utf8') as f:
            f.writelines(lines)


if __name__ == '__main__':
    # Путь к AzurLaneLuaScripts\CN
    FOLDER = r''
    ex = SettingExtractor()
    ex.generate(FOLDER)
