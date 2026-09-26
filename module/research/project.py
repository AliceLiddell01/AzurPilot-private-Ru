"""
Модель данных и распознавание исследовательских проектов.

Модуль определяет структуры данных исследовательских проектов и функции их распознавания по скриншотам.

Содержит два основных класса данных:
- ResearchProject: для серверов CN/EN/TW; распознает пакет из 5 проектов по скриншоту списка
  через OCR названий проектов + шаблонное сопоставление серий
- ResearchProjectJp: для JP-сервера; определяет серию, жанр, расход ресурсов, чертежи кораблей
  и другие сведения поочередным открытием страниц деталей и сопоставлением шаблонов (так как на JP названия проектов не подлежат OCR)

Модуль также предоставляет вспомогательные функции:
- Распознавание номера серии: анализ структуры штрихов римских цифр оператором Собеля
- Обнаружение завершенных проектов: проверка цвета индикаторов состояния (зеленый = завершено)
- Распознавание деталей на JP: сопоставление по шаблонам серии, длительности, жанра, затрат и чертежей кораблей

Терминология:
    Серия (Series): номер серии исследований S1-S9, соответствующий определенному пулу кораблей исследований
    Жанр (Genre): код типа проекта B/C/D/E/G/H/Q/T
    Чертеж (Blueprint): чертеж корабля из исследований для его усиления
    DR: решающий проект (Decisive / Dreamship Rarity), корабль радужной/золотой редкости
    PRY: приоритетный проект (Priority Rarity), корабль высшей редкости
    Моделирование судьбы (Fate Simulation): моделирование судьбы с помощью чертежей для максимально усиленных кораблей
"""
from datetime import timedelta

from scipy import signal

from module.base.decorator import cached_property
from module.base.utils import *
from module.device.method.utils import removesuffix
from module.logger import logger
from module.ocr.ocr import Duration, Ocr
from module.research.assets import *
from module.research.project_data import LIST_RESEARCH_PROJECT
from module.research.series import get_detail_series, get_research_series_3
from module.statistics.utils import *

RESEARCH_SERIES = (SERIES_1, SERIES_2, SERIES_3, SERIES_4, SERIES_5)
RESEARCH_STATUS = [STATUS_1, STATUS_2, STATUS_3, STATUS_4, STATUS_5]
OCR_RESEARCH = [OCR_RESEARCH_1, OCR_RESEARCH_2, OCR_RESEARCH_3, OCR_RESEARCH_4, OCR_RESEARCH_5]
OCR_RESEARCH = Ocr(OCR_RESEARCH, name='RESEARCH', threshold=64, alphabet='0123456789BCDEGHQTMIULRF-')
RESEARCH_DETAIL_GENRE = [DETAIL_GENRE_B, DETAIL_GENRE_C, DETAIL_GENRE_D, DETAIL_GENRE_E, DETAIL_GENRE_G,
                         DETAIL_GENRE_H_0, DETAIL_GENRE_H_1, DETAIL_GENRE_Q, DETAIL_GENRE_T]


def get_research_series_old(image, series_button=RESEARCH_SERIES):
    """
    Определяет серию исследования простым анализом цветов (устаревший алгоритм).

    Распознает римские цифры подсчетом количества белых линий.
    Заменена функцией get_research_series(), сохранена для совместимости.

    Args:
        image (np.ndarray): Скриншот страницы списка исследований.
        series_button (tuple): Определение кнопок 5 областей индикаторов серий.

    Returns:
        list[int]: Список номеров серий 5 проектов, например [1, 1, 1, 2, 3].
    """
    result = []
    # Устанавливаем 'prominence = 50' для фильтрации возможных шумов.
    # 2021.07.18: после техобслуживания 07.15 цифра IV стала меньше, чем I, II, III.
    #   Наклонная черта '/' в 'V' внутри 'IV' стала темнее из-за сглаживания.
    #   Поэтому высота снижена до 160 для лучшей детекции.
    parameters = {'height': 160, 'prominence': 50, 'width': 1}

    for button in series_button:
        im = color_similarity_2d(resize(crop(image, button.area, copy=False), (46, 25)), color=(255, 255, 255))
        peaks = [len(signal.find_peaks(row, **parameters)[0]) for row in im[5:-5]]
        upper, lower = max(peaks), min(peaks)
        # print(peaks)

        # Удаление шумов вида [2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 3, 2]
        if upper == 3 and lower == 2 and peaks.count(3) <= 2:
            upper = 2

        if upper == lower and 1 <= upper <= 3:
            series = upper
        elif upper == 3 and lower == 2:
            series = 4
        elif upper == 2 and lower == 1:
            series = 5
        else:
            series = 0
            logger.warning(f'[Исследование — серия] Неизвестная серия: кнопка={button}, верх={upper}, низ={lower}')
        result.append(series)

    return result


