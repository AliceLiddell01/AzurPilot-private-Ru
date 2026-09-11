"""
科研项目筛选器。

本模块负责从截图中检测科研项目列表，并根据用户配置的筛选规则
对项目进行排序和过滤，输出符合优先级的候选项目列表。

支持两种服务器检测策略：
- JP 服务器：逐个点击项目进入详情页，通过模板匹配识别系列、
  类型、消耗和舰船信息（因 JP 服务器无 OCR 项目名称）
- 其他服务器：通过 OCR 识别项目名称 + 模板匹配识别系列编号

筛选规则基于正则表达式解析用户配置的过滤器字符串，
支持按系列(S1-S9)、舰船、稀有度(DR/PRY)、类型(B/C/D/E/G/H/Q/T)、
编号和时长进行多维度筛选，并支持 preset（预设）和 custom（自定义）两种模式。

术语对照：
    系列(Series): 科研系列编号 S1-S9
    类型(Genre): 科研项目类型 B/C/D/E/G/H/Q/T
    蓝图(Blueprint): 科研产出的舰船设计图
    DR: 决战方案(Dreamship Rarity)，金色稀有度科研舰船
    PRY: 近代方案(Priority Rarity)，紫色稀有度科研舰船
"""
import re
from functools import partial

from module.base.decorator import Config
from module.base.filter import Filter
from module.base.timer import Timer
from module.config.config_generated import GeneratedConfig
from module.logger import logger
from module.research.assets import *
from module.research.preset import *
from module.research.project import research_detect, research_jp_detect
from module.research.ui import ResearchUI

RESEARCH_ENTRANCE = [ENTRANCE_1, ENTRANCE_2, ENTRANCE_3, ENTRANCE_4, ENTRANCE_5]
FILTER_REGEX = re.compile('(s[123456789])?'
                          '-?'
                          '(neptune|monarch|ibuki|izumo|roon|saintlouis'
                          '|seattle|georgia|kitakaze|azuma|friedrich'
                          '|gascogne|champagne|cheshire|drake|mainz|odin'
                          '|anchorage|hakuryu|agir|august|marcopolo'
                          '|plymouth|rupprecht|harbin|chkalov|brest'
                          '|kearsarge|hindenburg|shimanto|schultz|flandre'
                          '|napoli|nakhimov|halford|bayard|daisen'
                          '|goudenleeuw|mecklenburg|dmitri|kansas|vittorio'
                          '|valparaiso|maximmelmann|duncan|takahashi|orage)?'
                          '(dr|pry)?'
                          '([bcdeghqt])?'
                          '-?'
                          '(\d{3})?'
                          '(\d.\d|\d\d?)?')
FILTER_ATTR = ('series', 'ship', 'ship_rarity', 'genre', 'number', 'duration')
FILTER_PRESET = ('shortest', 'cheapest', 'reset')
FILTER = Filter(FILTER_REGEX, FILTER_ATTR, FILTER_PRESET)


