"""岛屿店铺基类模块。

为烧烤店、茶馆、餐厅、制造工坊等所有岛屿店铺提供通用的岗位管理与商品生产框架。
包含产品选择、厨师筛选、岗位填充循环、季节性商品处理等核心逻辑，是所有店铺模块的父类。
"""
from module.island.island import *
from collections import Counter
from datetime import timedelta

from module.config.time_source import now as current_time
from module.handler.login import LoginHandler
from module.island.warehouse import *
from module.logger import logger
from module.island.island_season import get_global_season_config


class IslandShopBase(Island, WarehouseOCR):
    _MAX_FILL_LOOP = 10  # Максимальное число итераций цикла while при заполнении постов
    PRODUCT_SELECT_RETRY_LIMIT = 3  # Максимум повторных входов после ошибки распознавания выбора блюда
    POST_PRODUCE_LIMIT = 7  # Максимальное количество продукции за один цикл на каждом посту ресторана

    def __init__(self, config, device=None, task=None):
        # Инициализируем каждого родителя отдельно
        Island.__init__(self, config=config, device=device, task=task)
        WarehouseOCR.__init__(self)  # WarehouseOCR, вероятно, не требует параметров

        # Атрибуты, которые должны задаваться подклассом
        self.shop_items = []  # Список товаров
        self.shop_type = ""  # Тип магазина: grill, teahouse, tailor, toolshop, furniture
        self.filter_asset = None  # Ресурс фильтра склада
        self.post_buttons = {}  # Кнопки постов
        self.time_prefix = "time_meal"  # Префикс переменной времени
        self.special_character = False
        self.special_food = None
        # Конфигурация выбора персонажа
        self.chef_config = None

        # Общие атрибуты
        self.name_to_config = {}
        self.posts = {}
        self.post_check_meal = {}  # Продукты, находящиеся в производстве на постах
        self.post_products = []  # Упорядоченный список; одно блюдо может встречаться в нескольких слотах
        self.warehouse_counts = {}  # Продукты, распознанные на складе
        self.to_post_products = {}
        self.current_totals = {}

        # Особые материалы, могут переопределяться подклассом
        self.special_materials = {}

        # Состав наборов, может переопределяться подклассом
        self.meal_compositions = {}

        # Префиксы конфигурации, могут переопределяться подклассом
        self.config_meal_prefix = "Island_Meal"
        self.config_number_prefix = "Island_MealNumber"
        self.config_away_cook = "IslandNextTask_AwayCook"
        self.config_post_number = "Island_PostNumber"

        # Конфигурация прокрутки, может переопределяться подклассом
        self.post_manage_swipe_count = 1  # По умолчанию одна прокрутка на 450

    # ==================== Поддержка сезонной конфигурации ====================

    def _init_season_config(self):
        """初始化季节配置"""
        self.season_config = get_global_season_config(self.config)
        self.current_season = self.season_config.season
        self.season_name = self.season_config.season_name

        if self.season_config.is_seasonal_enabled:
            logger.info(f"[Остров] Текущий сезон: {self.season_name}; сезонные ограничения включены")
        else:
            logger.info("[Остров] Сезонные ограничения отключены")

    def is_seasonal_item_enabled(self, item_name):
        """
        判断指定物品在当前季节是否启用

        如果季节限定未启用（none），则默认所有物品可用。
        如果季节限定启用，则只返回本季节的物品列表。

        Args:
            item_name: 物品名称

        Returns:
            bool
        """
        if not hasattr(self, 'season_config') or self.season_config is None:
            return True
        if not self.season_config.is_seasonal_enabled:
            return True
        # Проверяем, присутствует ли предмет в списке текущего сезона
        seasonal_items = self.season_config.get_seasonal_items(self.shop_type)
        if item_name in seasonal_items:
            return True
        # Сезонные предметы не из текущего сезона отключаем
        # Проверяем, является ли предмет ограниченным предметом другого сезона
        for season_key in ['spring', 'summer', 'autumn', 'winter']:
            if season_key == self.season_config.season:
                continue
            from module.island.island_season import SEASONAL_ITEMS
            other_items = SEASONAL_ITEMS.get(season_key, {}).get(self.shop_type, [])
            if item_name in other_items:
                logger.info(f"[Остров] Предмет [{item_name}] относится к сезону {season_key}; в текущем сезоне {self.season_name} недоступен")
                return False
        return True
    def produce_check(self):
        self.device.sleep(0.5)
        image = self.device.screenshot()
        area = (493, 597, 621, 643)
        color = get_color(image, area)
        if color_similar(color, (153, 156, 156), 80):
            return True
        else:
            return False
    def setup_config(self, config_meal_prefix, config_number_prefix,
                     config_away_cook, config_post_number):
        """从配置中读取餐品需求 - 修改为8种餐品"""
        # Задаём префиксы конфигурации
        self.config_meal_prefix = config_meal_prefix
        self.config_number_prefix = config_number_prefix
        self.config_away_cook = config_away_cook
        self.config_post_number = config_post_number

        # Читаем потребности для 8 видов блюд
        self.post_products = []

        for i in range(1, 9):  # От 1 до 8
            meal_key = f'{self.config_meal_prefix}{i}'
            number_key = f'{self.config_number_prefix}{i}'

            meal_name = getattr(self.config, meal_key, None)
            if meal_name is not None and meal_name != "None":
                meal_number = getattr(self.config, number_key, 0)
                self.post_products.append((meal_name, meal_number))

    def initialize_shop(self):
        """初始化店铺，子类必须在__init__中调用"""
        self.name_to_config = {item['name']: item for item in self.shop_items}

        # Инициализируем состояния постов
        for post_id, button in self.post_buttons.items():
            self.posts[post_id] = {'status': 'none', 'button': button}

    # ============ Общие методы ============
    def post_check(self, post_id, time_var_name):
        """检查岗位状态（通用）"""
        post_button = self.posts[post_id]['button']
        self.post_close()
        self.post_open(post_button)
        self.device.sleep(0.5)
        image = self.device.screenshot()
        ocr_post_number = Digit(OCR_POST_NUMBER, letter=(57, 58, 60), threshold=100,
                                alphabet='0123456789')
        if self.appear(ISLAND_WORK_COMPLETE, offset=1):
            self.posts[post_id]['status'] = 'idle'
            setattr(self, time_var_name, None)
        elif self.appear(ISLAND_WORKING):
            product = self.post_product_check()
            number = ocr_post_number.ocr(image)
            time_work = Duration(ISLAND_WORKING_TIME)
            time_value = time_work.ocr(self.device.image)
            finish_time = current_time() + time_value
            setattr(self, time_var_name, finish_time)
            self.posts[post_id]['status'] = 'working'
            if product is not None:
                if product in self.post_check_meal:
                    self.post_check_meal[product] += number
                else:
                    self.post_check_meal[product] = number
        elif self.appear(ISLAND_POST_SELECT):
            self.posts[post_id]['status'] = 'idle'
            setattr(self, time_var_name, None)
        self.post_get_and_close()
        self.device.sleep(0.5)

    def post_product_check(self):
        """检查岗位生产的产品（通用）"""
        for item in self.shop_items:
            if self.appear(item['post_action']):
                return item['name']
        return None

    def get_warehouse_counts(self):
        """获取仓库数量（通用）"""
        self.warehouse_filter(self.filter_asset)
        image = self.device.screenshot()

        for dish in self.shop_items:
            self.warehouse_counts[dish['name']] = self.ocr_item_quantity(image, dish['template'])
            if self.warehouse_counts[dish['name']]:
                logger.info(f"{dish['name']}: {self.warehouse_counts[dish['name']]}")
        return self.warehouse_counts
    def select_special_character(self,product):
        return self.select_character(character_list=self.chef_config)
    def produce_special_food(self):
        pass

    def retry_product_selection_from_postmanage(self, post_button, product, failed_product, failed_count):
        """餐品选择失败后退出岗位，重新进入同一岗位走完整派遣流程。"""
        if failed_count >= self.PRODUCT_SELECT_RETRY_LIMIT:
            raise GameStuckError(
                f"При выборе блюда для производства {product} {failed_count} раз подряд не удалось распознать {failed_product}"
            )

        logger.warning(
            f"При выборе блюда для производства {product} не удалось распознать {failed_product}; "
            f"выход из позиции и повторный вход ({failed_count}/{self.PRODUCT_SELECT_RETRY_LIMIT})"
        )
        if not self.back_to_postmanage_from_dispatch():
            raise GameStuckError(f"После ошибки выбора блюда для производства {product} не удалось вернуться к управлению позициями")

        self.post_manage_mode(POST_MANAGE_PRODUCTION)
        self.post_manage_swipe(self.post_manage_swipe_count)
        if not self.post_open(post_button):
            raise GameStuckError(f"После ошибки выбора блюда для производства {product} не удалось повторно открыть позицию")
        self.device.sleep(0.5)

    def increase_product_selection_failure(self, product_select_failures, failed_product):
        """记录单个餐品的选择失败次数。"""
        failed_count = product_select_failures.get(failed_product, 0) + 1
        product_select_failures[failed_product] = failed_count
        return failed_count

    def post_produce(self, post_id, product, number, time_var_name,product2=None):
        """生产产品（通用）"""
        post_button = self.posts[post_id]['button']
        self.post_close()
        self.post_open(post_button)
        self.device.sleep(0.5)
        time_work = Duration(ISLAND_WORKING_TIME)
        selection = self.name_to_config[product]['selection']
        selection_check = self.name_to_config[product]['selection_check']
        product_select_failures = {}
        for _ in self.loop(timeout=120, skip_first=False):
            if self.appear_then_click(ISLAND_POST_SELECT, offset=1):
                self.device.sleep(0.5)
                continue
            if self.appear(ISLAND_SELECT_CHARACTER_CHECK, offset=1):
                if self.special_character:
                    selected = self.select_special_character(product)
                    character_filter = "special"
                else:
                    selected = self.select_character(character_list=self.chef_config)
                    character_filter = self.chef_config
                if selected:
                    if not self.confirm_selected_character(f"{product}: производственное назначение"):
                        self.back_to_postmanage_from_dispatch()
                        return 0
                else:
                    logger.warning(f"[Остров] Для производственного задания {product} нет доступных персонажей: {character_filter}")
                    self.back_to_postmanage_from_dispatch()
                    return 0
                continue
            if self.appear(ISLAND_SELECT_PRODUCT_CHECK, offset=1):
                if self.select_product(selection, selection_check):
                    self.device.sleep(0.5)
                    if self.produce_check():
                        logger.warning(f"[Остров] Недостаточно сырья; невозможно произвести {product}")
                        self.device.sleep(0.5)
                        if product == self.special_food:
                            if product2:
                                selection2 = self.name_to_config[product2]['selection']
                                selection_check2 = self.name_to_config[product2]['selection_check']
                                if not self.select_product(selection2, selection_check2):
                                    failed_count = self.increase_product_selection_failure(
                                        product_select_failures, product2
                                    )
                                    self.retry_product_selection_from_postmanage(
                                        post_button, product, product2, failed_count
                                    )
                                    continue
                                self.device.sleep(0.5)
                                if self.produce_check():
                                    logger.warning(f"[Остров] Недостаточно сырья; невозможно произвести {product2}")
                                    self.device.click(ISLAND_BACK)
                                    self.device.sleep(0.5)
                                    return 0  # Возвращаем 0 при нехватке сырья
                                else:
                                    self.post_add_one(number - 1)
                                    self.device.sleep(0.5)
                                    self.device.click(POST_ADD_ORDER)
                                    self.device.sleep(0.5)
                                    break
                            else:
                                self.device.click(ISLAND_BACK)
                                self.device.sleep(0.5)
                                return 0  # Возвращаем 0 при нехватке сырья

                        else:
                            self.device.click(ISLAND_BACK)
                            self.post_close()
                            self.post_manage_swipe(self.post_manage_swipe_count)
                            self.device.sleep(0.5)
                            return 0  # Возвращаем 0 при нехватке сырья
                    else:
                        self.post_add_one(number - 1)
                        self.device.sleep(0.5)
                        self.device.click(POST_ADD_ORDER)
                        self.device.sleep(0.5)
                        break
                else:
                    failed_count = self.increase_product_selection_failure(
                        product_select_failures, product
                    )
                    self.retry_product_selection_from_postmanage(
                        post_button, product, product, failed_count
                    )
                continue
        else:
            raise GameStuckError(f"Истекло время производственного задания {product}")
        self.wait_until_appear(ISLAND_POSTMANAGE_CHECK)
        self.device.sleep(0.5)
        self.post_manage_swipe(self.post_manage_swipe_count)
        logger.info(post_button)
        self.post_open(post_button)
        self.device.sleep(0.5)
        image = self.device.screenshot()
        ocr_post_number = Digit(OCR_POST_NUMBER, letter=(57, 58, 60), threshold=100,
                                alphabet='0123456789')
        actual_number = ocr_post_number.ocr(image)
        time_value = time_work.ocr(self.device.image)
        finish_time = current_time() + time_value
        setattr(self, time_var_name, finish_time)
        self.posts[post_id]['status'] = 'working'
        # Списываем предварительные материалы, подкласс может переопределить логику
        self.deduct_materials(product, actual_number)
        logger.info(f"[Остров] Производство запланировано: {product} x{actual_number}")
        self.post_close()
        # Возвращаем фактическое количество произведённой продукции
        return actual_number

    def deduct_materials(self, product, number):
        """扣除前置材料（包括套餐原材料）"""
        # Списываем сырьё наборов
        if product in self.meal_compositions:
            composition = self.meal_compositions[product]
            quantity_per = composition.get('quantity_per', 1)
            for material in composition['required']:
                material_needed = number * quantity_per
                if material in self.warehouse_counts:
                    self.warehouse_counts[material] -= material_needed
                    logger.info(f"[Остров] Списано сырьё: {material} -{material_needed} (для приготовления {product})")

    def get_idle_posts(self):
        """获取空闲的岗位ID列表（通用）"""
        return [post_id for post_id, post_info in self.posts.items()
                if post_info['status'] == 'idle']

    # ============ Основная логика ============

    def _schedule_and_track(self, produced_pass):
        """排产并将本轮产出记录到 produced_pass。
        produced_pass 跨多次排产累加，让后续 _compute_base_demands 的 current_totals
        能看到刚生产但未入库的量（不修改 warehouse_counts——仓库里确实还没有）。
        """
        if not self.to_post_products:
            return
        to_post_snapshot = dict(self.to_post_products)
        self.schedule_production()
        for name in to_post_snapshot:
            remaining = self.to_post_products.get(name, 0)
            produced_qty = to_post_snapshot[name] - remaining
            if produced_qty > 0:
                produced_pass[name] = produced_pass.get(name, 0) + produced_qty

    def _compute_base_demands(self, check_materials=False, force_skip=None):
        """计算基础需求：严格按槽位顺序处理，找到第一个有缺口的槽位
        即停止，后续槽位本轮不处理。

        保留线：取本轮已迭代槽位中各产品的最高目标（无缺口时覆盖全部
        槽位，全部达标时保留线取最大目标），扣除后 current_totals 为
        超额库存，可作为原料被后续槽位消费。

        Args:
            check_materials: False（默认）需求计算，原料为0不阻断，留给
                             process_meal_requirements 分解。
                             True 排产失败后使用，严格检查零库存来跳过缺口。
            force_skip: 强制跳过的产品名集合。排产多次失败（非原料原因如
                        角色被占）时使用，让本轮不再停留在这个缺口上。
        """
        # ============ Расчёт базовых потребностей ============
        logger.info("[Остров] Этап: базовые потребности" + (" (строгий режим)" if check_materials else ""))

        self.to_post_products = {}
        virtual_totals = dict(self.current_totals)
        force_skip = force_skip or set()

        # Идём по слотам и обрабатываем только первый воспроизводимый дефицит
        break_idx = len(self.post_products)
        for idx, (name, target) in enumerate(self.post_products):
            current = virtual_totals.get(name, 0)
            if current < target:
                if name in force_skip:
                    logger.info(f"[Остров] Слот {idx + 1} {name} уже не удалось обработать в этом цикле; принудительный пропуск")
                    continue
                deficit = target - current
                # В строгом режиме check_materials=True проверяем нулевой запас, чтобы пропустить невоспроизводимый дефицит
                if self.get_max_producible(
                        name, min(self.POST_PRODUCE_LIMIT, deficit),
                        skip_zero_materials=not check_materials) <= 0:
                    logger.info(f"[Остров] В слоте {idx + 1} для {name} полностью отсутствуют материалы; пропуск в этом цикле")
                    continue
                self.to_post_products[name] = deficit
                virtual_totals[name] = target
                break_idx = idx
                break

        # Резервная линия: берём максимальную цель только среди уже пройденных слотов, включая точку break
        max_targets = {}
        for name, target in self.post_products[:break_idx + 1]:
            max_targets[name] = max(max_targets.get(name, 0), target)
        for name, max_target in max_targets.items():
            current = self.current_totals.get(name, 0)
            if current < max_target:
                self.current_totals[name] = 0
            else:
                self.current_totals[name] = current - max_target

    def run(self):
        self.island_error = False
        self.goto_postmanage()
        self.post_manage_mode(POST_MANAGE_PRODUCTION)
        self.post_close()
        self.post_manage_swipe(self.post_manage_swipe_count)

        # Проверяем состояние постов
        post_count = getattr(self.config, self.config_post_number, 2)
        time_vars = []
        for i in range(post_count):
            time_var_name = f'{self.time_prefix}{i + 1}'
            time_vars.append(time_var_name)
            setattr(self, time_var_name, None)
            post_id = f'ISLAND_{self.shop_type.upper()}_POST{i + 1}'
            self.post_check(post_id, time_var_name)

        # Получаем свободные посты
        idle_posts = self.get_idle_posts()

        if idle_posts:
            self.get_warehouse_counts()
            self.goto_postmanage()
            self.post_manage_mode(POST_MANAGE_PRODUCTION)
            self.post_close()
            self.post_manage_swipe(self.post_manage_swipe_count)

            # Вычисляем текущий общий запас
            self.current_totals = {}
            all_product_names = set(name for name, _ in self.post_products)
            for item in all_product_names | set(self.post_check_meal.keys()) | set(
                    self.warehouse_counts.keys()):
                self.current_totals[item] = self.post_check_meal.get(item, 0) + self.warehouse_counts.get(item, 0)

            # ============ Отладочная информация ============
            logger.info(f"[Остров] === Отладочная информация ===")
            logger.info(f"[Остров] Запасы на складе: {self.warehouse_counts}")
            logger.info(f"[Остров] Запасы в производстве: {self.post_check_meal}")
            logger.info(f"[Остров] Текущий общий запас: {self.current_totals}")
            logger.info(f"[Остров] Конфигурация базовых потребностей ({len(self.post_products)} слотов): {self.post_products}")
            logger.info("===============")

            # Сохраняем исходные запасы и восстанавливаем их при retry
            _orig_totals = dict(self.current_totals)
            self._compute_base_demands()

            logger.info(f"[Остров] Ожидающие приготовления: {self.to_post_products}")
            logger.info(f"[Остров] Текущий остаток запасов: {self.current_totals}")
            # ============ Разбираем составные наборы ============
            if self.to_post_products:
                self.to_post_products = self.process_meal_requirements(self.to_post_products)
                logger.info(f"[Остров] План производства базовых потребностей: {self.to_post_products}")

            # ============ Назначаем производство базовых потребностей, пока есть свободные посты и дефицит ============
            _produced_pass = {}  # Накопленное производство в рамках текущего вызова run()
            _force_skip_run = set()  # Дефициты, которые несколько раз не удалось произвести не из-за сырья; принудительно пропускаются в этом цикле
            _loop_count = 0

            self._schedule_and_track(_produced_pass)

            while self.get_idle_posts():
                _loop_count += 1
                if _loop_count > self._MAX_FILL_LOOP:
                    logger.warning(f"[Остров] [Цикл] Достигнуто максимальное число итераций {self._MAX_FILL_LOOP}; принудительный выход")
                    break
                self.current_totals = dict(_orig_totals)
                for name, qty in _produced_pass.items():
                    self.current_totals[name] = self.current_totals.get(name, 0) + qty

                self._compute_base_demands(force_skip=_force_skip_run)
                if not self.to_post_products:
                    logger.info("[Остров] Потребности всех слотов удовлетворены")
                    break

                self.to_post_products = self.process_meal_requirements(self.to_post_products)
                logger.info(f"[Остров] План производства базовых потребностей: {self.to_post_products}")

                prev_pass_total = sum(_produced_pass.values())
                self._schedule_and_track(_produced_pass)

                if sum(_produced_pass.values()) == prev_pass_total and self.to_post_products:
                    # Сначала переключаемся в строгий режим, чтобы обойти ситуацию с реально отсутствующим сырьём
                    logger.info("[Остров] [Цикл] Не удалось распределить текущий дефицит; переход к строгому сканированию")
                    self.to_post_products = {}
                    self.current_totals = dict(_orig_totals)
                    for name, qty in _produced_pass.items():
                        self.current_totals[name] = self.current_totals.get(name, 0) + qty
                    self._compute_base_demands(check_materials=True)
                    if not self.to_post_products:
                        break
                    self.to_post_products = self.process_meal_requirements(self.to_post_products)
                    logger.info(f"[Остров] План производства базовых потребностей (строгий режим): {self.to_post_products}")

                    strict_prev_total = sum(_produced_pass.values())
                    self._schedule_and_track(_produced_pass)

                    if sum(_produced_pass.values()) == strict_prev_total and self.to_post_products:
                        # В строгом режиме тоже нет выпуска — причина не в сырье, например персонаж занят; принудительно пропускаем
                        stuck_now = set(self.to_post_products.keys())
                        logger.info(f"[Остров] [Цикл] Строгий режим также не дал результата; принудительный пропуск: {stuck_now}")
                        _force_skip_run.update(stuck_now)
                        self.to_post_products = {}
                    continue

            # ============ Проверяем оставшиеся свободные посты и назначаем особое или постоянное блюдо ============
            # Повторно проверяем свободные посты, поскольку часть могла быть занята базовыми потребностями
            idle_posts_after_basic = self.get_idle_posts()

            # Получаем конфигурацию особого и постоянного блюда
            special_food = self.special_food
            away_cook = getattr(self.config, self.config_away_cook, None)

            # Проверяем допустимость особого блюда: оно не None и не строка "None"
            has_special_food = (special_food and special_food != "None" and
                                special_food in self.name_to_config)

            # Проверяем допустимость постоянного блюда: оно не None и не строка "None"
            has_away_cook = (away_cook and away_cook != "None" and
                             away_cook in self.name_to_config)

            if idle_posts_after_basic and (has_special_food or has_away_cook):
                logger.info(f"[Остров] После базовых потребностей осталось свободных позиций: {len(idle_posts_after_basic)}")

                # Назначаем производство в зависимости от доступных вариантов
                for post_id in idle_posts_after_basic:
                    post_num = post_id[-1]
                    time_var_name = f'{self.time_prefix}{post_num}'

                    if has_special_food and has_away_cook:
                        # Случай 1: настроены и особое, и постоянное блюдо
                        logger.info(f"[Остров] Настроены и особое блюдо {special_food}, и постоянное блюдо {away_cook}")
                        logger.info(f"[Остров] Сначала пробуем произвести особое блюдо; при нехватке сырья — постоянное")

                        # Пытаемся произвести особое блюдо; при нехватке сырья автоматически пробуем постоянное
                        result = self.post_produce(
                            post_id,
                            product=special_food,
                            number=self.POST_PRODUCE_LIMIT,
                            time_var_name=time_var_name,
                            product2=away_cook
                        )

                        if result == 0:
                            # Сырья недостаточно и для особого, и для постоянного блюда
                            logger.info(f"[Остров] Недостаточно сырья и для особого блюда {special_food}, и для постоянного {away_cook}; позиция остаётся свободной")
                            break
                        else:
                            logger.info(f"[Остров] Для позиции {post_id} назначено производство")

                    elif has_special_food and not has_away_cook:
                        # Случай 2: есть только особое блюдо, постоянного нет
                        logger.info(f"[Остров] Настроено только особое блюдо {special_food}; постоянное блюдо не задано")

                        result = self.post_produce(
                            post_id,
                            product=special_food,
                            number=self.POST_PRODUCE_LIMIT,
                            time_var_name=time_var_name
                        )

                        if result == 0:
                            # Недостаточно сырья для особого блюда
                            logger.info(f"[Остров] Недостаточно сырья для особого блюда {special_food}; позиция остаётся свободной")
                            break
                        else:
                            logger.info(f"[Остров] Для позиции {post_id} назначено производство особого блюда")

                    elif not has_special_food and has_away_cook:
                        # Случай 3: есть только постоянное блюдо, особого нет
                        logger.info(f"[Остров] Настроено только постоянное блюдо {away_cook}; особое блюдо не задано")

                        # Проверяем ограничения по материалам
                        batch_size = self.POST_PRODUCE_LIMIT
                        batch_size = self.get_max_producible(away_cook, batch_size)

                        if batch_size > 0:
                            result = self.post_produce(
                                post_id,
                                product=away_cook,
                                number=batch_size,
                                time_var_name=time_var_name
                            )

                            if result == 0:
                                logger.info(f"[Остров] Недостаточно сырья для постоянного блюда {away_cook}; позиция остаётся свободной")
                                break
                            else:
                                logger.info(f"[Остров] Для позиции {post_id} назначено постоянное блюдо {away_cook} x{batch_size}")
                        else:
                            logger.info(f"[Остров] Недостаточно материалов для производства {away_cook}; позиция {post_id} пропущена")
                            break

                    else:
                        # Случай 4: нет ни особого, ни постоянного блюда
                        logger.info("[Остров] Особое или постоянное блюдо не задано; позиция остаётся свободной")
                        break  # Выходим из цикла и больше не обрабатываем остальные свободные посты

            elif idle_posts_after_basic:
                # Свободные посты есть, но особое или постоянное блюдо не настроено
                logger.info(f"[Остров] Есть свободные позиции ({len(idle_posts_after_basic)}), но особое или постоянное блюдо не задано; позиции остаются свободными")

        # ============ Настраиваем задержку задачи ============
        finish_times = []
        for var in time_vars:
            time_value = getattr(self, var)
            if time_value is not None:
                finish_times.append(time_value)
        hours_later = current_time() + timedelta(hours=6)
        finish_times.append(hours_later)
        finish_times.sort()
        self.config.task_delay(target=finish_times)
        if self.island_error:
            from module.exception import GameBugError
            raise GameBugError("Обнаружена ошибка острова ERROR1; требуется перезапуск")

    def process_meal_requirements(self, source_products):
        """处理套餐需求（修正版）"""
        logger.info(f"[Остров] === Вход в process_meal_requirements ===")
        logger.info(f"[Остров] Входные потребности: {source_products}")

        result = {}

        # 1. Разделяем потребности на наборы и базовые блюда
        meal_demands = {}
        base_demands = {}

        for product, quantity in source_products.items():
            if quantity <= 0:
                continue
            if product in self.meal_compositions:
                meal_demands[product] = quantity
                logger.info(f"[Остров]   Распознано как набор: {product} x{quantity}")
            else:
                base_demands[product] = quantity
                logger.info(f"[Остров]   Распознано как базовое блюдо: {product} x{quantity}")

        logger.info(f"[Остров] Потребность в наборах: {meal_demands}")
        logger.info(f"[Остров] Базовая потребность: {base_demands}")

        # 2. Обрабатываем потребности в наборах — добавляем их напрямую, поскольку наборы можно производить непосредственно
        # Здесь уже передана чистая потребность, повторно вычитать запас не нужно
        for meal, meal_quantity in meal_demands.items():
            if meal_quantity > 0:
                result[meal] = meal_quantity
                logger.info(f"[Остров]   Набор производится напрямую: {meal} x{meal_quantity}")

        # 3. Обрабатываем базовые потребности, которые также могут быть сырьём для наборов
        material_needs = {}

        # Вычисляем суммарное количество сырья для всех наборов
        for meal, meal_quantity in meal_demands.items():
            if meal_quantity > 0 and meal in self.meal_compositions:
                composition = self.meal_compositions[meal]
                for material in composition['required']:
                    needed = meal_quantity * composition.get('quantity_per', 1)
                    material_needs[material] = material_needs.get(material, 0) + needed
                    logger.info(f"[Остров]   Для набора {meal} требуется сырьё: {material} x{needed}")

        logger.info(f"[Остров] Общая потребность в сырье: {material_needs}")

        # 4. Обрабатываем базовые потребности с учётом потребности в сырье
        for base_product, base_quantity in base_demands.items():
            logger.info(f"[Остров]   Обработка базового блюда {base_product}: базовая потребность={base_quantity}")

            # Общая потребность = базовая чистая потребность + потребность в сырье для наборов
            total_needed = base_quantity

            # Если базовое блюдо также является сырьём для набора, добавляем потребность в сырье
            if base_product in material_needs:
                # Из потребности в сырье вычитаем запас, поскольку раньше он ещё не вычитался
                raw_material_needed = material_needs[base_product]

                # Проверяем запас сырья
                current_stock = self.current_totals.get(base_product, 0)
                logger.info(f"[Остров]     Потребность в сырье: +{raw_material_needed}, текущий запас: {current_stock}")

                # Вычисляем чистую потребность в сырье
                net_raw_needed = max(0, raw_material_needed - current_stock)
                total_needed += net_raw_needed

                logger.info(f"[Остров]     Чистая потребность в сырье: {net_raw_needed}, общая потребность: {total_needed}")

                # Удаляем из material_needs, чтобы не учитывать повторно
                del material_needs[base_product]
            else:
                # Это не сырьё для набора, используем базовую потребность напрямую
                logger.info(f"[Остров]     Общая потребность: {total_needed}")

            if total_needed > 0:
                result[base_product] = total_needed
                logger.info(f"[Остров]     Добавлено в план производства: {base_product} x{total_needed}")
            else:
                logger.info(f"[Остров]     Производство не требуется")

        # 5. Обрабатываем оставшуюся потребность в сырье, которого нет в списке базовых потребностей
        for material, material_quantity in material_needs.items():
            logger.info(f"[Остров]   Обработка оставшегося сырья {material}: потребность={material_quantity}")

            current_stock = self.current_totals.get(material, 0)
            logger.info(f"[Остров]     Текущий общий запас: {current_stock}")

            net_needed = max(0, material_quantity - current_stock)
            if net_needed > 0:
                result[material] = net_needed
                logger.info(f"[Остров]     Добавлено в план производства: {material} x{net_needed}")
            else:
                logger.info(f"[Остров]     Запаса достаточно; производство не требуется")

        logger.info(f"[Остров] План производства без учёта ограничений особого сырья: {result}")
        # 6. Учитываем ограничения особых материалов
        result = self.apply_special_material_constraints(result)

        logger.info(f"[Остров] Итоговый план производства: {result}")
        logger.info(f"[Остров] === Выход из process_meal_requirements ===")

        return result

    def get_max_producible(self, product, requested_quantity, skip_zero_materials=False):
        """获取最大可生产数量。

        Args:
            product: 产品名称
            requested_quantity: 请求生产数量
            skip_zero_materials: 需求计算阶段为 True，原料库存为 0 时不阻断套餐，
                                 交给 process_meal_requirements 分解需求。
                                 排产阶段为 False，严格检查避免游戏层拒绝导致 stalled。
        """
        max_producible = requested_quantity
        logger.info(f"[Остров] Проверка максимального количества для производства {product}; потребность: {requested_quantity}")

        # 1. Для набора проверяем запас сырья
        if product in self.meal_compositions:
            composition = self.meal_compositions[product]
            for material in composition['required']:
                # Используем фактический запас склада, поскольку производство расходует складские материалы
                material_stock = self.warehouse_counts.get(material, 0)
                quantity_per = composition.get('quantity_per', 1)
                if quantity_per == 0:
                    continue
                max_by_material = material_stock // quantity_per
                if max_by_material <= 0:
                    if skip_zero_materials and material_stock == 0:
                        # На этапе расчёта потребности при реально нулевом запасе не блокируем, оставляем декомпозицию process_meal_requirements
                        logger.info(f"[Остров]   У {product} запас сырья {material} равен 0; ограничение пропущено на этапе расчёта потребности")
                        continue
                    else:
                        # На этапе распределения или при наличии недостаточного запаса обрабатываем строго
                        logger.info(f"[Остров]   Для {product} не хватает сырья: {material} (запас: {material_stock})")
                        return 0
                max_producible = min(max_producible, max_by_material)
                logger.info(f"[Остров]   Для {product}: сырьё {material}, запас {material_stock}, требуется на единицу {quantity_per}, максимум производства {max_by_material}")

        # 2. Проверяем ограничение количества на пост
        max_producible = min(max_producible, self.POST_PRODUCE_LIMIT)
        logger.info(f"[Остров] Ограничение позиции: максимум {self.POST_PRODUCE_LIMIT}, после ограничения: {max_producible}")

        # 3. Проверяем особые материалы, логика может переопределяться подклассом
        max_producible = self.check_special_materials(product, max_producible)
        logger.info(f"[Остров] После проверки особого сырья: {max_producible}")

        return max_producible

    def apply_special_material_constraints(self, requirements):
        """应用特殊材料限制（需求阶段）。子类可覆盖此方法。

        Args:
            requirements: 字典，{产品名: 需求数量}

        Returns:
            调整后的需求字典
        """
        return requirements

    def process_away_cook(self):
        """处理常驻餐品"""
        away_cook = getattr(self.config, self.config_away_cook, None)

        # Проверяем допустимость away_cook
        if away_cook and away_cook != "None" and away_cook in self.name_to_config:
            self.to_post_products = {away_cook: 9999}
            logger.info(f"[Остров] Режим постоянного блюда: производство {away_cook}")
        else:
            self.to_post_products = {}
            if away_cook is None or away_cook == "None":
                logger.info("[Остров] Постоянное блюдо не задано; позиция остаётся свободной")
            elif away_cook not in self.name_to_config:
                logger.info(f"[Остров] Постоянное блюдо '{away_cook}' отсутствует в списке товаров; позиция остаётся свободной")

    def schedule_production(self):
        """安排生产，利用所有空闲岗位"""
        if not self.to_post_products:
            logger.info("[Остров] Нет блюд, требующих производства")
            return

        # Получаем свободные посты
        idle_posts = self.get_idle_posts()
        if not idle_posts:
            logger.info("[Остров] Нет свободных позиций")
            return

        # Проверяем режим постоянного блюда с неограниченным количеством
        is_away_cook_mode = False
        away_cook_product = None
        for product, quantity in self.to_post_products.items():
            if quantity == 9999:  # Маркер режима постоянного блюда
                is_away_cook_mode = True
                away_cook_product = product
                break

        if is_away_cook_mode:
            logger.info(f"[Остров] Режим постоянного блюда: назначение {away_cook_product} на все свободные позиции")
            # Назначаем производство на каждый свободный пост
            for post_id in idle_posts:
                # Проверяем ограничения по материалам
                batch_size = self.POST_PRODUCE_LIMIT
                batch_size = self.get_max_producible(away_cook_product, batch_size)

                if batch_size <= 0:
                    logger.info(f"[Остров] Недостаточно исходных материалов для производства {away_cook_product}; позиция {post_id} пропущена")
                    continue

                # Назначаем производство
                post_num = post_id[-1]
                time_var_name = f'{self.time_prefix}{post_num}'
                self.post_produce(post_id, away_cook_product, batch_size, time_var_name)

            logger.info("[Остров] Режим постоянного блюда: производство назначено на все свободные позиции")
            return

        # Обычный режим: обрабатываем потребности всех продуктов
        products_to_process = list(self.to_post_products.items())

        # При нескольких продуктах сортируем по порядку слотов, отдавая приоритет сырью
        if len(products_to_process) > 1:
            # Строим отображение порядка слотов
            slot_index = {}
            idx = 0
            for name, _ in self.post_products:
                if name not in slot_index:
                    slot_index[name] = idx
                    idx += 1

            # Для сырья используем индекс самого раннего слота набора, который оно обслуживает
            for meal, comp in self.meal_compositions.items():
                if meal in slot_index:
                    meal_slot = slot_index[meal]
                    for mat in comp['required']:
                        if mat not in slot_index or slot_index[mat] > meal_slot:
                            slot_index[mat] = meal_slot

            # Сортируем по порядку слотов; внутри одного слота сырьё идёт раньше готового продукта
            # Собираем имена всего сырья из составов наборов, чтобы продукты двойного назначения не считались обычными
            material_names = set()
            for comp in self.meal_compositions.values():
                material_names.update(comp['required'])

            # Неизвестные slot_index продукты по умолчанию ставим после известных слотов
            default_slot = len(slot_index) + 1

            def slot_priority(item):
                product, _ = item
                slot = slot_index.get(product, default_slot)
                is_material = product in material_names
                return (slot, 0 if is_material else 1)

            products_to_process.sort(key=slot_priority)

        # Распределяем производственные задачи по свободным постам
        _produced_any = set()  # Продукты, для которых в этом цикле произведена хотя бы 1 единица
        post_index = 0
        total_idle_posts = len(idle_posts)

        for product, required_quantity in products_to_process:
            if required_quantity <= 0:
                continue

            # Получаем текущую оставшуюся потребность
            remaining_need = self.to_post_products.get(product, 0)
            if remaining_need <= 0:
                continue

            logger.info(f"[Остров] Попытка назначить производство {product}; потребность: {remaining_need}")

            # Распределяем производство по свободным постам до удовлетворения потребности или исчерпания постов
            while remaining_need > 0 and post_index < total_idle_posts:
                post_id = idle_posts[post_index]

                # Вычисляем максимальное возможное количество
                max_producible = self.get_max_producible(
                    product, min(self.POST_PRODUCE_LIMIT, remaining_need))

                if max_producible <= 0:
                    logger.info(f"[Остров] Материалов для производства {product} временно недостаточно; потребность сохранена до следующего цикла")
                    break  # Пропускаем текущий продукт, но сохраняем его в to_post_products

                # Назначаем производство
                post_num = post_id[-1]
                time_var_name = f'{self.time_prefix}{post_num}'

                # Назначаем производство и получаем фактическое количество
                actual_number = self.post_produce(post_id, product, max_producible, time_var_name)

                # Нулевое фактическое производство означает нехватку сырья
                if actual_number == 0:
                    logger.info(f"[Остров] При производстве {product} обнаружена нехватка сырья; потребность сохранена до следующего цикла")
                    break  # Пропускаем текущий продукт, но сохраняем его в to_post_products

                # Отмечаем производство; частичный выпуск не считается простоем
                _produced_any.add(product)
                # Обновляем потребность
                if product in self.to_post_products:
                    self.to_post_products[product] -= actual_number
                    if self.to_post_products[product] <= 0:
                        del self.to_post_products[product]

                # Обновляем оставшуюся потребность
                remaining_need = self.to_post_products.get(product, 0)

                # Переходим к следующему посту
                post_index += 1

            # Если все посты уже распределены, завершаем цикл
            if post_index >= total_idle_posts:
                break

        if self.to_post_products:
            logger.info(f"[Остров] Распределение производства завершено; оставшаяся потребность: {self.to_post_products}")
        else:
            logger.info("[Остров] Все доступные продукты назначены в производство")

    def check_special_materials(self, product, batch_size):
        """检查特殊材料（子类可覆盖）"""
        # Реализация по умолчанию не проверяет особые материалы
        return batch_size