def _get_research_series(img):
    """
    Анализирует направление штрихов отдельного индикатора серии оператором Собеля.

    Внутренняя вспомогательная функция, вызываемая из get_research_series().
    Определяет вертикальный (0) или наклонный (1) штрих по углам градиента границ,
    сопоставляя последовательность штрихов с номером серии.

    Args:
        img (np.ndarray): Вырезанное и масштабированное изображение индикатора серии.

    Returns:
        int: Номер серии (1-6), либо 0 при невозможности распознать.
    """
    # img = rgb2luma(img)
    img = extract_white_letters(img)
    pos = img.shape[0] * 2 // 5

    img = img[pos - 4:pos + 5]
    img = cv2.GaussianBlur(img, (5, 5), 1)
    img = img[3:6]

    threshold = np.mean(img)
    edge = np.where(np.diff((img[1] > threshold).astype(np.uint8)) == 1)[0]

    grad_x = cv2.Sobel(img, cv2.CV_16S, 1, 0)[1]
    grad_y = cv2.Sobel(img, cv2.CV_16S, 0, 1)[1]

    edge = np.arctan([
        grad_y[i] / grad_x[i]
        for i in edge
    ])
    edge = tuple(
        0 if i > -.1
        else 1
        for i in edge
        if i < .1
    )

    return {
        (0,): 1,
        (0, 0): 2,
        (0, 0, 0): 3,
        (0, 1): 4,
        (1,): 5,
        (1, 0): 6
    }.get(edge, 0)


def get_research_series(image, series_button=RESEARCH_SERIES):
    """
    Распознает номер серии исследования обнаружением границ Собеля.

    Анализирует направление штрихов римских цифр в области индикатора серии
    по углам градиента границ (вертикальный "/" или наклонный "\\"), различая серии.

    Args:
        image (np.ndarray): Скриншот страницы списка исследований.
        series_button (tuple): Определение кнопок 5 областей индикаторов серий.

    Returns:
        list[int]: Список номеров серий 5 проектов, например [1, 1, 1, 2, 3].
    """
    result = []
    for button in series_button:
        # img = resize(crop(image, button.area), (46, 25))
        img = crop(image, button.area, copy=False)
        img = cv2.resize(img, (46, 25), interpolation=cv2.INTER_AREA)
        series = _get_research_series(img)
        result.append(series)
    return result


def get_research_name(image, ocr=OCR_RESEARCH):
    """
    Распознает названия 5 проектов в списке исследований с помощью OCR.

    Args:
        image (np.ndarray): Скриншот страницы списка исследований.
        ocr (Ocr): Экземпляр распознавателя OCR, по умолчанию специализированный OCR RESEARCH
            с поддержкой смешанного буквенно-цифрового распознавания (алфавит: 0123456789BCDEGHQTMIULRF-).

    Returns:
        list[str]: Список названий 5 проектов, например:
            ['D-057-UL', 'C-038-RF', 'G-185-MI', 'H-339-MI', 'Q-027-MI'].
    """
    names = ocr.ocr(image)
    if not isinstance(names, list):
        names = [names]
    return names


def get_research_finished(image):
    """
    Определяет завершенные исследовательские проекты по цвету индикатора состояния.

    Обходит индикаторы состояния 5 проектов и анализирует каналы цвета RGB:
    - Зеленый (максимум канала G) = завершено
    - Синий (максимум канала B) = выполняется
    - Прочие цвета = аномалия, пропуск

    Args:
        image (np.ndarray): Скриншот страницы списка исследований.

    Returns:
        int: Индекс завершенного проекта (0-4), либо None, если завершенных проектов нет.
    """
    for index in [2, 1, 3, 0, 4]:
        button = RESEARCH_STATUS[index]
        color = get_color(image, button.area)
        if max(color) - min(color) < 40:
            logger.warning(f'[Исследование — состояние] Неожиданный цвет: {color}')
            continue
        color_index = np.argmax(color)  # R, G, B
        if color_index == 1:
            return index  # Зеленый
        elif color_index == 2:
            continue  # Синий
        else:
            logger.warning(f'[Исследование — состояние] Неожиданный цвет: {color}')
            continue

    return None


