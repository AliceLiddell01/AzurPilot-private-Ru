"""Модуль управления ресурсами.

Управляет регистрацией экземпляров Button и Template, сбросом кэша и оптимизацией памяти.
При смене задач освобождает ресурсы, которые больше не требуются (модели OCR,
изображения шаблонов, кэш распознавания карты), для контроля потребления памяти при длительной работе.

Типичное потребление памяти:
    - каждая модель OCR: около 20 МБ
    - ресурсы UI (около 80 кнопок Button): около 3 МБ
    - каждое изображение шаблона: около 6 МБ
    - кэш изображений распознавания карты: переменный объём
"""

import gc
import re

import module.config.server as server
from module.base.decorator import cached_property, del_cached_property


def get_assets_from_file(file, regex):
    """Извлечь имена констант ресурсов из исходного файла Python с помощью регулярного выражения.

    Args:
        file (str): Путь к исходному файлу.
        regex (re.Pattern): Скомпилированное регулярное выражение с одной группой захвата.

    Returns:
        set[str]: Множество совпавших имён констант ресурсов.
    """
    assets = set()
    with open(file, 'r', encoding='utf-8') as f:
        for row in f.readlines():
            result = regex.search(row)
            if result:
                assets.add(result.group(1))
    return assets


class PreservedAssets:
    """Сбор UI-ресурсов, которые необходимо сохранять при переключении задач.

    Эти ресурсы используются для обнаружения экранов и навигации; их сброс приведёт
    к невозможности корректно определить текущую страницу.

    Attributes:
        ui (set[str]): Множество имён сохраняемых ресурсов UI, включая кнопки навигации UI и кнопки обработки всплывающих окон.
    """

    @cached_property
    def ui(self):
        assets = set()
        assets |= get_assets_from_file(
            file='./module/ui/assets.py',
            regex=re.compile(r'^([A-Za-z][A-Za-z0-9_]+) = ')
        )
        assets |= get_assets_from_file(
            file='./module/ui/ui.py',
            regex=re.compile(r'\(([A-Z][A-Z0-9_]+),')
        )
        assets |= get_assets_from_file(
            file='./module/handler/info_handler.py',
            regex=re.compile(r'\(([A-Z][A-Z0-9_]+),')
        )
        # MAIN_CHECK эквивалентен MAIN_GOTO_CAMPAIGN
        # assets.add('MAIN_GOTO_CAMPAIGN')
        return assets


# Глобальный экземпляр для определения ресурсов, которые нужно сохранять
_preserved_assets = PreservedAssets()


class Resource:
    """Базовый класс для всех ресурсов Button и Template.

    Предоставляет механизм глобальной регистрации экземпляров ресурсов и функциональность сброса кэша.
    Все объекты Button и Template автоматически регистрируются в словаре `instances` при загрузке модуля,
    а при смене задач кэш загруженных изображений пакетом освобождается через `resource_release()`.

    Attributes:
        instances (dict[str, Resource]): Глобальный реестр экземпляров ресурсов;
            ключ — идентификатор ресурса (обычно путь к файлу или имя ресурса), значение — экземпляр Resource.
        cached (list[str]): Список имён свойств, кэш которых необходимо сбрасывать;
            подклассы должны поддерживать этот список при создании кэшируемых свойств.
    """
    # Атрибут класса: хранит все экземпляры кнопок и шаблонов
    instances = {}
    # Атрибут экземпляра: хранит список имён кэшируемых свойств
    cached = []

    def resource_add(self, key):
        """Зарегистрировать текущий экземпляр в глобальной таблице ресурсов.

        Args:
            key (str): Уникальный идентификатор ресурса.
        """
        Resource.instances[key] = self

    def resource_release(self):
        """Сбросить все кэшированные свойства текущего экземпляра.

        Вызывает `del_cached_property` для удаления кэшированных значений свойств,
        чтобы при следующем обращении изображение пересчитывалось или загружалось заново.
        """
        for cache in self.cached:
            del_cached_property(self, cache)

    @classmethod
    def is_loaded(cls, obj):
        """Проверить, загружены ли данные изображения для объекта ресурса.

        Args:
            obj: Объект Button или Template.

        Returns:
            bool: Возвращает True, если данные изображения уже загружены.
        """
        if hasattr(obj, '_image') and obj._image is None:
            return False
        elif hasattr(obj, 'image') and obj.image is None:
            return False
        return True

    @classmethod
    def resource_show(cls):
        """Вывести информацию обо всех незагруженных ресурсах для отладки.

        Выводит список ресурсов в текущем реестре, данные изображений которых ещё не загружены.
        """
        from module.logger import logger
        logger.hr('Отображение ресурсов')
        for key, obj in cls.instances.items():
            if cls.is_loaded(obj):
                continue
            logger.info(f'{obj}: {key}')

    @staticmethod
    def parse_property(data, s=None):
        """Разобрать значение свойства объекта Button или Template.

        Поддерживает как словарь с разделением по серверам, так и прямое значение.
        Если значение является словарём, выбирается значение для текущего сервера.

        Args:
            data: Значение свойства. Может быть словарём (по серверам) или прямым значением.
            s (str | None): Идентификатор сервера ('cn', 'en', 'jp', 'tw').
                Если None, используется глобальный `server.server`.

        Returns:
            Разобранное значение свойства.

        Example:
            >>> Resource.parse_property({'cn': (100, 200), 'en': (110, 210)}, s='cn')
            (100, 200)
            >>> Resource.parse_property((100, 200))
            (100, 200)
        """
        if s is None:
            s = server.server
        if isinstance(data, dict):
            return data[s]
        else:
            return data


