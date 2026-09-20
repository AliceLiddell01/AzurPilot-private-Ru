"""岛屿茶馆模块。

继承 IslandShopBase，配置茶馆的商品列表与岗位参数。
包含迎春花茶等固定位置饮品定义，支持季节性菜品、时长 OCR 与重试滑动机制。
"""
from module.island_teahouse.assets import *
from module.island.island_shop_base import IslandShopBase
from module.island.assets import *
from module.ui.page import *
from collections import Counter
from datetime import timedelta

from module.config.time_source import now as current_time
from module.logger import logger
from module.base.button import Button
from module.island.island_season import SEASONAL_ITEMS
from module.ocr.ocr import Duration, Digit


# Кнопка с фиксированной позицией — весенний цветочный чай выбирается по фиксированным координатам без проверки цвета и прокрутки вниз
FIXED_SELECT_SPRING_FLOWER_TEA = Button(
    area=(), color=(), button=(212, 300, 292, 360),
    file={'cn': '', 'en': '', 'jp': '', 'tw': ''}
)


class IslandTeahouse(IslandShopBase):
    def __init__(self, config, device=None, task=None):
        super().__init__(config=config, device=device, task=task)

        # Задаём тип магазина
        self.shop_type = "teahouse"
        self.time_prefix = "time_tea"
        self.chef_config = self.config.IslandTeahouse_ChefFilter
        self.post_open_retry_swipe = True

        # === Инициализируем глобальную сезонную конфигурацию ===
        self._init_season_config()
        old_seasonal_enabled = getattr(self.config, 'IslandTeahouse_Seasonal', False)

        # === Определяем сезонный напиток высокого приоритета ===
        self.seasonal_high_priority_drink = None  # Аналог весеннего цветочного чая / арбузного сока
        seasonal_items = self.season_config.get_seasonal_items('teahouse') if hasattr(self, 'season_config') else []

        if old_seasonal_enabled:
            # Сезонный напиток высокого приоритета задаём только при включённом переключателе «весенний цветочный чай»
            # Напиток позиции 1 (аналог весеннего цветочного чая): высокий приоритет и фиксированные координаты
            if 'spring_flower_tea' in seasonal_items:
                self.seasonal_high_priority_drink = {
                    'name': 'spring_flower_tea', 'cn_name': '迎春花茶',
                    'template': TEMPLATE_APPLE_JUICE, 'post_action': POST_APPLE_JUICE,
                    'selection': FIXED_SELECT_SPRING_FLOWER_TEA, 'selection_check': FIXED_SELECT_SPRING_FLOWER_TEA,
                }
            elif 'watermelon_juice' in seasonal_items:
                self.seasonal_high_priority_drink = {
                    'name': 'watermelon_juice', 'cn_name': '西瓜汁',
                    'template': TEMPLATE_APPLE_JUICE, 'post_action': POST_APPLE_JUICE,
                    'selection': FIXED_SELECT_SPRING_FLOWER_TEA, 'selection_check': FIXED_SELECT_SPRING_FLOWER_TEA,
                }

            if self.seasonal_high_priority_drink:
                self.special_food = self.seasonal_high_priority_drink['name']
                logger.info(f"[Остров — напитки Белого Медведя] Приоритетный сезонный напиток: {self.seasonal_high_priority_drink['cn_name']}")
            else:
                self.special_food = 'spring_flower_tea'
        else:
            logger.info("[Остров — напитки Белого Медведя] Приоритетное производство весеннего цветочного чая отключено; сезонный напиток пропущен")

        # Задаём список товаров
        self.shop_items = []
        # ---- Сезонные напитки с фиксированной позицией ----
        if self.seasonal_high_priority_drink:
            self.shop_items.append(self.seasonal_high_priority_drink)
        # ---- Обычные блюда ----
        self.shop_items.extend([
            {'name': 'apple_juice', 'template': TEMPLATE_APPLE_JUICE, 'var_name': 'apple_juice',
             'selection': SELECT_APPLE_JUICE, 'selection_check': SELECT_APPLE_JUICE_CHECK,
             'post_action': POST_APPLE_JUICE},
            {'name': 'banana_mango', 'template': TEMPLATE_BANANA_MANGO, 'var_name': 'banana_mango',
             'selection': SELECT_BANANA_MANGO, 'selection_check': SELECT_BANANA_MANGO_CHECK,
             'post_action': POST_BANANA_MANGO},
            {'name': 'honey_lemon', 'template': TEMPLATE_HONEY_LEMON, 'var_name': 'honey_lemon',
             'selection': SELECT_HONEY_LEMON, 'selection_check': SELECT_HONEY_LEMON_CHECK,
             'post_action': POST_HONEY_LEMON},
            {'name': 'strawberry_lemon', 'template': TEMPLATE_STRAWBERRY_LEMON, 'var_name': 'strawberry_lemon',
             'selection': SELECT_STRAWBERRY_LEMON, 'selection_check': SELECT_STRAWBERRY_LEMON_CHECK,
             'post_action': POST_STRAWBERRY_LEMON},
            {'name': 'strawberry_honey', 'template': TEMPLATE_STRAWBERRY_HONEY, 'var_name': 'strawberry_honey',
             'selection': SELECT_STRAWBERRY_HONEY, 'selection_check': SELECT_STRAWBERRY_HONEY_CHECK,
             'post_action': POST_STRAWBERRY_HONEY},
            {'name': 'floral_fruity', 'template': TEMPLATE_FLORAL_FRUITY, 'var_name': 'floral_fruity',
             'selection': SELECT_FLORAL_FRUITY, 'selection_check': SELECT_FLORAL_FRUITY_CHECK,
             'post_action': POST_FLORAL_FRUITY},
            {'name': 'fruit_paradise', 'template': TEMPLATE_FRUIT_PARADISE, 'var_name': 'fruit_paradise',
             'selection': SELECT_FRUIT_PARADISE, 'selection_check': SELECT_FRUIT_PARADISE_CHECK,
             'post_action': POST_FRUIT_PARADISE},
            {'name': 'lavender_tea', 'template': TEMPLATE_LAVENDER_TEA, 'var_name': 'lavender_tea',
             'selection': SELECT_LAVENDER_TEA, 'selection_check': SELECT_LAVENDER_TEA_CHECK,
             'post_action': POST_LAVENDER_TEA},
            {'name': 'sunny_honey', 'template': TEMPLATE_SUNNY_HONEY, 'var_name': 'sunny_honey',
             'selection': SELECT_SUNNY_HONEY, 'selection_check': SELECT_SUNNY_HONEY_CHECK,
             'post_action': POST_SUNNY_HONEY},
        ])
        # Задаём составы наборов
        self.meal_compositions = {
            'floral_fruity': {
                'required': ['lavender_tea', 'apple_juice'],
                'quantity_per': 1
            },
            'fruit_paradise': {
                'required': ['banana_mango', 'strawberry_honey'],
                'quantity_per': 1
            },
            'sunny_honey': {
                'required': ['strawberry_lemon', 'honey_lemon'],
                'quantity_per': 1
            }
        }

        # Задаём кнопки постов
        self.post_buttons = {
            'ISLAND_TEAHOUSE_POST1': ISLAND_TEAHOUSE_POST1,
            'ISLAND_TEAHOUSE_POST2': ISLAND_TEAHOUSE_POST2
        }

        # Задаём ресурс фильтра
        self.filter_asset = 'teahouse'

        # Задаём префиксы конфигурации (обновлено до 4 параметров, конфигурация задач удалена)
        self.setup_config(
            config_meal_prefix="IslandTeahouse_Meal",
            config_number_prefix="IslandTeahouse_MealNumber",
            config_away_cook="IslandTeahouseNextTask_AwayCook",
            config_post_number="IslandTeahouse_PostNumber"
        )

        # === Автоматическое переключение сезонных блюд ===
        # Если в Meal настроено весеннее ограниченное блюдо (pineapple_juice),
        # а текущий сезон не spring, автоматически заменяем его блюдом соответствующего слота текущего сезона
        self._auto_switch_seasonal_meals()

        # Особый материал: мёд (только для проверки и ограничения запасов, без отдельной обязательной задачи расходования)
        self.fresh_honey = 0
        self.initialize_shop()

    def _auto_switch_seasonal_meals(self):
        """
        自动切换用户配置中的春季限定餐品到当前季节对应餐品。
        迎春花茶(spring_flower_tea) -> 春季保持，夏季切换为西瓜汁(watermelon_juice)，秋冬移除。
        鲜榨菠萝汁(pineapple_juice) -> 春季保持，夏季切换为黄瓜汁(cucumber_juice)，秋冬移除。
        """
        SEASONAL_TEAHOUSE_SWITCH = {
            'spring_flower_tea': 0,  # Весенний цветочный чай -> слот 0
            'pineapple_juice': 1,    # Свежевыжатый ананасовый сок -> слот 1
        }
        CN_NAMES = {
            'spring_flower_tea': '迎春花茶',
            'pineapple_juice': '鲜榨菠萝汁',
        }
        if not hasattr(self, 'season_config') or not self.season_config:
            return
        current_season = self.season_config.season
        current_teahouse_items = SEASONAL_ITEMS.get(current_season, {}).get('teahouse', [])
        for spring_item, slot_idx in SEASONAL_TEAHOUSE_SWITCH.items():
            if not any(name == spring_item for name, _ in self.post_products):
                continue
            cn_name = CN_NAMES.get(spring_item, spring_item)
            if slot_idx < len(current_teahouse_items):
                seasonal_item = current_teahouse_items[slot_idx]
                if seasonal_item != spring_item:
                    self.post_products = [
                        (seasonal_item, target) if name == spring_item else (name, target)
                        for name, target in self.post_products
                    ]
                    logger.info(
                        f"Автопереключение сезонного блюда: {spring_item}({cn_name}) -> {seasonal_item}"
                        f" (текущий сезон: {self.season_config.season_name})"
                    )
            else:
                self.post_products = [
                    (name, target) for name, target in self.post_products
                    if name != spring_item
                ]
                logger.info(
                    f"Автоудаление сезонного блюда: {spring_item}({cn_name})"
                    f" ({self.season_config.season_name}: для этого слота нет сезонного блюда)"
                )

    def get_warehouse_counts(self):
        """覆盖：获取仓库数量，包括蜂蜜"""
        # Сначала вызываем родительский метод для получения базовых запасов
        super().get_warehouse_counts()

        # Дополнительно получаем количество мёда для ограничения запасов
        self.warehouse_filter('basic','other_from')
        image = self.device.screenshot()
        self.fresh_honey = self.ocr_item_quantity(image, TEMPLATE_FRESH_HONEY)
        logger.info(f"[Остров — напитки Белого Медведя] Количество мёда: {self.fresh_honey}")

        # Сохраняем запас мёда в warehouse_counts для унифицированной обработки
        self.warehouse_counts['fresh_honey'] = self.fresh_honey

        return self.warehouse_counts

    def check_special_materials(self, product, batch_size):
        """覆盖：检查特殊材料（蜂蜜）限制"""
        if batch_size <= 0:
            return 0

        # sunny_honey требует honey_lemon или мёд
        if product == 'sunny_honey':
            # Вычисляем доступное сырьё: мёд + запас honey_lemon
            honey_available = self.fresh_honey
            honey_lemon_available = self.warehouse_counts.get('honey_lemon', 0)
            total_available = honey_available + honey_lemon_available

            max_by_material = min(batch_size, total_available)
            return max_by_material

        # Для honey_lemon нужен мёд
        if product == 'honey_lemon':
            max_by_honey = min(batch_size, self.fresh_honey)
            return max_by_honey

        return batch_size

    def post_produce(self, post_id, product, number, time_var_name, product2=None):
        """
        覆盖父类 post_produce：
        季节高优先级饮品（迎春花茶/西瓜汁等）：点击岗位 → ISLAND_POST_SELECT进入选择 → 处理选人 → 点击固定坐标。
        不调用父类select_product（跳过图像匹配和滑动）。
        其他餐品走父类逻辑。
        """
        seasonal_drink_name = self.seasonal_high_priority_drink['name'] if self.seasonal_high_priority_drink else ''
        if product == seasonal_drink_name:
            post_button = self.posts[post_id]['button']
            self.post_close()
            self.post_open(post_button)
            self.device.sleep(0.5)
            time_work = Duration(ISLAND_WORKING_TIME)
            # Переходим в интерфейс выбора товара (выбор персонажа + товара)
            while 1:
                self.device.screenshot()
                if self.appear(ISLAND_SELECT_CHARACTER_CHECK, offset=1):
                    # Выбираем повара
                    if self.select_character(character_list=self.chef_config):
                        if not self.confirm_selected_character(f"{product}: производственное назначение"):
                            self.back_to_postmanage_from_dispatch()
                            return 0
                    else:
                        logger.warning(f"[Остров — напитки Белого Медведя] Для производственного задания {product} нет доступных персонажей: {self.chef_config}")
                        self.back_to_postmanage_from_dispatch()
                        return 0
                    continue
                if self.appear(ISLAND_SELECT_PRODUCT_CHECK, offset=1):
                    # В списке товаров нажимаем фиксированную позицию без проверки значка
                    self.device.click(FIXED_SELECT_SPRING_FLOWER_TEA)
                    self.device.sleep(0.5)
                    break
                # Нажимаем для перехода к выбору
                if self.appear_then_click(ISLAND_POST_SELECT, offset=1):
                    self.device.sleep(0.3)
                    continue
                self.device.sleep(0.3)
            # Проверяем материалы и размещаем заказ
            if self.produce_check():
                logger.warning(f"[Остров — напитки Белого Медведя] Недостаточно сырья; spring_flower_tea невозможно произвести")
                self.device.click(ISLAND_BACK)
                self.device.sleep(0.5)
                return 0
            else:
                self.post_add_one(number - 1)
                self.device.sleep(0.5)
                self.device.click(POST_ADD_ORDER)
                self.device.sleep(0.5)
            self.wait_until_appear(ISLAND_POSTMANAGE_CHECK)
            self.device.sleep(0.5)
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
            self.deduct_materials(product, actual_number)
            logger.info(f"[Остров — напитки Белого Медведя] Производство запланировано: {product} x{actual_number}")
            self.post_close()
            return actual_number

        return super().post_produce(post_id, product, number, time_var_name, product2)

    def run(self):
        """
        覆盖父类的run方法，实现迎春花茶的优先级控制：
        迎春花茶为最高优先级，在基础需求前单独生产。
        其他逻辑沿用父类流程。
        """
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
            logger.info(f"[Остров — напитки Белого Медведя] === Отладочная информация ===")
            logger.info(f"[Остров — напитки Белого Медведя] Запасы на складе: {self.warehouse_counts}")
            logger.info(f"[Остров — напитки Белого Медведя] В производстве: {self.post_check_meal}")
            logger.info(f"[Остров — напитки Белого Медведя] Текущий общий запас: {self.current_totals}")
            logger.info(f"[Остров — напитки Белого Медведя] Базовая конфигурация требований ({len(self.post_products)} слотов): {self.post_products}")
            logger.info("===============")

            # Сохраняем исходные запасы и восстанавливаем их при retry
            _orig_totals = dict(self.current_totals)
            self._compute_base_demands()

            logger.info(f"[Остров — напитки Белого Медведя] Ожидающие приготовления: {self.to_post_products}")
            logger.info(f"[Остров — напитки Белого Медведя] Текущий остаток запасов: {self.current_totals}")

            # ============ Разбираем составные наборы ============
            if self.to_post_products:
                self.to_post_products = self.process_meal_requirements(self.to_post_products)
                logger.info(f"[Остров — напитки Белого Медведя] План производства базовых требований: {self.to_post_products}")

            # ================================================================
            # Этап: сезонный напиток высокого приоритета, управляемый переключателем «весенний цветочный чай»
            # При включённом переключателе производим сезонный напиток отдельно перед базовыми потребностями, гарантируя максимальный приоритет
            # При выключенном сразу переходим к базовым потребностям
            # ================================================================
            if self.seasonal_high_priority_drink:
                drink_name = self.seasonal_high_priority_drink['name']
                drink_cn = self.seasonal_high_priority_drink['cn_name']
                logger.info(f"[Остров — напитки Белого Медведя] Этап: приоритетный сезонный напиток — {drink_cn}")
                temp_products = self.to_post_products.copy()
                self.to_post_products = {drink_name: self.POST_PRODUCE_LIMIT}
                logger.info(f"[Остров — напитки Белого Медведя] Отдельно запланировано производство {drink_cn}: {self.to_post_products}")

                self.schedule_production()

                # Восстанавливаем оставшийся план базовых потребностей
                self.to_post_products = temp_products
                logger.info(f"[Остров — напитки Белого Медведя] Оставшийся план базового производства: {self.to_post_products}")
            else:
                logger.info("[Остров — напитки Белого Медведя] Приоритетное производство весеннего цветочного чая отключено; переход непосредственно к базовым требованиям")

            # ============ Назначаем производство базовых потребностей, пока есть свободные посты и дефицит ============
            _produced_pass = {}
            _force_skip_run = set()
            _loop_count = 0

            self._schedule_and_track(_produced_pass)

            while self.get_idle_posts():
                _loop_count += 1
                if _loop_count > self._MAX_FILL_LOOP:
                    logger.warning(f"[Остров — напитки Белого Медведя] [Цикл] Достигнут максимум итераций {self._MAX_FILL_LOOP}; принудительный выход")
                    break
                self.current_totals = dict(_orig_totals)
                for name, qty in _produced_pass.items():
                    self.current_totals[name] = self.current_totals.get(name, 0) + qty

                self._compute_base_demands(force_skip=_force_skip_run)
                if not self.to_post_products:
                    logger.info("[Остров — напитки Белого Медведя] Требования всех слотов удовлетворены")
                    break

                self.to_post_products = self.process_meal_requirements(self.to_post_products)
                logger.info(f"[Остров — напитки Белого Медведя] План производства базовых требований: {self.to_post_products}")

                prev_pass_total = sum(_produced_pass.values())
                self._schedule_and_track(_produced_pass)

                if sum(_produced_pass.values()) == prev_pass_total and self.to_post_products:
                    logger.info("[Остров — напитки Белого Медведя] [Цикл] Не удалось распределить текущий дефицит; переход к строгому сканированию")

                    self.to_post_products = {}
                    self.current_totals = dict(_orig_totals)
                    for name, qty in _produced_pass.items():
                        self.current_totals[name] = self.current_totals.get(name, 0) + qty
                    self._compute_base_demands(check_materials=True)
                    if not self.to_post_products:
                        break
                    self.to_post_products = self.process_meal_requirements(self.to_post_products)
                    logger.info(f"[Остров — напитки Белого Медведя] План базового производства (строгий режим): {self.to_post_products}")

                    strict_prev_total = sum(_produced_pass.values())
                    self._schedule_and_track(_produced_pass)

                    if sum(_produced_pass.values()) == strict_prev_total and self.to_post_products:
                        stuck_now = set(self.to_post_products.keys())
                        logger.info(f"[Остров — напитки Белого Медведя] [Цикл] Строгий режим также не дал результата; принудительный пропуск: {stuck_now}")
                        _force_skip_run.update(stuck_now)
                        self.to_post_products = {}
                    continue

            # ============ Проверяем оставшиеся свободные посты и назначаем специальное или постоянное блюдо ============
            idle_posts_after_basic = self.get_idle_posts()
            away_cook = getattr(self.config, self.config_away_cook, None)
            has_away_cook = (away_cook and away_cook != "None" and
                             away_cook in self.name_to_config)

            if idle_posts_after_basic and has_away_cook:
                logger.info(f"[Остров — напитки Белого Медведя] После базовых требований осталось свободных позиций: {len(idle_posts_after_basic)}")
                for post_id in idle_posts_after_basic:
                    post_num = post_id[-1]
                    time_var_name = f'{self.time_prefix}{post_num}'
                    logger.info(f"[Остров — напитки Белого Медведя] Попытка произвести постоянное блюдо {away_cook}")
                    batch_size = self.POST_PRODUCE_LIMIT
                    batch_size = self.get_max_producible(away_cook, batch_size)
                    if batch_size > 0:
                        result = self.post_produce(
                            post_id, product=away_cook, number=batch_size,
                            time_var_name=time_var_name
                        )
                        if result == 0:
                            logger.info(f"[Остров — напитки Белого Медведя] Недостаточно сырья для постоянного блюда {away_cook}; позиция остаётся свободной")
                            break
                        else:
                            logger.info(f"[Остров — напитки Белого Медведя] Для позиции {post_id} назначено постоянное блюдо {away_cook} x{batch_size}")
                    else:
                        logger.info(f"[Остров — напитки Белого Медведя] Недостаточно материалов для {away_cook}; позиция {post_id} пропущена")
                        break
            elif idle_posts_after_basic:
                logger.info(f"[Остров — напитки Белого Медведя] Есть свободные позиции ({len(idle_posts_after_basic)}), но постоянное блюдо не задано; позиции остаются свободными")

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

    def deduct_materials(self, product, number):
        """覆盖：扣除前置材料，包括蜂蜜和套餐原材料"""
        # Сначала вызываем родительский метод для списания сырья наборов
        super().deduct_materials(product, number)

        # Для набора sunny_honey списываем сырьё
        if product == 'sunny_honey':
            # Для sunny_honey требуется по 1 honey_lemon и strawberry_lemon
            # honey_lemon можно приготовить из мёда, поэтому сначала списываем мёд, а при нехватке — honey_lemon

            honey_needed = number
            honey_lemon_needed = number

            # Сначала списываем мёд
            if self.fresh_honey >= honey_needed:
                self.fresh_honey -= honey_needed
                logger.info(f"[Остров — напитки Белого Медведя] Списан мёд: fresh_honey -{honey_needed} (для sunny_honey)")
            else:
                # Мёда недостаточно — списываем honey_lemon
                remaining_needed = honey_needed - self.fresh_honey
                if self.fresh_honey > 0:
                    logger.info(f"[Остров — напитки Белого Медведя] Списан мёд: fresh_honey -{self.fresh_honey} (для sunny_honey)")
                    self.fresh_honey = 0

                # Списываем запас honey_lemon
                if 'honey_lemon' in self.warehouse_counts:
                    available_honey_lemon = min(remaining_needed, self.warehouse_counts['honey_lemon'])
                    if available_honey_lemon > 0:
                        self.warehouse_counts['honey_lemon'] -= available_honey_lemon
                        logger.info(f"[Остров — напитки Белого Медведя] Списан honey_lemon: honey_lemon -{available_honey_lemon} (для sunny_honey)")

    def apply_special_material_constraints(self, requirements):
        """覆盖：根据蜂蜜库存调整需求"""
        result = requirements.copy()

        # Сначала обрабатываем потребность в honey_lemon
        if 'honey_lemon' in result and result['honey_lemon'] > 0:
            honey_lemon_needed = result['honey_lemon']
            max_honey_lemon = min(honey_lemon_needed, self.fresh_honey)

            if max_honey_lemon < honey_lemon_needed:
                logger.info(f"[Остров — напитки Белого Медведя] Недостаточно мёда: потребность honey_lemon изменена с {honey_lemon_needed} на {max_honey_lemon}")

            result['honey_lemon'] = max_honey_lemon

        # Обрабатываем потребность в sunny_honey
        if 'sunny_honey' in result and result['sunny_honey'] > 0:
            sunny_honey_needed = result['sunny_honey']

            # Для sunny_honey нужен honey_lemon, на каждый из которых требуется 1 мёд
            # Но потребность в honey_lemon могла уже быть скорректирована выше
            honey_lemon_for_sunny = sunny_honey_needed

            # Вычисляем количество мёда, доступного для sunny_honey
            # Вычитаем мёд, уже выделенный для honey_lemon
            honey_allocated = result.get('honey_lemon', 0)
            honey_remaining = max(0, self.fresh_honey - honey_allocated)

            max_sunny_honey = min(sunny_honey_needed, honey_remaining)

            if max_sunny_honey < sunny_honey_needed:
                logger.info(f"[Остров — напитки Белого Медведя] Недостаточно мёда: потребность sunny_honey изменена с {sunny_honey_needed} на {max_sunny_honey}")

            result['sunny_honey'] = max_sunny_honey

        return result


if __name__ == "__main__":
    az = IslandTeahouse('alas', task='Alas')
    az.device.screenshot()
    az.run()