def parse_time(string):
    """
    Разбирает строку времени в объект timedelta.

    Args:
        string (str): Строка времени в формате 'HH:MM:SS',
            например '01:00:00', '05:47:10', '17:50:51'.

    Returns:
        timedelta: Разобранный интервал времени, либо None при ошибке разбора.
    """
    result = re.search(r'(\d+):(\d+):(\d+)', string)
    if not result:
        logger.warning(f'[Исследование — время] Некорректная строка времени: {string}')
        return None
    else:
        result = [int(s) for s in result.groups()]
        return timedelta(hours=result[0], minutes=result[1], seconds=result[2])


def match_template(image, template, area, offset=30, similarity=0.85):
    """
    Выполняет сопоставление по шаблону в указанной области скриншота.

    Args:
        image (np.ndarray): Полный скриншот.
        template (np.ndarray): Изображение искомого шаблона.
        area (tuple): Область обрезки изображения (x1, y1, x2, y2).
        offset (int, tuple): Смещение расширения области поиска.
            Целое число задает симметричное смещение по вертикали, кортеж — независимые (горизонталь, вертикаль).
        similarity (float): Порог сходства (0-1), при значении ниже порога возвращается 0.0.

    Returns:
        float: Сходство сопоставления (0-1), либо 0.0 при значении ниже порога.
    """
    if isinstance(offset, tuple):
        offset = np.array((-offset[0], -offset[1], offset[0], offset[1]))
    else:
        offset = np.array((0, -offset, 0, offset))
    image = crop(image, offset + area, copy=False)
    res = cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED)
    _, sim, _, point = cv2.minMaxLoc(res)
    if sim < similarity:
        sim = 0.0
    return sim


def get_research_series_jp_old(image):
    """
    Определяет номер серии со страницы деталей на JP-сервере (устаревший алгоритм).

    В основном совпадает с get_research_series, отличаясь областью кнопки и отсутствием масштабирования.
    Заменена функцией get_research_series_jp(), сохранена для совместимости.

    Args:
        image (np.ndarray): Скриншот страницы деталей исследования.

    Returns:
        str: Обозначение серии, например "S4".
    """
    # Устанавливаем 'prominence = 50' для фильтрации возможных шумов.
    parameters = {'height': 160, 'prominence': 50, 'width': 1}

    area = SERIES_DETAIL.area
    # На сервере JP проверяется только одна область, масштабирование не требуется.
    im = color_similarity_2d(crop(image, area, copy=False), color=(255, 255, 255))
    peaks = [len(signal.find_peaks(row, **parameters)[0]) for row in im[5:-5]]
    upper, lower = max(peaks), min(peaks)
    # print(upper, lower)

    # Удаление шумов вида [2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 3, 2]
    if upper == 3 and lower == 2 and peaks.count(3) <= 2:
        upper = 2

    if upper == lower and 1 <= upper <= 3:
        series = upper
    elif upper == 3 and lower == 2:
        series = 4
    elif upper == 2 and lower == 1:
        series = 5
    else:
        series = 0
        logger.warning(f'Неизвестная серия исследования: upper={upper}, lower={lower}')

    return f'S{series}'


def get_research_series_jp(image):
    """
    Определяет номер серии со страницы деталей на JP-сервере.

    Распознает номер серии в области индикатора серии страницы деталей сопоставлением шаблонов.

    Args:
        image (np.ndarray): Скриншот страницы деталей исследования.

    Returns:
        str: Обозначение серии, например "S4".
    """
    series = get_detail_series(image)
    return f'S{series}'


def get_research_duration_jp(image):
    """
    Распознает длительность исследования со страницы деталей на JP-сервере через OCR.

    Args:
        image (np.ndarray): Скриншот страницы деталей исследования.

    Returns:
        int: Длительность исследования в секундах.
    """
    ocr = Duration(DURATION_DETAIL)
    duration = ocr.ocr(image).total_seconds()
    return duration