class ResearchSelector(ResearchUI):
    """
    科研项目筛选器，负责检测和筛选科研项目。

    从截图中识别 5 个科研项目的名称、系列和状态，然后根据用户
    配置的筛选规则（preset 或 custom）对项目进行优先级排序。

    JP 服务器使用逐个点击详情页的检测策略，其他服务器使用 OCR + 模板匹配。

    Attributes:
        projects (list[ResearchProject]): 当前屏幕上的 5 个科研项目列表。
        storage_has_boxes (bool): 仓库中是否有可拆解的科技箱/装备，
            影响 E 系列科研的筛选。由 StorageHandler 设置。
    """
    # Текущий список исследовательских проектов
    projects: list
    # Значение из StorageHandler
    storage_has_boxes = True

    def research_goto_detail(self, index, skip_first_screenshot=True):
        """
        点击进入指定索引的科研项目详情页。

        Args:
            index (int): 科研项目索引，0 到 4。
            skip_first_screenshot (bool): 是否跳过首次截图，复用上一状态的截图。
        """
        logger.info(f'[Исследование — детали] Вход в детали проекта (проект {index})')
        click_timer = Timer(10)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # DETAIL_NEXT появляется даже до полной загрузки страницы деталей исследования
            if not self.appear(DETAIL_NEXT, offset=(20, 20)):
                if click_timer.reached():
                    self.device.click(RESEARCH_ENTRANCE[index])
                    click_timer.reset()
            else:
                # Проверяем RESEARCH_COST_CHECKER, чтобы убедиться, что страница деталей исследования полностью загружена
                self.wait_until_appear(RESEARCH_COST_CHECKER, offset=(20, 20), skip_first_screenshot=True)
                break

    def _research_jp_detect(self, skip_first_screenshot=True):
        """
        包装 research_jp_detect()，增加错误处理。

        Args:
            skip_first_screenshot:

        Returns:
            ResearchProjectJp
        """
        timeout = Timer(2, count=6).start()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.info_bar_count():
                logger.info('[Исследование — обнаружение] Обработка информационной панели')
                timeout.reset()
                continue

            project = research_jp_detect(self.device.image)
            if project.duration == '0':
                logger.warning(f'[Исследование — обнаружение] Некорректная длительность проекта: {project}')
                continue
            else:
                return project

    @Config.when(SERVER='jp')
    def research_detect(self):
        """
        实际上此处不需要截图。'image' 是一个空参数。
        添加此参数仅是为了确保所有 "research_detect" 具有相同的参数签名。
        """
        projects = []
        proj_sorted = []

        for _ in range(5):
            self.device.click_record_clear()
            """
            每次进入第 4 个（中右侧）入口时，
            所有科研项目会从右向左移动 1 个位置。
            """
            self.research_goto_detail(3)
            """
            'image' 是上述的空参数。
            我们需要的是当前屏幕 'self.device.image'。
            """
            project = self._research_jp_detect()
            logger.attr('Исследовательский проект', project)
            projects.append(project)
            self.research_detail_quit()
        """
        page_research 应与之前保持一致。
        由于我们首先进入了第 4 个入口，
        从左到右的索引为 (2, 3, 4, 0, 1)。
        """
        for pos in range(5):
            proj_sorted.append(projects[(pos + 2) % 5])

        self.projects = proj_sorted

    @Config.when(SERVER=None)
    def research_detect(self):
        timeout = Timer(5, count=5).start()
        while 1:
            projects = research_detect(self.device.image)

            if timeout.reached():
                logger.warning('[Исследование — обнаружение] Не удалось распознать название проекта после 3 попыток; считаем его правильным')
                break

            if sum([p.valid for p in projects]) < 5:
                # Серия крайнего левого исследовательского проекта перекрыта информацией боевого пропуска, см. #1037
                logger.info('[Исследование — обнаружение] Обнаружен некорректный проект')
                logger.info('[Исследование — обнаружение] Возможная причина: информация боевого пропуска или слишком ранний снимок')
                # Редкий случай, поэтому небольшая задержка sleep допустима
                self.device.sleep(1)
                self.device.screenshot()
                continue
            else:
                break

        self.projects = projects

    def research_sort_filter(self, enforce=False):
        """
        根据用户配置的筛选规则对科研项目进行优先级排序。

        加载预设或自定义过滤器字符串，解析后应用到当前项目列表，
        输出按优先级排序的候选项目列表。

        Args:
            enforce (bool): 是否为强制模式，强制模式下会追加默认
                预设作为兜底筛选条件。

        Returns:
            list: ResearchProject 对象和预设字符串的列表，
                如 [object, object, object, 'reset']
        """
        # Загружаем строку фильтра
        preset = self.config.Research_PresetFilter
        if preset == 'custom':
            string = self.config.Research_CustomFilter
            if enforce:
                string = string + ' > ' + DICT_FILTER_PRESET[GeneratedConfig.Research_PresetFilter]
        else:
            if (self.config.Research_UseCube == 'always_use' or enforce) \
                    and f'{preset}_cube' in DICT_FILTER_PRESET:
                preset = f'{preset}_cube'
            if preset not in DICT_FILTER_PRESET:
                logger.warning(f'[Исследование — фильтр] Предустановка не найдена: {preset}; используется предустановка по умолчанию')
                preset = GeneratedConfig.Research_PresetFilter
            string = DICT_FILTER_PRESET[preset]

        logger.attr('Предустановка исследования', preset)
        logger.info('[Исследование — ресурсы] Кубы мудрости: {}, монеты: {}, детали: {}'.format(
            self.config.Research_UseCube,
            self.config.Research_UseCoin,
            self.config.Research_UsePart))
        logger.attr('Разрешить задержку', self.config.Research_AllowDelay)

        # Без учёта регистра
        string = string.lower()
        # В фильтре используется 'hakuryu', но допускаем и 'hakuryu', и 'hakuryuu'
        string = string.replace('hakuryuu', 'hakuryu')
        # Допускаем оба варианта: 'fastest' и 'shortest'
        string = string.replace('fastest', 'shortest')
        # Допускаем оба варианта: 'PR' и 'PRY'
        string = re.sub(r'pr([\d\- >])', r'pry\1', string)

        FILTER.load(string)
        priority = FILTER.apply(self.projects, func=partial(self._research_check, enforce=enforce))

        # Логирование
        logger.attr('Порядок фильтрации', ' > '.join([str(project) for project in priority]))
        return priority

    def _research_check(self, project, enforce=False):
        """
        检查单个科研项目是否符合用户的资源消耗和类型约束。

        根据用户配置检查魔方(coin)、金币(cube)、部件(part)的消耗限制，
        以及 B 系列、T 系列、E 系列的特殊过滤规则。

        Args:
            project (ResearchProject): 待检查的科研项目。
            enforce (bool): 是否为强制模式，强制模式下忽略部分资源限制。

        Returns:
            bool: 项目是否通过所有检查条件。
        """
        if not project.valid:
            return False

        # Проверяем затраты проекта
        is_05 = str(project.duration) == '0.5'
        if project.need_cube:
            if self.config.Research_UseCube == 'do_not_use':
                return False
            if self.config.Research_UseCube == 'only_no_project' and not enforce:
                return False
            if self.config.Research_UseCube == 'only_05_hour' and not is_05 and not enforce:
                return False
        if project.need_coin:
            if self.config.Research_UseCoin == 'do_not_use':
                return False
            if self.config.Research_UseCoin == 'only_no_project' and not enforce:
                return False
            if self.config.Research_UseCoin == 'only_05_hour' and not is_05 and not enforce:
                return False
        if project.need_part:
            if self.config.Research_UsePart == 'do_not_use':
                return False
            if self.config.Research_UsePart == 'only_no_project' and not enforce:
                return False
            if self.config.Research_UsePart == 'only_05_hour' and not is_05 and not enforce:
                return False

        # Причины игнорировать серии B и E-2:
        # - Нельзя гарантировать выполнение условий исследования.
        #   Можно проработать целый день и ничего не получить из-за невыполненных предварительных условий.
        # - Исследования серии B дают мало пользы.
        #   Золотой B-4 примерно эквивалентен C-12, но требует много нефти.

        if project.genre.upper() == 'B':
            return False
        # Для серии T требуются комиссии
        # 2022.05.08 исследования серии T разрешены, поскольку комиссии теперь принудительно включены
        # 2022.07.17 серия T снова запрещена: без выполненных предварительных условий проект нельзя добавить в очередь
        if project.genre.upper() == 'T':
            return self.config.Research_AllowGenreT
        # 2021.08.19 разрешено разбирать технологические ящики для E-2, но без изменений для JP-сервера
        # 2022.08.23 разрешены все E-2, поскольку теперь поддерживается разбор снаряжения
        #   Если на складе нет ящиков для разбора, игнорируем E-2,
        #   иначе возникнет цикл: запуск исследования -> попытка разбора -> отмена исследования
        if not self.storage_has_boxes:
            if self.config.SERVER == 'jp':
                if project.genre.upper() == 'E' and str(project.duration) != '6':
                    return False
            else:
                if project.genre.upper() == 'E' and project.task != '':
                    return False

        return True

    def research_sort_shortest(self, enforce):
        """
        按最短时长优先排序科研项目。

        当筛选结果为空时，优先选择时长最短的项目以快速获取科研收益。

        Args:
            enforce (bool): 是否为强制模式。

        Returns:
            list: ResearchProject 对象和预设字符串的列表，
                如 [object, object, object, 'reset']
        """
        FILTER.load(FILTER_STRING_SHORTEST)
        priority = FILTER.apply(self.projects, func=partial(self._research_check, enforce=enforce))

        logger.attr('Порядок фильтрации', ' > '.join([str(project) for project in priority]))
        return priority

    def research_sort_cheapest(self, enforce):
        """
        按最低消耗优先排序科研项目。

        当筛选结果为空时，优先选择消耗最少资源的项目以节省资源。

        Args:
            enforce (bool): 是否为强制模式。

        Returns:
            list: ResearchProject 对象和预设字符串的列表，
                如 [object, object, object, 'reset']
        """
        FILTER.load(FILTER_STRING_CHEAPEST)
        priority = FILTER.apply(self.projects, func=partial(self._research_check, enforce=enforce))

        logger.attr('Порядок фильтрации', ' > '.join([str(project) for project in priority]))
        return priority
