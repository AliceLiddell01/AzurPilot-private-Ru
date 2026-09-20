"""岛屿矿山与林场模块。

管理矿山和林场的自动化资源采集，包括铜、铝、铁、硫、银等矿产及林木资源。
配置工人筛选与库存管理，支持登录重连与仓库 OCR 数量检测。
"""
from module.island.island import *
from module.island_mine_forest.assets import *
from module.ui.page import *
from module.handler.login import LoginHandler
from module.config.utils import *
from module.island.warehouse import *
from datetime import timedelta
from module.config.time_source import now as current_time


class IslandMineForest(Island,LoginHandler):
    def __init__(self, *args, **kwargs):
        Island.__init__(self, *args, **kwargs)
        self.worker_filters = {
            'mine': self.config.IslandMine_WorkerFilter,
            'forest': self.config.IslandForest_WorkerFilter,
        }

        # Конфигурация склада шахты и лесопилки (по образцу INVENTORY_CONFIG в модуле фермы)
        self.inventory_config = {
            'mine': {
                'filter': 'mine',
                'items': [
                    {'name': 'Copper', 'template': TEMPLATE_COPPER, 'selection': SELECT_COPPER, 'selection_check': SELECT_COPPER_CHECK},
                    {'name': 'Aluminium', 'template': TEMPLATE_ALUMINIUM, 'selection': SELECT_ALUMINIUM, 'selection_check': SELECT_ALUMINIUM_CHECK},
                    {'name': 'Iron', 'template': TEMPLATE_IRON, 'selection': SELECT_IRON, 'selection_check': SELECT_IRON_CHECK},
                    {'name': 'Sulphur', 'template': TEMPLATE_SULPHUR, 'selection': SELECT_SULPHUR, 'selection_check': SELECT_SULPHUR_CHECK},
                    {'name': 'Silver', 'template': TEMPLATE_SILVER, 'selection': SELECT_SILVER, 'selection_check': SELECT_SILVER_CHECK},
                ]
            },
            'forest': {
                'filter': 'forest',
                'items': [
                    {'name': 'Elegant', 'template': TEMPLATE_ELEGANT, 'selection': SELECT_ELEGANT, 'selection_check': SELECT_ELEGANT_CHECK},
                    {'name': 'Practical', 'template': TEMPLATE_PRACTICAL, 'selection': SELECT_PRACTICAL, 'selection_check': SELECT_PRACTICAL_CHECK},
                    {'name': 'Selected', 'template': TEMPLATE_SELECTED, 'selection': SELECT_SELECTED, 'selection_check': SELECT_SELECTED_CHECK},
                ]
            }
        }

    # Конфигурация продукции (выбор роли/продукта/детекция, по образцу name_to_config фермы)
    PRODUCT_CONFIGS = {
        "Copper": (SELECT_COPPER, SELECT_COPPER_CHECK, POST_COPPER),
        "Aluminium": (SELECT_ALUMINIUM, SELECT_ALUMINIUM_CHECK, POST_ALUMINIUM),
        "Iron": (SELECT_IRON, SELECT_IRON_CHECK, POST_IRON),
        "Sulphur": (SELECT_SULPHUR, SELECT_SULPHUR_CHECK, POST_SULPHUR),
        "Silver": (SELECT_SILVER, SELECT_SILVER_CHECK, POST_SILVER),
        "Elegant": (SELECT_ELEGANT, SELECT_ELEGANT_CHECK, POST_ELEGANT),
        "Practical": (SELECT_PRACTICAL, SELECT_PRACTICAL_CHECK, POST_PRACTICAL),
        "Selected": (SELECT_SELECTED, SELECT_SELECTED_CHECK, POST_SELECTED),
    }

    # По 4 ед. за смену
    UNITS_PER_RUN = 4
    # Максимальное число циклов (обычная древесина: 9, медная руда: 12, железная руда: 8, остальные: 5)
    PRODUCT_MAX_RUNS = {
        # Шахта
        'Copper': 12, 'Aluminium': 5, 'Iron': 8,
        'Sulphur': 5, 'Silver': 5,
        # Лесопилка
        'Elegant': 5, 'Practical': 9, 'Selected': 5,
    }


    # Область распознавания текста склада: базовые координаты (300,258,408,289), смещение вправо 142px
    # диапазон y 258~289 для захвата текста (оптимальная область y=261~283)
    OCR_TEXT_BASE = (300, 258, 408, 289)
    OCR_TEXT_DELTA = 142
    # Цифровая область: относительное смещение сетки WarehouseOCR (45, 90, 99, 110)
    # Начало сетки (301,150), шаг колонок 142px
    WAREHOUSE_GRID_ORIGIN = (301, 150)
    WAREHOUSE_GRID_DELTA = (142, 167)
    NUMBER_REL = (45, 90, 99, 110)

    def _get_threshold(self, product_name):
        """获取指定产品的最低库存阈值"""
        mine_products = ['Copper', 'Aluminium', 'Iron', 'Sulphur', 'Silver']
        forest_products = ['Elegant', 'Practical', 'Selected']
        if product_name in mine_products:
            return getattr(self.config, f'IslandMine_Min{product_name}', 0)
        elif product_name in forest_products:
            return getattr(self.config, f'IslandForest_Min{product_name}', 0)
        return 0

    @staticmethod
    def _post_available_for_dispatch(post_info):
        """只有检测后处于空闲状态的岗位才可在本轮派遣。"""
        return post_info.get('state') == 'idle'

    # ==================== Детекция склада (OCR названий предметов, считывание количества Digit) ====================
    # Маппинг распознаваемых названий предметов (шахта + лесопилка)
    # Распознавание OCR может быть неполным, для каждого предмета задано несколько вариантов
    ITEM_CN_NAMES = {
        'Copper': ['铜矿'],
        'Aluminium': ['铝矿'],
        'Iron': ['铁矿'],
        'Sulphur': ['硫磺', '硫矿'],
        'Silver': ['银矿'],
        'Practical': ['实用之木', '实用之本'],
        'Selected': ['精选之木', '精选之本'],
        'Elegant': ['典雅之木', '典雅之本'],
    }

    def warehouse_inventory(self, category):
        """
        获取仓库库存。
        扫描仓库网格所有 6 个位置：
        1. OCR 读取每个位置的中文文字确定物品
        2. 匹配到后读取对应图标右下角的数字
        3. 仓库内物品顺序不固定，缺省项数量记为 0
        """
        config = self.inventory_config[category]
        self.warehouse_filter(config['filter'])
        image = self.device.screenshot()

        # Инициализация количества всех предметов нулями
        results = {item['name']: 0 for item in config['items']}

        # Сканирование всех 6 позиций сетки (порядок предметов не фиксирован)
        for idx in range(6):
            # Область распознавания текста
            tx1 = self.OCR_TEXT_BASE[0] + idx * self.OCR_TEXT_DELTA
            ty1 = self.OCR_TEXT_BASE[1]
            tx2 = self.OCR_TEXT_BASE[2] + idx * self.OCR_TEXT_DELTA
            ty2 = self.OCR_TEXT_BASE[3]
            text_area = (tx1, ty1, tx2, ty2)
            text_btn = Button(area=text_area, color=(), button=text_area, name=f'TEXT_POS{idx}')
            ocr = Ocr(text_btn, lang='azur_lane')
            text = ocr.ocr(image)

            if not text:
                # Пустой слот, пропуск
                continue

            # Сопоставление распознанного текста с известными предметами
            matched_name = None
            for name, cn_names in self.ITEM_CN_NAMES.items():
                for cn_name in cn_names:
                    if cn_name in text:
                        matched_name = name
                        break
                if matched_name:
                    break

            if matched_name is None:
                logger.info(f"[Остров — шахта и лес] pos{idx}: распознано '{text}' → соответствие известному предмету не найдено; пропуск")
                continue

            # Учитываем только предметы текущей категории
            if matched_name not in results:
                continue

            # Считывание цифровой области
            cx = self.WAREHOUSE_GRID_ORIGIN[0] + idx * self.WAREHOUSE_GRID_DELTA[0]
            cy = self.WAREHOUSE_GRID_ORIGIN[1]  # row 0
            nx1 = cx + self.NUMBER_REL[0]
            ny1 = cy + self.NUMBER_REL[1]
            nx2 = cx + self.NUMBER_REL[2]
            ny2 = cy + self.NUMBER_REL[3]
            num_area = (nx1, ny1, nx2, ny2)
            num_btn = Button(area=num_area, color=(), button=num_area, name=f'NUM_{matched_name}')
            digit = Digit(num_btn, letter=(255, 255, 255), threshold=200, alphabet='0123456789')
            count = digit.ocr(image)
            results[matched_name] = count if count else 0
            logger.info(f"  pos{idx}: {matched_name}({text.strip()}) = {results[matched_name]}")

        return results

    def check_inventory_and_prepare_lists(self):
        """
        检查仓库库存 + 生产中数量，返回需要生产的产物及缺口数量。
        返回 {'mine': [need1, ...], 'forest': [need1, ...]}
        同时设置 self.needs_count = {(category, product): count_needed}
        """
        needs = {'mine': [], 'forest': []}
        self.needs_count = {}

        for category in ['mine', 'forest']:
            inventory = self.warehouse_inventory(category)
            for item in self.inventory_config[category]['items']:
                name = item['name']
                warehouse_count = inventory.get(name, 0)

                # Прибавляем объем в производстве (занятые посты × 4 ед.)
                in_production = 0
                post_ids = self.mine_post_ids if category == 'mine' else self.forest_post_ids
                for pid in post_ids:
                    if self.posts.get(pid, {}).get('crop') == name:
                        runs = self.posts.get(pid, {}).get('runs', 0)
                        in_production += runs * self.UNITS_PER_RUN

                effective_count = warehouse_count + in_production
                threshold = self._get_threshold(name)
                if effective_count < threshold:
                    needs[category].append(name)
                    need_count = threshold - effective_count
                    self.needs_count[(category, name)] = need_count
                    logger.info(f"[Остров — шахта и лес] {name}: склад {warehouse_count} + в производстве {in_production} = {effective_count} < {threshold} → дефицит {need_count}")
                else:
                    logger.info(f"[Остров — шахта и лес] {name}: склад {warehouse_count} + в производстве {in_production} = {effective_count} ≥ {threshold} → дефицита нет")

        logger.info(f"[Остров — шахта и лес] Требуется произвести: {needs}")
        return needs

    # ==================== Детекция постов (по образцу decided_lists фермы) ====================
    def post_plant_check(self, category):
        """检测当前打开的岗位正在生产什么产物"""
        for item in self.inventory_config[category]['items']:
            name = item['name']
            if name in self.PRODUCT_CONFIGS:
                _, _, post_check = self.PRODUCT_CONFIGS[name]
                if post_check is not None and self.appear(post_check):
                    return name
        return None

    def _record_working_post(self, post_id, category, time_var_name):
        """记录当前岗位的生产状态、剩余次数和完成时间。"""
        product_name = self.post_plant_check(category)
        if product_name:
            self.posts[post_id]['crop'] = product_name
        else:
            self.posts[post_id]['crop'] = None
        self.posts[post_id]['state'] = 'working'

        ocr_post_number = Digit(OCR_POST_NUMBER, letter=(57, 58, 60), threshold=100,
                                alphabet='0123456789')
        number = ocr_post_number.ocr(self.device.image)
        self.posts[post_id]['runs'] = number if number else 0
        logger.info(f"[Остров — шахта и лес] {post_id}: производится {product_name or 'неизвестный продукт'}, осталось запусков: {self.posts[post_id]['runs']}")

        time_work = Duration(ISLAND_WORKING_TIME)
        time_value = time_work.ocr(self.device.image)
        finish_time = current_time() + time_value if time_value is not None else None
        setattr(self, time_var_name, finish_time)

    def collect_and_detect_post(self, post_button, post_id, category, time_var_name):
        """
        打开岗位，收获已完成产物，并检测当前状态。
        一次打开完成收获+检测，避免重复开岗。
        对工作中岗位，读取生产次数用于后续计算防止多产。
        """
        collected = False
        self.post_close()
        self.post_open(post_button)
        self.device.screenshot()
        was_complete = self.appear(ISLAND_WORK_COMPLETE, offset=1)

        if was_complete or self.appear(POST_GET, offset=(50, 0)):
            # Завершено или продукцию можно забрать → сначала собираем, затем перепроверяем статус
            self.post_get_stay()
            collected = True
            self.device.screenshot()
            if self.appear(ISLAND_POST_SELECT, offset=1):
                self.posts[post_id]['crop'] = None
                self.posts[post_id]['runs'] = 0
                self.posts[post_id]['state'] = 'idle'
                setattr(self, time_var_name, None)
                logger.info(f"[Остров — шахта и лес] {post_id}: сбор завершён, позиция свободна")
            elif self.appear(ISLAND_WORKING):
                self._record_working_post(post_id, category, time_var_name)
            else:
                if was_complete:
                    self.posts[post_id]['crop'] = None
                    self.posts[post_id]['runs'] = 0
                    self.posts[post_id]['state'] = 'idle'
                    setattr(self, time_var_name, None)
                    logger.warning(f"[Остров — шахта и лес] {post_id}: состояние после сбора не распознано; по предыдущему завершённому состоянию позиция считается свободной")
                else:
                    self.posts[post_id]['crop'] = 'unknown'
                    self.posts[post_id]['runs'] = 0
                    self.posts[post_id]['state'] = 'working'
                    logger.warning(f"[Остров — шахта и лес] {post_id}: состояние позиции не распознано; считаем её занятой")

        elif self.appear(ISLAND_WORKING):
            # В работе → детекция продукта
            self._record_working_post(post_id, category, time_var_name)

        elif self.appear(ISLAND_POST_SELECT, offset=1):
            # Свободно
            self.posts[post_id]['crop'] = None
            self.posts[post_id]['runs'] = 0
            self.posts[post_id]['state'] = 'idle'
            setattr(self, time_var_name, None)
            logger.info(f"[Остров — шахта и лес] {post_id}: позиция свободна")

        self.post_close()
        return collected

    # ==================== Настройка производства (по образцу post_plant фермы без выбора семян) ====================
    def post_plant(self, post_button, product, category, time_var_name, need_count=None):
        """
        设置岗位生产（模仿农田 post_plant，去掉买种子步骤）。
        按缺口数量计算生产次数，避免浪费。
        """
        self.post_close()
        self.post_open(post_button)
        self.device.screenshot()

        # Получение кнопки выбора продукта и кнопки подтверждения детекции
        selection = None
        selection_check = None
        for item in self.inventory_config[category]['items']:
            if item['name'] == product:
                selection = item['selection']
                selection_check = item['selection_check']
                break

        # Расчет необходимого количества единиц для производства
        max_runs = self.PRODUCT_MAX_RUNS.get(product, 5)
        max_units = max_runs * self.UNITS_PER_RUN
        if need_count and need_count > 0:
            # Минимум 1 цикл на пост (4 ед.), не более максимума для продукта
            runs = max(1, min(max_runs, -(-need_count // self.UNITS_PER_RUN)))  # Деление с округлением вверх (ceil)
            target_units = runs * self.UNITS_PER_RUN
        else:
            runs = max_runs
            target_units = max_units  # По умолчанию максимальное производство
        logger.info(f"[Остров — шахта и лес] {product}: дефицит {need_count or 'полное производство'} ед.; назначено {runs}/{max_runs} запусков ({target_units} ед.)")

        while 1:
            self.device.screenshot()

            if self.appear_then_click(ISLAND_POST_SELECT, offset=1):
                continue

            if self.appear(ISLAND_SELECT_CHARACTER_CHECK, offset=1):
                character_filter = self.worker_filters.get(category, "WorkerJuu")
                if self.select_character(character_list=character_filter):
                    if not self.confirm_selected_character(f"{product}: производственное назначение"):
                        self.back_to_postmanage_from_dispatch()
                        return False
                else:
                    logger.warning(f"[Остров — шахта и лес] Для производственного задания {product} нет доступных персонажей: {character_filter}")
                    self.back_to_postmanage_from_dispatch()
                    return False
                continue

            if self.appear(ISLAND_SELECT_PRODUCT_CHECK, offset=1):
                # Клик по кнопке продукта
                if selection is not None:
                    self.device.click(selection)
                    self.device.sleep(0.3)
                # Проверка правильности выбора продукта (проверка появления selection_check)
                if selection_check is not None:
                    self.device.screenshot()
                    if not self.match_template_color(selection_check, offset=20, similarity=0.85, threshold=10):
                        logger.warning(f"[Остров — шахта и лес] Выбор продукта {product} не подтверждён; возможно, требуется прокрутка")
                        self.device.swipe_vector(vector=(0, -200), box=(333, 142, 431, 602), name="SelectionUpSwipe")
                        self.device.sleep(0.3)
                        self.device.click(SELECT_PRODUCT_INERTIA_STOP)
                        self.device.sleep(0.2)
                        continue
                # Установка объема производства
                if runs == max_runs:
                    # Для максимума напрямую используем POST_MAX
                    self.device.click(POST_MAX)
                    self.device.sleep(0.3)
                elif runs > 1:
                    # По умолчанию 1 цикл, циклическое увеличение через POST_ADD_ONE_A/B/C
                    self.post_add_one(runs - 1, interval=0.1)
                # При runs == 1 по умолчанию уже 1 цикл, действий не требуется
                self.device.click(POST_ADD_ORDER)
                self.device.sleep(0.5)
                break

        # Повторное открытие → запись времени
        self.post_open(post_button)
        self.device.sleep(0.3)
        self.device.screenshot()
        time_work = Duration(ISLAND_WORKING_TIME)
        time_value = time_work.ocr(self.device.image)
        finish_time = current_time() + time_value
        setattr(self, time_var_name, finish_time)

        # Обновление записи поста
        for pid, pinfo in self.posts.items():
            if pinfo['button'] == post_button:
                pinfo['crop'] = product
                pinfo['state'] = 'working'
                break

        # Закрываем окно деталей во избежание перекрытия последующих свайпов и действий
        self.post_close()
        return True

    # ==================== Основной процесс запуска ====================
    def run(self):
        """
        流程：
          1. 初始化岗位信息，进入管理 → 收获 + 检测所有岗位（一次遍历）
          2. 退出管理，去仓库检查库存
          3. 回到管理 → 分配空闲岗位
          4. 执行生产
        """
        self.island_error = False

        # ===== Считывание количества постов =====
        mine_positions = self.config.IslandMine_Positions
        forest_positions = self.config.IslandForest_Positions

        # ===== Инициализация информации о постах =====
        self.posts = {}
        MINE_POST_BUTTONS = [ISLAND_MINE_POST1, ISLAND_MINE_POST2, ISLAND_MINE_POST3, ISLAND_MINE_POST4]
        FOREST_POST_BUTTONS = [ISLAND_FOREST_POST1, ISLAND_FOREST_POST2, ISLAND_FOREST_POST3, ISLAND_FOREST_POST4]
        self.time_vars = {'mine': [None] * mine_positions, 'forest': [None] * forest_positions}

        self.mine_post_ids = []
        for i in range(mine_positions):
            pid = f'ISLAND_MINE_POST{i + 1}'
            self.mine_post_ids.append(pid)
            self.posts[pid] = {'button': MINE_POST_BUTTONS[i], 'crop': None, 'runs': 0, 'state': 'unknown'}

        self.forest_post_ids = []
        for i in range(forest_positions):
            pid = f'ISLAND_FOREST_POST{i + 1}'
            self.forest_post_ids.append(pid)
            self.posts[pid] = {'button': FOREST_POST_BUTTONS[i], 'crop': None, 'runs': 0, 'state': 'unknown'}

        # ===== Шаг 1: вход в управление → сбор + детекция всех постов =====
        logger.info("[Остров — шахта и лес] Вход в управление: сбор готовой продукции и проверка состояния позиций")
        self.goto_postmanage()
        self.post_manage_mode(POST_MANAGE_PRODUCTION)
        self.post_close()
        self.post_manage_down_swipe(450)
        self.post_manage_down_swipe(450)

        # Шахта (видна сверху)
        collected_posts = []
        for pid in self.mine_post_ids:
            info = self.posts[pid]
            time_var = f'time_{pid}'
            setattr(self, time_var, None)
            if self.collect_and_detect_post(info['button'], pid, 'mine', time_var):
                collected_posts.append(pid)

        # Свайп к лесопилке
        self.device.sleep(1)
        self.post_manage_down_swipe(600)
        self.device.sleep(0.5)

        # Лесопилка
        for pid in self.forest_post_ids:
            info = self.posts[pid]
            time_var = f'time_{pid}'
            setattr(self, time_var, None)
            if self.collect_and_detect_post(info['button'], pid, 'forest', time_var):
                collected_posts.append(pid)

        if collected_posts:
            logger.info(f"[Остров — шахта и лес] При первичной проверке собрана готовая продукция: {collected_posts}")
        else:
            logger.info("[Остров — шахта и лес] При первичной проверке готовая продукция для сбора не найдена")

        # ===== Шаг 2: выход из управления → проверка запасов на складе =====
        logger.info("[Остров — шахта и лес] Выход из управления и проверка запасов на складе")
        self.ui_goto(page_island_management)
        needs = self.check_inventory_and_prepare_lists()
        # Возврат на страницу управления после проверки запасов
        self.goto_postmanage()
        self.post_manage_mode(POST_MANAGE_PRODUCTION)
        self.post_close()

        # ===== Шаг 3: распределение свободных постов =====
        # Поиск свободных постов (crop == None)
        idle_posts = {'mine': [], 'forest': []}
        for category, post_ids in [('mine', self.mine_post_ids), ('forest', self.forest_post_ids)]:
            for pid in post_ids:
                if self._post_available_for_dispatch(self.posts[pid]):
                    idle_posts[category].append(pid)

        logger.info(f"[Остров — шахта и лес] Свободные позиции: шахта — {len(idle_posts['mine'])}, лес — {len(idle_posts['forest'])}")

        # Сначала распределяем дефицитную продукцию, остальное по умолчанию
        all_to_plant = {'mine': [], 'forest': []}
        for category in ['mine', 'forest']:
            # Продукция из needs, относящаяся к этой категории
            cat_needs = needs.get(category, [])
            idle_count = len(idle_posts[category])

            # Выделяем min(свободно, дефицит) постов под дефицитный продукт
            num_from_needs = min(len(cat_needs), idle_count)
            for i in range(num_from_needs):
                all_to_plant[category].append(cat_needs[i])

            # Оставшиеся свободные → распределяем продукт по умолчанию, остальные оставляем пустыми
            remaining = idle_count - num_from_needs
            if category == 'mine':
                # MineSilver: несколько свободных постов производят серебряную руду, остальные пусты
                silver_count = min(self.config.IslandMine_MineSilver, remaining)
                for _ in range(silver_count):
                    all_to_plant['mine'].append('Silver')
            else:
                # CutElegant: несколько свободных постов производят изящную древесину, остальные пусты
                elegant_count = min(self.config.IslandForest_CutElegant, remaining)
                for _ in range(elegant_count):
                    all_to_plant['forest'].append('Elegant')

            if all_to_plant[category]:
                logger.info(f"[Остров — шахта и лес] {category}: требуется назначить производство: {all_to_plant[category]}")

        # ===== Шаг 4: запуск производства (без покупки семян) =====
        if any(all_to_plant.values()):
            # Сначала возвращаемся наверх (шахта видна)
            self.post_manage_down_swipe(450)
            self.post_manage_down_swipe(450)
            self.device.sleep(0.5)

            # Обработка шахты
            for i, pid in enumerate(idle_posts['mine']):
                if i >= len(all_to_plant['mine']):
                    break
                product = all_to_plant['mine'][i]
                need_count = self.needs_count.get(('mine', product), None)
                time_var = f'time_{pid}'
                logger.info(f"[Остров — шахта и лес] Назначение шахты {pid}: {product}")
                self.post_plant(self.posts[pid]['button'], product, 'mine', time_var, need_count=need_count)

            # Свайп к лесопилке
            self.device.sleep(1)
            self.post_manage_down_swipe(600)
            self.device.sleep(0.5)

            # Обработка лесопилки
            for i, pid in enumerate(idle_posts['forest']):
                if i >= len(all_to_plant['forest']):
                    break
                product = all_to_plant['forest'][i]
                need_count = self.needs_count.get(('forest', product), None)
                time_var = f'time_{pid}'
                logger.info(f"[Остров — шахта и лес] Назначение леса {pid}: {product}")
                self.post_plant(self.posts[pid]['button'], product, 'forest', time_var, need_count=need_count)

        # ===== Сбор времени завершения =====
        future_finish = []
        for category in ['mine', 'forest']:
            post_ids = self.mine_post_ids if category == 'mine' else self.forest_post_ids
            for pid in post_ids:
                time_var = f'time_{pid}'
                ft = getattr(self, time_var, None)
                if ft is not None:
                    future_finish.append(ft)

        six_hours_later = current_time() + timedelta(hours=6)
        future_finish.append(six_hours_later)
        future_finish.sort()
        self.config.task_delay(target=future_finish)
        logger.info(f'[Остров — шахта и лес] Следующий запуск: {future_finish[0]}')

        if self.island_error:
            from module.exception import GameBugError
            raise GameBugError("Обнаружена ошибка острова ERROR1; требуется перезапуск")