def get_research_genre_jp(image):
    """
    Определяет жанр исследования со страницы деталей на JP-сервере по шаблонам.

    Перебирает шаблоны всех жанров (B/C/D/E/G/H/Q/T) и выбирает вариант с наибольшим сходством.

    Args:
        image (np.ndarray): Скриншот страницы деталей исследования.

    Returns:
        str: Код жанра, например 'd', 'c', 'g'. При неудаче возвращает пустую строку.
    """
    genre = ''
    for button in RESEARCH_DETAIL_GENRE:
        if button.match(image, offset=(30, 20), similarity=0.9):
            # DETAIL_GENRE_H_0.name.split("_")[2] == 'H'
            genre = button.name.split("_")[2]
            break
    if not genre:
        logger.warning(f'Не удалось распознать тип исследования!')
    return genre


def get_research_cost_jp(image):
    """
    Определяет затраты ресурсов со страницы деталей на JP-сервере по шаблонам.

    Проверяет наличие иконок затрат монет, кубов и деталей на странице деталей.
    При одном типе затрат размер шаблона составляет 78x78, при двух — 77x77,
    поэтому порог сходства установлен на уровне 0.8 для устойчивости к погрешностям.

    Args:
        image (np.ndarray): Скриншот страницы деталей исследования.

    Returns:
        dict: Словарь затрат с ключами 'need_coin', 'need_cube', 'need_part' и булевыми флагами.
    """
    size_template = (78, 78)
    area_template = (0, 0, 78, 57)
    folder = './assets/stats_basic'
    templates = load_folder(folder)
    costs = {'coin': False, 'cube': False, 'plate': False}
    for name, template in templates.items():
        template = load_image(template)
        template = crop(resize(template, size_template), area_template, copy=False)
        sim = match_template(image=image,
                             template=template,
                             area=DETAIL_COST.area,
                             offset=(10, 10),
                             similarity=0.8)
        if not sim:
            continue
        for cost in costs:
            if re.compile(cost).match(name.lower()):
                costs[cost] = True
                continue

    # Переименование ключей в соответствии со свойствами ResearchProjectJp
    costs['need_coin'] = costs.pop('coin')
    costs['need_cube'] = costs.pop('cube')
    costs['need_part'] = costs.pop('plate')
    return costs


def get_research_ship_jp(image):
    """
    Определяет чертеж корабля со страницы деталей на JP-сервере по шаблонам.

    Находит в библиотеке шаблонов чертежей наиболее подходящий корабль для области чертежей на странице деталей.
    Для жанра D длительностью 2.5/5/8 часов отображаются 4 предмета, а для 0.5 часа — 3,
    поэтому кнопка DETAIL_BLUEPRINT не должна покрывать только первый предмет.

    Args:
        image (np.ndarray): Скриншот страницы деталей исследования.

    Returns:
        str: Название корабля, например 'azuma', 'drake'. При неудаче возвращает пустую строку.
    """
    folder = './assets/research_blueprint'
    templates = load_folder(folder)
    similarity = 0.0
    ship = ''
    for name, template in templates.items():
        sim = match_template(image=image,
                             template=load_image(template),
                             area=DETAIL_BLUEPRINT.area,
                             offset=(10, 10),
                             similarity=0.9)
        if sim > similarity:
            similarity = sim
            ship = name
    if ship == '':
        logger.warning(f'Не удалось распознать корабль')
    return ship


def research_jp_detect(image):
    """
    Полностью распознает исследовательский проект со страницы деталей на JP-сервере.

    Комбинирует вызовы распознавания серии, длительности, жанра, затрат и корабля,
    формируя готовый объект ResearchProjectJp.

    Args:
        image (np.ndarray): Скриншот страницы деталей исследования.

    Returns:
        ResearchProjectJp: Распознанный объект исследовательского проекта.
    """
    project = ResearchProjectJp()
    project.series = get_research_series_jp(image)
    project.duration = removesuffix(str(get_research_duration_jp(image) / 3600), '.0')
    if project.duration == '':
        project.duration = '0'
    project.genre = get_research_genre_jp(image)
    costs = get_research_cost_jp(image)
    for cost in costs:
        project.__setattr__(cost, costs[cost])
    if project.genre.lower() == 'd':
        project.ship = get_research_ship_jp(image).lower()
    if project.ship:
        project.ship_rarity = 'dr' if project.ship in project.DR_SHIP else 'pry'
    project.name = f'{project.series}-{project.genre}-{project.duration}{project.ship}'
    if not project.check_valid():
        logger.warning(f'[Исследование — проект] Некорректный проект {project}')
    return project


