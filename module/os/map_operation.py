"""
大世界海域操作模块。

负责大世界（Operation Siren）模式下的海域层面操作，包括海域名称识别、
当前海域检测、海域初始化、特殊海域退出等功能。

主要类:
    OSMapOperation: 海域操作类，整合命令处理器、任务处理器、港口处理器等。

功能:
    - 多服务器海域名称 OCR 识别（CN/EN/JP/TW），包含大量服务器特有的 OCR 修正逻辑。
    - 海域初始化：处理弹窗、动画延迟、海域名称识别。
    - 特殊海域（隐秘海域、深渊海域、要塞）的进入和退出。
    - 指挥喵搜索状态检测和进度读取。

术语:
    海域 (Zone): 大世界地图上的一个可进入区域，有安全区/危险区等类型。
    隐秘海域 (Obscure Zone): 特殊海域类型，需要特定条件才能进入。
    深渊海域 (Abyssal Zone): 特殊海域类型，包含强力敌人。
    要塞 (Stronghold): 塞壬要塞，特殊海域类型。
    安全区 (Safe Zone): 已完全清理的海域。
    危险区 (Danger Zone): 未完全清理的海域。
"""
from module.base.decorator import Config
from module.base.timer import Timer
from module.base.utils import *
from module.exception import MapDetectionError, ScriptError
from module.logger import logger
from module.ocr.ocr import Ocr
from module.os.assets import *
from module.os.globe_zone import Zone
from module.os.map_fleet_selector import OSFleetSelector
from module.os_handler.assets import AUTO_SEARCH_REWARD, EXCHANGE_CHECK
from module.os_handler.map_order import MapOrderHandler
from module.os_handler.mission import MissionHandler
from module.os_handler.port import PortHandler
from module.os_handler.storage import StorageHandler
from module.ui.assets import BACK_ARROW, OS_CHECK


def _remove_zone_suffix(name, suffixes, trim_chars=''):
    """从海域名称中移除后缀和尾部字符。

    先移除尾部指定字符，再匹配并移除已知后缀。
    用于多服务器的海域名称标准化。

    Args:
        name (str): 原始海域名称。
        suffixes (tuple[str, ...]): 要移除的后缀列表。
        trim_chars (str): 要从尾部移除的字符集合。

    Returns:
        str: 处理后的海域名称。
    """
    while trim_chars and any(name.endswith(char) for char in trim_chars):
        name = name[:-1]
    for suffix in suffixes:
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name


