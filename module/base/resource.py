"""资源管理模块。

管理 Button 和 Template 实例的注册、缓存释放和内存优化。
在任务切换时释放不再需要的资源（OCR 模型、模板图像、地图检测缓存），
以控制长时间运行时的内存占用。

典型内存占用：
    - 每个 OCR 模型约 20MB
    - UI 资源（约 80 个 Button）约 3MB
    - 模板图像每个约 6MB
    - 地图检测缓存图像不定
"""

import gc
import re

import module.config.server as server
from module.base.decorator import cached_property, del_cached_property


def get_assets_from_file(file, regex):
    """从 Python 源文件中通过正则表达式提取资源常量名称。

    Args:
        file (str): 源文件路径。
        regex (re.Pattern): 编译后的正则表达式，需包含一个捕获组。

    Returns:
        set[str]: 匹配到的资源常量名称集合。
    """
    assets = set()
    with open(file, 'r', encoding='utf-8') as f:
        for row in f.readlines():
            result = regex.search(row)
            if result:
                assets.add(result.group(1))
    return assets


class PreservedAssets:
    """收集需要在任务切换时保留的 UI 资源。

    这些资源用于页面检测和导航，释放后会导致无法正确识别当前页面。

    Attributes:
        ui (set[str]): 需要保留的 UI 资源名称集合，包括 UI 导航按钮和弹窗处理按钮。
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
    """所有 Button 和 Template 资源的基类。

    提供资源实例的全局注册机制和缓存释放功能。
    所有 Button 和 Template 对象在模块加载时自动注册到 `instances` 字典中，
    任务切换时通过 `resource_release()` 批量释放已加载的图像缓存。

    Attributes:
        instances (dict[str, Resource]): 全局资源实例注册表，
            键为资源标识符（通常为文件路径或资源名称），值为 Resource 实例。
        cached (list[str]): 需要释放缓存的属性名称列表，
            子类应在创建缓存属性时维护此列表。
    """
    # Атрибут класса: хранит все экземпляры кнопок и шаблонов
    instances = {}
    # Атрибут экземпляра: хранит список имён кэшируемых свойств
    cached = []

    def resource_add(self, key):
        """将当前实例注册到全局资源表。

        Args:
            key (str): 资源的唯一标识符。
        """
        Resource.instances[key] = self

    def resource_release(self):
        """释放当前实例的所有缓存属性。

        调用 `del_cached_property` 删除已缓存的属性值，
        使下次访问时重新计算或重新加载图像。
        """
        for cache in self.cached:
            del_cached_property(self, cache)

    @classmethod
    def is_loaded(cls, obj):
        """检查资源对象是否已加载图像数据。

        Args:
            obj: Button 或 Template 对象。

        Returns:
            bool: 如果图像数据已加载则返回 True。
        """
        if hasattr(obj, '_image') and obj._image is None:
            return False
        elif hasattr(obj, 'image') and obj.image is None:
            return False
        return True

    @classmethod
    def resource_show(cls):
        """打印所有未加载的资源信息，用于调试。

        输出当前注册表中尚未加载图像数据的资源列表。
        """
        from module.logger import logger
        logger.hr('Отображение ресурсов')
        for key, obj in cls.instances.items():
            if cls.is_loaded(obj):
                continue
            logger.info(f'{obj}: {key}')

    @staticmethod
    def parse_property(data, s=None):
        """解析 Button 或 Template 对象的属性值。

        支持按服务器区分的字典格式和直接值格式。
        当属性值为字典时，根据当前服务器选择对应的值。

        Args:
            data: 属性值。可以是字典（按服务器区分）或直接值。
            s (str | None): 服务器标识，如 'cn'、'en'、'jp'、'tw'。
                为 None 时使用全局 `server.server`。

        Returns:
            解析后的属性值。

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
    """释放不再需要的资源以优化内存占用。

    在任务调度的空闲期调用，释放三类资源：
    1. OCR 模型（每个约 20MB）
    2. Button/Template 图像缓存（UI 资源约 3MB，模板图像每个约 6MB）
    3. 地图检测缓存图像

    释放策略根据下一个任务动态调整：
    - 大世界/委托任务即将执行时，保留 OCR 模型
    - 有后续任务时，保留 azur_lane 模型和 UI 导航资源
    - 空闲时释放所有资源

    Args:
        next_task (str): 下一个任务名称。空字符串表示空闲状态。
    """
    released_ocr_models = 0
    from module.webui.setting import State
    if State.deploy_config.UseOcrServer:
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