def research_detect(image):
    """
    Пакетно распознает 5 исследовательских проектов по скриншоту списка исследований.

    Распознает названия проектов через OCR и номера серий сопоставлением шаблонов,
    формируя список объектов ResearchProject.

    Args:
        image (np.ndarray): Скриншот страницы списка исследований.

    Returns:
        list[ResearchProject]: Список 5 объектов исследовательских проектов.
    """
    projects = []
    for name, series in zip(get_research_name(image), get_research_series_3(image)):
        project = ResearchProject(name=name, series=series)
        logger.attr('Исследовательский проект', project)
        projects.append(project)
    return projects


class ResearchProject:
    """
    Модель данных исследовательского проекта для серверов CN/EN/TW.

    Находит информацию о проекте в базе данных по названию (например 'D-057-UL') и номеру серии (например 3),
    определяя жанр, номер, длительность, необходимые ресурсы и целевой корабль.

    OCR может ошибаться при распознавании, поэтому конструктор содержит обширную логику корректировки названий,
    например: 'G-185-MI' -> 'C-185-MI', 'D-022-ML' -> 'D-022-MI' и т. д.

    Attributes:
        valid (bool): Действителен ли проект (найден ли в базе данных).
        raw_series (int): Исходный номер серии (1-9).
        series (str): Форматированное обозначение серии, например 'S3'.
        name (str): Скорректированное название проекта, например 'D-057-UL'.
        genre (str): Код жанра проекта, например 'D', 'C', 'G'.
        number (str): Номер проекта, например '057'.
        duration (str): Длительность проекта (в часах), например '0.5', '2', '8'.
        ship (str): Название целевого корабля, например 'azuma', 'drake'.
            Для проектов вне жанра D обычно пустая строка.
        ship_rarity (str): Редкость корабля, 'dr' или 'pry'.
            Заполняется только при наличии чертежей у проектов жанра D.
        need_coin (bool): Расходуются ли монеты.
        need_cube (bool): Расходуются ли кубы мудрости.
        need_part (bool): Расходуются ли детали.
        task (str): Описание особых требований проекта, например 'Scrap 8 pieces of gear.'.
        equipment_amount (int): Требуемое количество разбираемого снаряжения (жанр E), 0 если требований нет.
        commission_amount (int): Требуемое количество завершаемых заказов (жанр T), 0 если требований нет.
    """
    REGEX_SHIP = re.compile(
        '('
        'neptune|monarch|ibuki|izumo|roon|saintlouis'
        '|seattle|georgia|kitakaze|azuma|friedrich'
        '|gascogne|champagne|cheshire|drake|mainz|odin'
        '|anchorage|hakuryu|agir|august|marcopolo'
        '|plymouth|rupprecht|harbin|chkalov|brest'
        '|kearsarge|hindenburg|shimanto|schultz|flandre'
        '|napoli|nakhimov|halford|bayard|daisen'
        '|goudenleeuw|mecklenburg|dmitri|kansas|vittorio'
        '|valparaiso|maximmelmann|duncan|takahashi|orage'
        ')')
    REGEX_INPUT = re.compile('(coin|cube|part)')
    REGEX_DR_SHIP = re.compile(
        'azuma|friedrich'
        '|drake'
        '|hakuryu|agir'
        '|plymouth|brest'
        '|kearsarge|hindenburg'
        '|napoli|nakhimov'
        '|goudenleeuw|mecklenburg'
        '|valparaiso|maximmelmann'
    )
    # Generate with:
    """
    out = []
    for row in LIST_RESEARCH_PROJECT:
        name = row['name']
        if name.startswith('D'):
            number = name.split('-')[1]
            out.append(number)
    print(out)
    """
    C_PROJECT_NUMBERS = ['153', '185', '038']
    D_PROJECT_NUMBERS = [
        '718', '731', '744', '759', '774', '792', '318', '331', '344', '359', '374', '392', '705', '712', '746', '757',
        '779', '794', '305', '312', '346', '357', '379', '394', '721', '722', '772', '777', '795', '321', '322', '372',
        '377', '395', '708', '763', '775', '782', '768', '308', '363', '375', '382', '368', '719', '778', '786', '788',
        '793', '319', '378', '386', '388', '393', '783', '713', '739', '771', '796', '383', '313', '339', '371', '396',
        '703', '758', '766', '790', '797', '303', '358', '366', '390', '397', '780', '736', '787', '711', '764', '380',
        '336', '387', '311', '364', '737', '781', '732', '740', '747', '337', '381', '332', '340', '347', '418', '431',
        '444', '459', '474', '492', '018', '031', '044', '059', '074', '092', '405', '412', '446', '457', '479', '494',
        '005', '012', '046', '057', '079', '094', '421', '422', '472', '477', '495', '021', '022', '072', '077', '095',
        '408', '463', '475', '482', '468', '008', '063', '075', '082', '068', '419', '478', '486', '488', '493', '019',
        '078', '086', '088', '093', '483', '413', '439', '471', '496', '083', '013', '039', '071', '096', '403', '458',
        '466', '490', '497', '003', '058', '066', '090', '097', '480', '436', '487', '411', '464', '080', '036', '087',
        '011', '064', '437', '481', '432', '440', '447', '037', '081', '032', '040', '047']

    def __init__(self, name, series):
        """
        Args:
            name (str): Название проекта, например 'D-057-UL'.
            series (int): Номер серии, например 1, 2, 3.
        """
        self.valid = True
        # '4'
        self.raw_series = series
        # 'S4'
        self.series = f'S{series}'
        # 'D-057-UL'
        self.name = self.check_name(name)
        if self.name != name:
            logger.info(f'[Исследование — название] Название проекта {name} исправлено на {self.name}')
        # 'D'
        self.genre = ''
        # '057'
        self.number = ''
        # '0.5'
        self.duration = '24'
        # Аватар корабля, например 'Azuma'
        self.ship = ''
        # 'dr' или 'pry'
        self.ship_rarity = ''
        self.need_coin = False
        self.need_cube = False
        self.need_part = False
        # Требование проекта, например 'Scrap 8 pieces of gear.'
        self.task = ''

        matched = False
        for data in self.get_data(name=self.name, series=series):
            matched = True
            self.data = data
            self.genre = data['name'][0]
            self.number = data['name'][2:5]
            self.duration = str(data['time'] / 3600).rstrip('.0')
            self.task = data['task']
            for item in data['input']:
                item_name = item['name'].replace(' ', '').lower()
                result = re.search(ResearchProject.REGEX_INPUT, item_name)
                if result:
                    self.__setattr__(f'need_{result.group(1)}', True)
            for item in data['output']:
                item_name = item['name'].replace(' ', '').lower()
                result = re.search(ResearchProject.REGEX_SHIP, item_name)
                if not self.ship:
                    self.ship = result.group(1) if result else ''
                if self.ship:
                    self.ship_rarity = 'dr' if re.search(ResearchProject.REGEX_DR_SHIP, self.ship) else 'pry'
            break

        if not matched:
            logger.warning(f'[Исследование — проект] Некорректный проект {self}')
            self.valid = False

    def __str__(self):
        if self.valid:
            return f'{self.series} {self.name}'
        else:
            return f'{self.series} {self.name} (Invalid)'

    def __eq__(self, other):
        return str(self) == str(other)

    def check_name(self, name):
        """
        Исправляет типичные ошибки OCR в названиях проектов.

        Обрабатывает различные случаи ошибочного распознавания OCR: путаницу префиксов (G/D/C/L),
        ошибки в цифрах (D->0, O->0, S->5), исправление суффиксов (ML->MI, 0C->UL),
        а также известные серверные ошибки.

        Args:
            name (str): Исходное название проекта, распознанное OCR.

        Returns:
            str: Скорректированное название проекта, например 'D-057-UL'.
        """
        name = name.strip('-')
        # G-185-MI, D-T85-MI -> C-185-MI
        name = name.replace('G-185', 'C-185').replace('D-T85', 'C-185')
        # E-316-MI -> E-315-MI
        if name == '316-MI':
            name = 'E-315-MI'

        parts = name.split('-')
        parts = [i for i in parts if i]
        if len(parts) == 3:
            prefix, number, suffix = parts

            number = number.replace('D', '0').replace('O', '0').replace('S', '5')
            # E-316-MI -> E-315-MI
            number = number.replace('316', '315')
            # [TW] S5 D-349-MI -> S5 D-319-MI
            if prefix == 'D' and number == '349' and self.raw_series == 5:
                number = '319'

            if prefix in ['I1', 'U']:
                prefix = 'D'
            prefix = prefix.strip('I1')
            # LC-038-RF -> C-038-RF
            prefix = prefix.replace('LC', 'C')

            # S3 D-022-MI (S3-Drake-0.5) распознается как 'D-022-ML' из-за белой одежды Drake
            suffix = suffix.replace('ML', 'MI').replace('MIL', 'MI').replace('M1', 'MI')
            # S4 D-063-UL (S4-hakuryu-0.5) распознается как 'D-063-0C'
            # D-057-DC -> D-057-UL
            suffix = suffix.replace('0C', 'UL').replace('UC', 'UL')
            suffix = suffix.replace('DC5', 'UL').replace('DC3', 'UL').replace('DC', 'UL')
            # D-075-UL1 -> D-075-UL
            suffix = suffix.replace('UL1', 'UL').replace('ULI', 'UL').replace('UL5', 'UL')

            if suffix == 'U':
                suffix = 'UL'
            # Ошибка OCR на сервере TW: замена B на D
            if prefix == 'B' and number in ResearchProject.D_PROJECT_NUMBERS:
                # Сохраняем B-397-RF: S7 D-397-MI и S* B-397-RF делят номер 397
                if number == '397' and suffix == 'RF':
                    pass
                else:
                    prefix = 'D'
            # I-483-RF корректируется в -483-RF -> D-483-RF
            if prefix == '' and number in ResearchProject.D_PROJECT_NUMBERS:
                prefix = 'D'
            # L-153-MI -> C-153-MI
            if prefix == 'L' and number in ResearchProject.C_PROJECT_NUMBERS:
                prefix = 'C'
            return '-'.join([prefix, number, suffix])
        elif len(parts) == 2:
            # Пробуем вставить '-', обрабатывая результаты вроде H339-MI
            if name[0].isalpha() and name[1].isdigit():
                return self.check_name(f'{name[0]}-{name[1:]}')
        return name

    def get_data(self, name, series):
        """
        Запрашивает подходящие данные исследовательского проекта из базы данных.

        Поочередно проверяет точное совпадение, исправление префиксов (путаница G/C/D),
        нечеткое сопоставление суффиксов и другие стратегии для компенсации ошибок OCR.

        Args:
            name (str): Скорректированное название проекта, например 'D-057-UL'.
            series (int): Номер серии, например 1, 2, 3.

        Yields:
            dict: Словарь с данными найденного проекта, содержащий поля name, series, time,
                task, input, output и др.
        """
        for data in LIST_RESEARCH_PROJECT:
            if (data['series'] == series) and (data['name'] == name):
                yield data

        if len(name) and name[0].isdigit():
            for t in 'QGE':
                name1 = f'{t}-{self.name}'
                logger.info(f'[Исследование — сопоставление] Проверка наиболее похожего кандидата {name1}')
                for data in LIST_RESEARCH_PROJECT:
                    if (data['series'] == series) and (data['name'] == name1):
                        self.name = name1
                        yield data

        if name.startswith('D'):
            # Буква 'C' может распознаваться как 'D' из-за блика на карточке проекта
            name1 = 'C' + self.name[1:]
            for data in LIST_RESEARCH_PROJECT:
                if (data['series'] == series) and (data['name'] == name1):
                    self.name = name1
                    yield data

        for data in LIST_RESEARCH_PROJECT:
            if (data['series'] == series) and (data['name'].rstrip('MIRFUL-') == name.rstrip('MIRFUL-')):
                yield data

        return False

    @cached_property
    def equipment_amount(self):
        # Разобрать 8 ед. снаряжения.
        # Разобрать 15 ед. снаряжения.
        if '8 piece' in self.task:
            return 8
        elif '15 piece' in self.task:
            return 15
        else:
            return 0

    @cached_property
    def commission_amount(self):
        if '2 commissions' in self.task:
            return 2
        elif '4 commissions' in self.task:
            return 4
        elif '6 commissions' in self.task:
            return 6
        else:
            return 0