class OSMapOperation(MapOrderHandler, MissionHandler, PortHandler, StorageHandler, OSFleetSelector):
    """大世界海域操作类。

    整合地图命令处理器、任务处理器、港口处理器、仓库处理器和舰队选择器，
    提供海域层面的完整操作能力。

    Attributes:
        zone (Zone): 当前海域对象。
        is_zone_name_hidden (bool): 海域名称是否被安全区标记遮挡。
    """
    zone: Zone
    is_zone_name_hidden = False

    def is_meowfficer_searching(self):
        """
        Returns:
            bool: 是否有指挥喵正在搜索中。

        Page:
            in: IN_MAP
        """
        return self.appear(MEOWFFICER_SEARCHING, offset=(10, 10))

    def no_meowfficer_searching(self):
        """
        Returns:
            bool: 是否没有指挥喵搜索中且没有自动搜索奖励。

        Page:
            in: IN_MAP
        """
        return not self.appear(AUTO_SEARCH_REWARD, offset=(50, 50)) and not self.is_meowfficer_searching()

    def get_meowfficer_searching_percentage(self):
        """
        Returns:
            float: 指挥喵搜索进度百分比，范围 0 到 1。

        Pages:
            in: IN_MAP, is_meowfficer_searching == True
        """
        return color_bar_percentage(
            self.device.image, area=MEOWFFICER_SEARCHING_PERCENTAGE.area, prev_color=(74, 223, 255))

    @Config.when(SERVER='en')
    def get_zone_name(self):
        # Только для EN-сервера
        ocr = Ocr(MAP_NAME, lang='azur_lane', letter=(206, 223, 247), threshold=96, name='OCR_OS_MAP_NAME')
        name = ocr.ocr(self.device.image)
        name = "".join(name.split())
        name = name.lower()
        name = name.strip('\\/-—–－')
        if '-' in name:
            name = name.split('-')[0]
        if 'é' in name:  # Сопоставление названий Средиземного моря
            name = name.replace('é', 'e')
        if 'nvcity' in name:  # OCR порта NY City ошибочно распознаёт 'Y' как 'V'
            name = 'nycity'
        if 'cibraltar' in name:
            name = 'gibraltar'
        # Исправление периодических ошибок OCR
        name = name.replace('sate', 'safe')
        self.is_zone_name_hidden = 'safe' in name

        # Периодические ошибки OCR: оперативные исправления
        name = name.replace('pasage', 'passage')
        name = name.replace('shef', 'shelf')
        name = name.replace('nnocean', 'naocean')
        # Исправление A OceanwsectorB-Safe zone
        name = re.sub('^aocean', 'naocean',  name)

        # `-` отсутствует или из-за размера шрифта распознаётся как '.'
        name = name.replace('safe', '')
        name = name.replace('zone', '')
        if name.endswith('.'):
            name = name[0:-1]
        return name

    @Config.when(SERVER='jp')
    def get_zone_name(self):
        # Только для JP-сервера
        ocr = Ocr(MAP_NAME, lang='jp', letter=(157, 173, 192), threshold=127, name='OCR_OS_MAP_NAME')
        name = ocr.ocr(self.device.image)
        name = name.replace(' ', '')
        # Нормализуем различные тире в дефис
        import re
        name = re.sub(r'[\\/—–－−]', '-', name)
        name = name.strip('-')
        self.is_zone_name_hidden = '安全' in name
        # Удаляем знаки препинания
        for char in '・':
            name = name.replace(char, '')
        # Удаляем '異常海域' и 'セイレーン要塞海域'
        if '異' in name:
            name = name.split('異')[0]
        if 'セ' in name:
            name = name.split('セ')[0]
        
        if '-' in name:
            name = name.split('-')[0]
        else:
            # Удаляем суффиксы '安全海域' или '秘密海域' в конце результата JP OCR.
            name = _remove_zone_suffix(
                name,
                ('安全海域', '秘密海域', '異常海域', '要塞海域', '安全', '秘密', '異常', '要塞'),
            )
        # Иероглифы '一', '力' и '卜' не используются, но катакана 'ー', 'カ' и 'ト' иногда ошибочно распознаётся как эти иероглифы.
        # Катакана 'ペ' может ошибочно распознаваться как хирагана 'ぺ'.
        name = name.replace('一', 'ー').replace('力', 'カ').replace('卜', 'ト').replace('ぺ', 'ペ')
        name = name.replace('ジブフルタル', 'ジブラルタル')
        name = name.replace('タント', 'タラント').replace('タフント', 'タラント')
        name = name.replace('N海域', 'NA海域')
        # リバプル -> リバープール
        name = name.replace('リバプル', 'リバープール')
        name = name.replace('リバープル', 'リバープール')
        name = name.replace('リバプール', 'リバープール')
        return name

    @Config.when(SERVER='tw')
    def get_zone_name(self):
        # Только для TW-сервера
        ocr = Ocr(MAP_NAME, lang='tw', letter=(198, 215, 239), threshold=127, name='OCR_OS_MAP_NAME')
        name = ocr.ocr(self.device.image)
        name = name.replace(' ', '')
        # Нормализуем различные тире в дефис
        import re
        name = re.sub(r'[\\/—–－−一]', '-', name)
        name = name.strip('-')
        self.is_zone_name_hidden = '安全' in name
        # Удаляем '塞壬要塞海域'
        if '塞' in name:
            name = name.split('塞')[0]
            
        if '-' in name:
            name = name.split('-')[0]
        else:
            # Удаляем суффиксы '安全海域', '隱秘海域' и '深淵海域' в конце результата TW OCR.
            name = _remove_zone_suffix(
                name,
                ('安全海域', '隱秘海域', '深淵海域', '塞壬要塞海域', '安全', '隱秘', '深淵'),
            )
        return name

    @Config.when(SERVER=None)
    def get_zone_name(self):
        # Только для CN-сервера
        ocr = Ocr(MAP_NAME, lang='cnocr', letter=(214, 231, 255), threshold=127, name='OCR_OS_MAP_NAME')
        name = ocr.ocr(self.device.image)
        name = name.replace(' ', '')
        # Нормализуем различные тире в дефис
        import re
        name = re.sub(r'[\\/—–－−]', '-', name)
        name = name.strip('-')
        self.is_zone_name_hidden = '安全' in name
        if '-' in name:
            name = name.split('-')[0]
        else:
            name = _remove_zone_suffix(
                name,
                ('安全海域', '隐秘海域', '深渊海域', '塞壬要塞海域', '安全', '隐秘', '深渊'),
            )
        return name

    def get_current_zone(self):
        """
        Returns:
            Zone: 当前海域对象。

        Raises:
            MapDetectionError: 解析海域名称失败时抛出。
            ScriptError: 脚本错误时抛出。
        """
        name = self.get_zone_name()
        logger.info(f'[Операция «Сирена» — действия на карте] Название карты обработано: {name}')
        try:
            self.zone = self.name_to_zone(name)
        except ScriptError as e:
            raise MapDetectionError(*e.args) from e
        logger.attr('Зона', self.zone)
        self.zone_config_set()
        return self.zone

    def zone_config_set(self):
        """根据当前海域区域设置地图检测参数。

        区域 5（极地海域）使用不同的边缘颜色范围和角落检测方向。
        """
        if self.zone.region == 5:
            self.config.HOMO_EDGE_COLOR_RANGE = (0, 8)
            self.config.MAP_ENSURE_EDGE_INSIGHT_CORNER = 'bottom'
        else:
            self.config.HOMO_EDGE_COLOR_RANGE = (0, 33)
            self.config.MAP_ENSURE_EDGE_INSIGHT_CORNER = ''

    def zone_init(self, fallback_init=True):
        """
        包装 get_current_zone()，设置 self.zone 为当前海域。
        进入新海域后必须调用此方法。处理地图事件和海域名称从顶部出现的动画。

        Args:
            fallback_init (bool): 无法解析海域名称时，是否从全球地图获取海域。

        Returns:
            Zone: 当前海域对象。

        Raises:
            MapDetectionError: 解析海域名称失败时抛出。
        """
        logger.hr('[Операция «Сирена» — действия на карте] Инициализация зоны')
        self.wait_os_map_buttons()
        logger.info('[Операция «Сирена» — действия на карте] Получение названия зоны')
        timeout = Timer(1.5, count=5).start()
        for _ in self.loop():
            # Обрабатываем всплывающие окна
            if self.handle_map_event():
                timeout.reset()
                continue
            # Баг игры: окно AUTO_SEARCH_REWARD от предыдущей уже очищенной зоны
            if self.appear_then_click(AUTO_SEARCH_REWARD, offset=(50, 50), interval=3):
                continue
            # Окно EXCHANGE_CHECK после ежемесячного сброса
            if self.is_in_globe():
                self.os_globe_goto_map()
                timeout.reset()
                continue
            if self.appear(EXCHANGE_CHECK, offset=(30, 30), interval=3):
                self.device.click(BACK_ARROW)
                timeout.reset()
                continue
            # Обрабатываем заголовок завершённого задания: он может перекрыть название карты или вызвать ошибку OCR из-за дополнительного текста
            if self.is_in_map() and \
                    not self.appear(OS_CHECK, offset=(20, 20)):
                self.wait_until_appear(OS_CHECK)
                timeout.reset()
                continue

            if timeout.reached():
                logger.warning('[Операция «Сирена» — действия на карте] Истекло время инициализации зоны')
                break
            if self.is_in_map():
                try:
                    return self.get_current_zone()
                except MapDetectionError:
                    continue
            else:
                timeout.reset()

        if fallback_init:
            logger.warning('[Операция «Сирена» — карта] Не удалось получить название зоны; текущая зона определяется по глобусу')
            if hasattr(self, 'get_current_zone_from_globe'):
                return self.get_current_zone_from_globe()
            else:
                logger.warning('[Операция «Сирена» — карта] Метод OperationSiren.get_current_zone_from_globe() отсутствует')
                if not self.is_in_map():
                    logger.warning('[Операция «Сирена» — действия на карте] Запрошено название зоны вне карты Операции «Сирена»')
                return self.get_current_zone()

    def is_in_special_zone(self):
        """
        Returns:
            bool: 是否在隐秘海域、深渊海域或要塞中。
        """
        return self.appear(MAP_EXIT, offset=(20, 20), similarity=0.75)

    def map_exit(self):
        """
        从隐秘海域、深渊海域或要塞中退出。

        Pages:
            in: is_in_map
            out: is_in_map, 来源海域
        """
        logger.hr('[Операция «Сирена» — действия на карте] Выход с карты')
        confirm_timer = Timer(1, count=2)
        changed = False
        for _ in self.loop():
            # Условие завершения
            if changed and self.is_in_map():
                if confirm_timer.reached():
                    break
            else:
                confirm_timer.reset()
            # Если MAP_EXIT всё ещё отображается, зона ещё не покинута
            if self.appear(MAP_EXIT, offset=(20, 20), similarity=0.75):
                confirm_timer.reset()

            # Нажатия
            if self.appear_then_click(MAP_EXIT, offset=(20, 20), interval=3, similarity=0.75):
                continue
            if self.handle_popup_confirm('MAP_EXIT'):
                self.interval_reset(MAP_EXIT)
                continue
            if self.appear_then_click(AUTO_SEARCH_REWARD, offset=(50, 50)):
                # Иногда появляется
                self.device.screenshot_interval_set()
                continue
            if self.handle_map_event():
                self.interval_reset(MAP_EXIT)
                changed = True
                continue

        self.zone_init()
