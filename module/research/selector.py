"""
Фильтрация исследовательских проектов.

Модуль отвечает за обнаружение списка исследовательских проектов на скриншотах,
их сортировку и фильтрацию в соответствии с настроенными пользователем правилами,
а также выдачу списка проектов-кандидатов в порядке приоритета.

Поддерживаются две стратегии распознавания по серверам:
- JP-сервер: поочередный клик по проектам для перехода на страницу деталей, сопоставление
  по шаблонам серии, жанра, затрат и корабля (так как на JP названия проектов не подлежат OCR)
- Остальные серверы: распознавание названий проектов через OCR + сопоставление номера серии по шаблону

Правила фильтрации парсят строку фильтра регулярным выражением,
поддерживают многомерную фильтрацию по серии (S1-S9), кораблю, редкости (DR/PRY),
жанру (B/C/D/E/G/H/Q/T), номеру и длительности, а также поддерживают два режима:
preset (предустановка) и custom (пользовательский).

Терминология:
    Серия (Series): номер серии исследований S1-S9
    Жанр (Genre): тип исследовательского проекта B/C/D/E/G/H/Q/T
    Чертеж (Blueprint): чертеж корабля, получаемый за исследование
    DR: решающий проект (Decisive / Dreamship Rarity), корабли радужной/золотой редкости
    PRY: приоритетный проект (Priority Rarity), корабли высшей редкости
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
                          r'(\d{3})?'
                          r'(\d.\d|\d\d?)?')
FILTER_ATTR = ('series', 'ship', 'ship_rarity', 'genre', 'number', 'duration')
FILTER_PRESET = ('shortest', 'cheapest', 'reset')
FILTER = Filter(FILTER_REGEX, FILTER_ATTR, FILTER_PRESET)


class ResearchSelector(ResearchUI):
    """
    Фильтр исследовательских проектов, отвечающий за обнаружение и фильтрацию исследований.

    Распознает по скриншоту названия, серии и статусы 5 исследовательских проектов, затем
    ранжирует их по приоритету согласно правилам фильтрации пользователя (preset или custom).

    Для JP-сервера используется стратегия поочередного перехода к деталям, для других серверов — OCR + шаблоны.

    Attributes:
        projects (list[ResearchProject]): Список 5 исследовательских проектов на текущем экране.
        storage_has_boxes (bool): Есть ли на складе разбираемые техно-ящики/снаряжение,
            влияет на фильтрацию исследований жанра E. Задается StorageHandler.
    """
    # Список текущих исследовательских проектов
    projects: list
    # Данные из StorageHandler
    storage_has_boxes = True

    def research_goto_detail(self, index, skip_first_screenshot=True):
        """
        Переходит по клику на страницу деталей проекта по указанному индексу.

        Args:
            index (int): Индекс исследовательского проекта от 0 до 4.
            skip_first_screenshot (bool): Пропускать ли первый скриншот, повторно используя предыдущий.
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
        Обертка над research_jp_detect() с добавлением обработки ошибок.

        Args:
            skip_first_screenshot: Пропускать ли первый скриншот.

        Returns:
            ResearchProjectJp: Распознанный проект для JP-сервера.
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
        На самом деле скриншот здесь не требуется; 'image' является фиктивным параметром.
        Параметр добавлен исключительно ради единообразия сигнатуры всех методов research_detect.
        """
        projects = []
        proj_sorted = []

        for _ in range(5):
            self.device.click_record_clear()
            """
            При каждом входе в 4-й (средний правый) слот
            все исследовательские проекты сдвигаются на 1 позицию справа налево.
            """
            self.research_goto_detail(3)
            """
            'image' — вышеупомянутый пустой параметр.
            Нам требуется актуальный экран 'self.device.image'.
            """
            project = self._research_jp_detect()
            logger.attr('Исследовательский проект', project)
            projects.append(project)
            self.research_detail_quit()
        """
        page_research должно оставаться согласованным с исходным состоянием.
        Поскольку первым открывался 4-й слот,
        индексы слева направо соответствуют (2, 3, 4, 0, 1).
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
                # Серия крайнего левого исследования перекрыта информацией боевого пропуска, см. #1037
                logger.info('[Исследование — обнаружение] Обнаружен некорректный проект')
                logger.info('[Исследование — обнаружение] Возможная причина: информация боевого пропуска или слишком ранний снимок')
                # Редкий случай: небольшая задержка sleep допустима
                self.device.sleep(1)
                self.device.screenshot()
                continue
            else:
                break

        self.projects = projects

    def research_sort_filter(self, enforce=False):
        """
        Ранжирует исследовательские проекты по приоритету согласно правилам фильтрации пользователя.

        Загружает строку пресета или пользовательского фильтра, разбирает ее и применяет к текущему списку проектов,
        возвращая список кандидатов, отсортированных по приоритету.

        Args:
            enforce (bool): Режим принудительного выбора; при включении добавляет пресет
                по умолчанию в качестве резервного условия фильтрации.

        Returns:
            list: Список объектов ResearchProject и служебных строк пресетов,
                например [object, object, object, 'reset']
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
        # В фильтре используется 'hakuryu', но допускаются и 'hakuryu', и 'hakuryuu'
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
        Проверяет отдельный исследовательский проект на соответствие ограничениям по ресурсам и жанру.

        Проверяет лимиты расхода кубов мудрости (cube), монет (coin), деталей (part)
        согласно настройкам пользователя, а также специальные правила для жанров B, T, E.

        Args:
            project (ResearchProject): Проверяемый исследовательский проект.
            enforce (bool): Режим принудительного выбора; игнорирует часть ресурсных ограничений.

        Returns:
            bool: Прошел ли проект все условия проверки.
        """
        if not project.valid:
            return False

        # Проверяем стоимость проекта
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

        # Причины игнорировать серию B и E-2:
        # - Нельзя гарантировать выполнение условий исследования.
        #   После целого дня работы можно не получить ничего из-за невыполненных предварительных условий.
        # - Исследования серии B дают мало пользы.
        #   Золотой B-4 почти эквивалентен C-12, но требует много нефти.

        if project.genre.upper() == 'B':
            return False
        # Для серии T нужны комиссии
        # 2022.05.08: разрешаем исследования серии T, поскольку комиссии теперь принудительно включены
        # 2022.07.17: снова запрещаем серию T, если предварительные условия не выполнены и проект нельзя поставить в очередь
        if project.genre.upper() == 'T':
            return self.config.Research_AllowGenreT
        # 2021.08.19: разрешаем E-2 для разбора технологических ящиков, но не на сервере JP
        # 2022.08.23: разрешаем все E-2; разбор снаряжения теперь поддерживается
        #   Если на складе нет ящиков для разбора, игнорируем E-2,
        #   иначе зациклимся на запуске исследования, попытке разбора и отмене исследования
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
        Сортирует исследовательские проекты по возрастанию длительности.

        Если результаты фильтрации пусты, выбирает кратчайшие по времени проекты для скорейшего получения наград.

        Args:
            enforce (bool): Режим принудительного выбора.

        Returns:
            list: Список объектов ResearchProject и служебных строк пресетов,
                например [object, object, object, 'reset']
        """
        FILTER.load(FILTER_STRING_SHORTEST)
        priority = FILTER.apply(self.projects, func=partial(self._research_check, enforce=enforce))

        logger.attr('Порядок фильтрации', ' > '.join([str(project) for project in priority]))
        return priority

    def research_sort_cheapest(self, enforce):
        """
        Сортирует исследовательские проекты по минимальной стоимости ресурсов.

        Если результаты фильтрации пусты, выбирает проекты с наименьшим расходом ресурсов для их экономии.

        Args:
            enforce (bool): Режим принудительного выбора.

        Returns:
            list: Список объектов ResearchProject и служебных строк пресетов,
                например [object, object, object, 'reset']
        """
        FILTER.load(FILTER_STRING_CHEAPEST)
        priority = FILTER.apply(self.projects, func=partial(self._research_check, enforce=enforce))

        logger.attr('Порядок фильтрации', ' > '.join([str(project) for project in priority]))
        return priority