class ResearchProjectJp:
    """
    Модель данных исследовательского проекта для JP-сервера.

    Названия проектов на JP-сервере не подлежат OCR-распознаванию, поэтому используется сопоставление
    шаблонов для поочередного определения серии, жанра, затрат и чертежей кораблей со страницы деталей.
    Название формируется из результатов распознавания в виде '{series}-{genre}-{duration}{ship}'.

    Attributes:
        valid (bool): Действителен ли проект (проверен через check_valid()).
        name (str): Сгенерированный идентификатор проекта, например 'S4-D-0.5azuma'.
        series (str): Форматированное обозначение серии, например 'S4'.
        genre (str): Код жанра проекта, например 'd', 'c', 'g'.
        number (str): Номер проекта, на JP-сервере обычно пустая строка.
        duration (str): Длительность проекта (в часах), например '0.5', '2', '8'.
        ship (str): Название получаемого корабля, например 'azuma'.
        ship_rarity (str): Редкость корабля, 'dr' или 'pry'.
        need_coin (bool): Расходуются ли монеты.
        need_cube (bool): Расходуются ли кубы мудрости.
        need_part (bool): Расходуются ли детали.
        task (str): Особые требования проекта, на JP-сервере обычно пустая строка.
        equipment_amount (int): Требуемое количество разбираемого снаряжения (жанр E).
        commission_amount (int): Требуемое количество завершаемых заказов (жанр T).

    Атрибуты класса:
        GENRE (list[str]): Все допустимые коды жанров проектов.
        DURATION (list[str]): Все допустимые длительности проектов.
        SHIP_S1 ~ SHIP_S9 (list[str]): Списки кораблей для каждой серии.
        SHIP_ALL (list[str]): Объединенный список кораблей всех серий.
        DR_SHIP (list[str]): Все корабли редкости DR (решающие проекты).
    """
    GENRE = ['b', 'c', 'd', 'e', 'g', 'h', 'q', 't']
    DURATION = ['0.5', '1', '1.5', '2', '2.5', '3', '4', '5', '6', '8', '12']
    SHIP_S1 = ['neptune', 'monarch', 'ibuki', 'izumo', 'roon', 'saintlouis']
    SHIP_S2 = ['seattle', 'georgia', 'kitakaze', 'azuma', 'friedrich', 'gascogne']
    SHIP_S3 = ['champagne', 'cheshire', 'drake', 'mainz', 'odin']
    SHIP_S4 = ['anchorage', 'hakuryu', 'agir', 'august', 'marcopolo']
    SHIP_S5 = ['plymouth', 'rupprecht', 'harbin', 'chkalov', 'brest']
    SHIP_S6 = ['kearsarge', 'hindenburg', 'shimanto', 'schultz', 'flandre']
    SHIP_S7 = ['napoli', 'nakhimov', 'halford', 'bayard', 'daisen']
    SHIP_S8 = ['goudenleeuw', 'mecklenburg', 'dmitri', 'kansas', 'vittorio']
    SHIP_S9 = ['valparaiso', 'maximmelmann', 'duncan', 'takahashi', 'orage']
    SHIP_ALL = SHIP_S1 + SHIP_S2 + SHIP_S3 + SHIP_S4 + SHIP_S5 + SHIP_S6 + SHIP_S7 + SHIP_S8 + SHIP_S9
    DR_SHIP = [
        'azuma', 'friedrich',
        'drake',
        'hakuryu', 'agir',
        'plymouth', 'brest',
        'kearsarge', 'hindenburg',
        'napoli', 'nakhimov',
        'goudenleeuw', 'mecklenburg',
        'valparaiso', 'maximmelmann',
    ]

    def __init__(self):
        self.valid = True
        self.name = ''
        self.series = ''
        self.genre = ''
        self.number = ''
        self.duration = '24'
        self.ship = ''
        self.ship_rarity = ''
        self.need_coin = False
        self.need_cube = False
        self.need_part = False
        self.task = ''

    def check_valid(self):
        """
        Проверяет валидность исследовательского проекта для JP-сервера.

        Проверяет допустимость диапазонов серии, жанра и длительности,
        а также распознан ли чертеж корабля для проектов жанра D.

        Returns:
            bool: Действителен ли проект.
        """
        self.valid = False
        if self.series.lower() == "s0":
            return False
        if self.genre.lower() not in self.GENRE:
            return False
        if self.duration not in self.DURATION:
            return False
        if self.ship not in self.SHIP_ALL:
            self.ship = ''
        if self.genre.lower() == 'd' and not self.ship:
            return False
        self.valid = True
        return True

    def __str__(self):
        if self.valid:
            return f'{self.name}'
        else:
            return f'{self.name} (Invalid)'

    def __eq__(self, other):
        return str(self) == str(other)

    @cached_property
    def equipment_amount(self):
        if self.genre == 'E' and self.duration == '2':
            # На сервере JP нет названий исследований: невозможно различить E-031-MI и E-315-MI,
            # возвращаем максимальное значение 15
            return 15
        else:
            return 0

    @cached_property
    def commission_amount(self):
        if self.genre == 'T':
            if self.duration == '3':
                return 2
            elif self.duration == '4':
                return 4
            elif self.duration == '6':
                return 6
        return 0