def release_resources(next_task=''):
    """Освободить больше не требующиеся ресурсы для оптимизации потребления памяти.

    Вызывается в период простоя планировщика задач; освобождает три категории ресурсов:
    1. Модели OCR (каждая около 20 МБ)
    2. Кэш изображений Button/Template (ресурсы UI около 3 МБ, изображения шаблонов около 6 МБ каждое)
    3. Кэшированные изображения распознавания карты

    Стратегия освобождения динамически адаптируется в зависимости от следующей задачи:
    - При скором запуске Operation Siren / заказов сохраняются модели OCR
    - При наличии последующих задач сохраняются модель azur_lane и ресурсы навигации UI
    - В состоянии простоя освобождаются все ресурсы

    Args:
        next_task (str): Имя следующей задачи. Пустая строка означает состояние простоя.
    """
    released_ocr_models = 0
    from deploy.config import DeployConfig
    if DeployConfig().UseOcrServer:
        if not next_task:
            # В состоянии простоя отключаемся от OCR-сервера
            from module.ocr.ocr import OCR_MODEL
            try:
                OCR_MODEL.close()
            except AttributeError:
                pass
    else:
        # Освобождаем только при использовании локального OCR
        from module.ocr.al_ocr import release_ocr_models
        from module.ocr.ocr import OCR_MODEL
        # The Global OCR namespace is retained between active tasks.
        models = [] if next_task else ['azur_lane']
        for model in models:
            del_cached_property(OCR_MODEL, model)

        if models:
            cache_names = list(models)
            # The shared detection cache is released only while idle.
            if not next_task:
                cache_names.append('det')
            released_ocr_models = release_ocr_models(names=cache_names)

    # Освобождаем кэш ресурсов
    # В module.ui около 80 ресурсов, занимающих примерно 3 МБ
    # Всего в Alas около 800 ресурсов, но загружаются не все
    # Изображения шаблонов занимают больше: примерно 6 МБ каждое
    for key, obj in Resource.instances.items():
        # Сохраняем ресурсы, необходимые для переключения UI
        if next_task and str(obj) in _preserved_assets.ui:
            continue
        # if Resource.is_loaded(obj):
        #     logger.info(f'Release {obj}')
        obj.resource_release()

    # Освобождаем кэшированные изображения обнаружения карты
    from module.map_detection.utils_assets import ASSETS
    attr_list = [
        'ui_mask',
        'ui_mask_os',
        'ui_mask_stroke',
        'ui_mask_in_map',
        'ui_mask_os_in_map',
        'tile_center_image',
        'tile_corner_image',
        'tile_corner_image_list'
    ]
    for attr in attr_list:
        del_cached_property(ASSETS, attr)

    # Счётчик ссылок изображений NumPy/OpenCV освобождает их сразу; только когда глобальный OCR-кэш действительно очищен
    # собираем возможные циклические ссылки Python, чтобы не вносить паузы GC в циклы скриншотов и боя.
    if released_ocr_models:
        gc.collect(2)
