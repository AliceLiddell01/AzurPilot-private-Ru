"""岛屿餐厅模块。

继承 IslandShopBase，实现餐厅的菜品配置、季节性菜品管理与岗位运营。
包含凉拌双笋、芦笋炒虾仁等时令菜品定义，支持固定位置按钮与菜品种类统计。
"""
from module.island_restaurant.assets import *
from module.island.island_shop_base import IslandShopBase
from module.island.assets import *
from module.logger import logger
from collections import Counter
from datetime import timedelta

from module.config.time_source import now as current_time
from module.base.button import Button
from module.island.island_season import SEASONAL_ITEMS


# Кнопка с фиксированной позицией — положение двойных ростков бамбука в интерфейсе назначения без прокрутки
FIXED_SELECT_DOUBLE_BAMBOO_SHOOTS = Button(
    area=(), color=(), button=(212, 143, 292, 211),
    file={'cn': '', 'en': '', 'jp': '', 'tw': ''}
)

RESTAURANT_SEASONAL_DISHES = {
    'double_bamboo_shoots': {
        'name': 'double_bamboo_shoots', 'template': TEMPLATE_DOUBLE_BAMBOO_SHOOTS,
        'selection': SELECT_DOUBLE_BAMBOO_SHOOTS, 'selection_check': SELECT_DOUBLE_BAMBOO_SHOOTS_CHECK,
        'post_action': POST_DOUBLE_BAMBOO_SHOOTS, 'cn_name': '凉拌双笋'
    },
    'asparagus_shrimp': {
        'name': 'asparagus_shrimp', 'template': TEMPLATE_ASPARAGUS_SHRIMP,
        'selection': SELECT_ASPARAGUS_SHRIMP, 'selection_check': SELECT_ASPARAGUS_SHRIMP_CHECK,
        'post_action': POST_ASPARAGUS_SHRIMP, 'cn_name': '芦笋炒虾仁'
    },
    'amaranth_rice_ball': {
        'name': 'amaranth_rice_ball', 'template': TEMPLATE_AMARANTH_RICE_BALL,
        'selection': SELECT_AMARANTH_RICE_BALL, 'selection_check': SELECT_AMARANTH_RICE_BALL_CHECK,
        'post_action': POST_AMARANTH_RICE_BALL, 'cn_name': '苋菜饭团'
    },
    'tomato_egg': {
        'name': 'tomato_egg', 'template': TEMPLATE_TOMATO_EGG,
        'selection': SELECT_TOMATO_EGG, 'selection_check': SELECT_TOMATO_EGG_CHECK,
        'post_action': POST_TOMATO_EGG, 'cn_name': '番茄炒蛋'
    },
}

HIGH_PRIORITY_SEASONAL_DISHES = {
    name: {
        **RESTAURANT_SEASONAL_DISHES[name],
        'selection': FIXED_SELECT_DOUBLE_BAMBOO_SHOOTS,
        'selection_check': FIXED_SELECT_DOUBLE_BAMBOO_SHOOTS,
    }
    for name in ('double_bamboo_shoots', 'amaranth_rice_ball')
}


class IslandRestaurant(IslandShopBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Задаём тип магазина
        self.shop_type = "restaurant"
        self.time_prefix = "time_restaurant"
        self.chef_config = self.config.IslandRestaurant_ChefFilter

        # === Инициализируем глобальную сезонную конфигурацию ===
        self._init_season_config()

        # === Сопоставление сезонных блюд высокого приоритета ===
        self.seasonal_dish_slot = self._get_high_priority_seasonal_dish()

        if self.seasonal_dish_slot:
            logger.info(f"[Остров — ресторан «Есть рыба»] Приоритетное сезонное блюдо: {self.seasonal_dish_slot['cn_name']}")

        # Задаём список товаров, автоматически выбирая блюда по текущему сезону
        self.shop_items = self._get_current_seasonal_shop_items()
        # ---- Обычные блюда ----
        self.shop_items.extend([
            {'name': 'tofu', 'template': TEMPLATE_TOFU, 'var_name': 'tofu',
             'selection': SELECT_TOFU, 'selection_check': SELECT_TOFU_CHECK,
             'post_action': POST_TOFU},
            {'name': 'omurice', 'template': TEMPLATE_OMURICE, 'var_name': 'omurice',
             'selection': SELECT_OMURICE, 'selection_check': SELECT_OMURICE_CHECK,
             'post_action': POST_OMURICE},
            {'name': 'cabbage_tofu', 'template': TEMPLATE_CABBAGE_TOFU, 'var_name': 'cabbage_tofu',
             'selection': SELECT_CABBAGE_TOFU, 'selection_check': SELECT_CABBAGE_TOFU_CHECK,
             'post_action': POST_CABBAGE_TOFU},
            {'name': 'salad', 'template': TEMPLATE_SALAD, 'var_name': 'salad',
             'selection': SELECT_SALAD, 'selection_check': SELECT_SALAD_CHECK,
             'post_action': POST_SALAD},
            {'name': 'tofu_meat', 'template': TEMPLATE_TOFU_MEAT, 'var_name': 'tofu_meat',
             'selection': SELECT_TOFU_MEAT, 'selection_check': SELECT_TOFU_MEAT_CHECK,
             'post_action': POST_TOFU_MEAT},
            {'name': 'tofu_combo', 'template': TEMPLATE_TOFU_COMBO, 'var_name': 'tofu_combo',
             'selection': SELECT_TOFU_COMBO, 'selection_check': SELECT_TOFU_COMBO_CHECK,
             'post_action': POST_TOFU_COMBO},
            {'name': 'hearty_meal', 'template': TEMPLATE_HEARTY_MEAL, 'var_name': 'hearty_meal',
             'selection': SELECT_HEARTY_MEAL, 'selection_check': SELECT_HEARTY_MEAL_CHECK,
             'post_action': POST_HEARTY_MEAL},
            {'name': 'fish_chip', 'template': TEMPLATE_FISH_CHIP, 'var_name': 'fish_chip',
             'selection': SELECT_FISH_CHIP, 'selection_check': SELECT_FISH_CHIP_CHECK,
             'post_action': POST_FISH_CHIP},
            {'name': 'fo_tiao', 'template': TEMPLATE_FO_TIAO, 'var_name': 'fo_tiao',
             'selection': SELECT_FO_TIAO, 'selection_check': SELECT_FO_TIAO_CHECK,
             'post_action': POST_FO_TIAO},
            {'name': 'onion_fish', 'template': TEMPLATE_ONION_FISH, 'var_name': 'onion_fish',
             'selection': SELECT_ONION_FISH, 'selection_check': SELECT_ONION_FISH_CHECK,
             'post_action': POST_ONION_FISH},
        ])

        # Задаём составы наборов
        self.meal_compositions = {
            'hearty_meal': {
                'required': ['tofu', 'omurice'],
                'quantity_per': 1
            },
            'tofu_combo': {
                'required': ['cabbage_tofu', 'tofu_meat'],
                'quantity_per': 1
            }
        }

        # Особый материал: тофу (для приготовления специальных блюд)
        self.special_materials = {}

        # Задаём кнопки постов
        self.post_buttons = {
            'ISLAND_RESTAURANT_POST1': ISLAND_RESTAURANT_POST1,
            'ISLAND_RESTAURANT_POST2': ISLAND_RESTAURANT_POST2
        }

        # Задаём ресурс фильтра
        self.filter_asset = 'restaurant'

        # Задаём префиксы конфигурации
        self.setup_config(
            config_meal_prefix="IslandRestaurant_Meal",
            config_number_prefix="IslandRestaurant_MealNumber",
            config_away_cook="IslandRestaurantNextTask_AwayCook",
            config_post_number="IslandRestaurant_PostNumber"
        )

        # === Автоматическое переключение сезонных блюд ===
        # Если в Meal1~Meal8 настроено весеннее ограниченное блюдо (double_bamboo_shoots / asparagus_shrimp),
        # а текущий сезон не spring, автоматически заменяем его блюдом соответствующего слота текущего сезона
        self._auto_switch_seasonal_meals()

        # Инициализируем магазин
        self.initialize_shop()

    def _is_seasonal_priority_enabled(self):
        return getattr(self.config, 'IslandRestaurant_DoubleBambooShoots', False)

    def _get_current_seasonal_shop_items(self):
        if not hasattr(self, 'season_config') or not self.season_config:
            return []

        seasonal_items = self.season_config.get_seasonal_items('restaurant') or []
        result = []
        for item_name in seasonal_items:
            dish = RESTAURANT_SEASONAL_DISHES.get(item_name)
            if dish:
                result.append(dish.copy())
        return result

    def _get_high_priority_seasonal_dish(self):
        if not self._is_seasonal_priority_enabled():
            return None
        if not hasattr(self, 'season_config') or not self.season_config:
            return None

        seasonal_items = self.season_config.get_seasonal_items('restaurant') or []
        for item_name in seasonal_items:
            dish = HIGH_PRIORITY_SEASONAL_DISHES.get(item_name)
            if dish:
                return dish.copy()
        return None

    def _auto_switch_seasonal_meals(self):
        """
        自动切换用户配置中的春季限定餐品到当前季节对应餐品。
        """
        SEASONAL_MEAL_SWITCH = {
            'double_bamboo_shoots': 0,
            'asparagus_shrimp': 1,
        }
        if not hasattr(self, 'season_config') or not self.season_config:
            return
        current_season = self.season_config.season
        current_restaurant_items = SEASONAL_ITEMS.get(current_season, {}).get('restaurant', [])
        for spring_item, slot_idx in SEASONAL_MEAL_SWITCH.items():
            if not any(name == spring_item for name, _ in self.post_products):
                continue
            spring_name = '凉拌双笋' if spring_item == 'double_bamboo_shoots' else '芦笋炒虾仁'
            if slot_idx < len(current_restaurant_items):
                seasonal_item = current_restaurant_items[slot_idx]
                if seasonal_item != spring_item:
                    self.post_products = [
                        (seasonal_item, target) if name == spring_item else (name, target)
                        for name, target in self.post_products
                    ]
                    logger.info(
                        f"Автопереключение сезонного блюда: {spring_item}({spring_name}) -> {seasonal_item}"
                        f" (текущий сезон: {self.season_config.season_name})"
                    )
            else:
                self.post_products = [
                    (name, target) for name, target in self.post_products
                    if name != spring_item
                ]
                logger.info(
                    f"Автоудаление сезонного блюда: {spring_item}({spring_name})"
                    f" ({self.season_config.season_name}: для этого слота нет сезонного блюда)"
                )

    def select_product(self, product_selection, product_selection_check):
        """
        覆盖父类 select_product：
        高优先级季节菜品使用固定坐标点击，不进行模板匹配和滑动。
        其他餐品走父类逻辑。
        """
        if self.seasonal_dish_slot:
            dish_name = self.seasonal_dish_slot['name']
            fixed_selection = self.seasonal_dish_slot['selection']
            normal_selection = self.name_to_config.get(dish_name, {}).get('selection')
            if product_selection in (fixed_selection, normal_selection):
                self.device.click(FIXED_SELECT_DOUBLE_BAMBOO_SHOOTS)
                self.device.sleep(0.5)
                return True
        return super().select_product(product_selection, product_selection_check)

    def check_special_materials(self, product, batch_size):
        """覆盖：检查特殊材料（豆腐）限制"""
        if batch_size <= 0:
            return 0

        # cabbage_tofu требует 1 тофу
        if product == 'cabbage_tofu':
            tofu_needed_per_batch = 1
            tofu_available = self.warehouse_counts.get('tofu', 0)
            max_by_tofu = tofu_available // tofu_needed_per_batch
            return min(batch_size, max_by_tofu)

        # tofu_meat требует 2 тофу
        if product == 'tofu_meat':
            tofu_needed_per_batch = 2
            tofu_available = self.warehouse_counts.get('tofu', 0)
            max_by_tofu = tofu_available // tofu_needed_per_batch
            return min(batch_size, max_by_tofu)

        return batch_size

    def deduct_materials(self, product, number):
        """覆盖：扣除前置材料，包括豆腐"""
        # Сначала вызываем родительский метод для списания сырья наборов
        super().deduct_materials(product, number)

        # Для cabbage_tofu списываем тофу
        if product == 'cabbage_tofu':
            tofu_needed = number * 1
            if 'tofu' in self.warehouse_counts:
                self.warehouse_counts['tofu'] -= tofu_needed
                logger.info(f"[Остров — ресторан «Есть рыба»] Списан тофу: tofu -{tofu_needed} (для приготовления {product})")

        # Для tofu_meat списываем тофу
        if product == 'tofu_meat':
            tofu_needed = number * 2
            if 'tofu' in self.warehouse_counts:
                self.warehouse_counts['tofu'] -= tofu_needed
                logger.info(f"[Остров — ресторан «Есть рыба»] Списан тофу: tofu -{tofu_needed} (для приготовления {product})")

    def apply_special_material_constraints(self, requirements):
        """覆盖：根据豆腐库存调整需求，豆腐不足时自动补入生产计划"""
        result = requirements.copy()

        # Получаем запас тофу
        tofu_stock = self.warehouse_counts.get('tofu', 0)

        # Обрабатываем потребность в cabbage_tofu
        if 'cabbage_tofu' in result and result['cabbage_tofu'] > 0:
            cabbage_needed = result['cabbage_tofu']
            tofu_needed = cabbage_needed * 1  # На каждый cabbage_tofu нужен 1 тофу

            if tofu_stock < tofu_needed:
                max_cabbage = tofu_stock // 1
                deficit = cabbage_needed - max_cabbage
                result['cabbage_tofu'] = max_cabbage
                # Тофу можно производить здесь же: ограничиваем выпуск и добавляем дефицит тофу в план
                if 'tofu' in self.name_to_config:
                    result['tofu'] = result.get('tofu', 0) + deficit
                    logger.info(f"[Остров — ресторан «Есть рыба»] Недостаточно тофу: cabbage_tofu {cabbage_needed}→{max_cabbage}; добавлена потребность tofu x{deficit}")
                tofu_stock -= max_cabbage

        # Обрабатываем потребность в tofu_meat
        if 'tofu_meat' in result and result['tofu_meat'] > 0:
            tofu_meat_needed = result['tofu_meat']
            tofu_needed = tofu_meat_needed * 2  # На каждый tofu_meat нужно 2 тофу

            if tofu_stock < tofu_needed:
                max_tofu_meat = tofu_stock // 2
                deficit = tofu_meat_needed - max_tofu_meat
                result['tofu_meat'] = max_tofu_meat
                if 'tofu' in self.name_to_config:
                    result['tofu'] = result.get('tofu', 0) + deficit * 2
                    logger.info(f"[Остров — ресторан «Есть рыба»] Недостаточно тофу: tofu_meat {tofu_meat_needed}→{max_tofu_meat}; добавлена потребность tofu x{deficit * 2}")
                tofu_stock -= max_tofu_meat * 2

        return result

    def run(self):
        """
        覆盖父类的run方法，实现季节菜品的优先级控制：
          - 凉拌双笋（菜品1）：最高优先级，在基础需求前生产
          - 芦笋炒虾仁（菜品2）：最低优先级，等所有其他菜品（包括菜品1）都完成且系统空闲时才生产
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
            logger.info(f"[Остров — ресторан «Есть рыба»] === Отладочная информация ===")
            logger.info(f"[Остров — ресторан «Есть рыба»] Запасы на складе: {self.warehouse_counts}")
            logger.info(f"[Остров — ресторан «Есть рыба»] В производстве: {self.post_check_meal}")
            logger.info(f"[Остров — ресторан «Есть рыба»] Текущий общий запас: {self.current_totals}")
            logger.info(f"[Остров — ресторан «Есть рыба»] Базовые требования ({len(self.post_products)} слотов): {self.post_products}")
            logger.info("===============")

            # Сохраняем исходные запасы и восстанавливаем их при retry, чтобы обнуление max_targets не влияло на повторный расчёт
            _orig_totals = dict(self.current_totals)
            self._compute_base_demands()

            logger.info(f"[Остров — ресторан «Есть рыба»] Ожидающие приготовления: {self.to_post_products}")
            logger.info(f"[Остров — ресторан «Есть рыба»] Текущий остаток запасов: {self.current_totals}")

            # ============ Разбираем составные наборы ============
            if self.to_post_products:
                self.to_post_products = self.process_meal_requirements(self.to_post_products)
                logger.info(f"[Остров — ресторан «Есть рыба»] План производства базовых требований: {self.to_post_products}")

            # ================================================================
            # Сезонное блюдо высокого приоритета
            # Производим его отдельно перед всеми базовыми потребностями, гарантируя максимальный приоритет
            # ================================================================
            if self.seasonal_dish_slot:
                dish_name = self.seasonal_dish_slot['name']
                dish_cn = self.seasonal_dish_slot['cn_name']
                logger.info(f"[Остров — ресторан «Есть рыба»] Этап: приоритетное сезонное блюдо — {dish_cn}")

                # Извлекаем из производственного плана и назначаем отдельно
                slot1_qty = self.POST_PRODUCE_LIMIT
                if dish_name in self.to_post_products:
                    slot1_qty += self.to_post_products.pop(dish_name)

                # Временно назначаем только производство в позиции 1
                temp_products = self.to_post_products.copy()
                self.to_post_products = {dish_name: slot1_qty}
                logger.info(f"[Остров — ресторан «Есть рыба»] Отдельное производство {dish_cn}: {self.to_post_products}")

                self.schedule_production()

                # Восстанавливаем оставшийся план базовых потребностей
                self.to_post_products = temp_products
                logger.info(f"[Остров — ресторан «Есть рыба»] Оставшийся план базового производства: {self.to_post_products}")

            # ============ Назначаем производство базовых потребностей, пока есть свободные посты и дефицит ============
            _produced_pass = {}
            _force_skip_run = set()
            _loop_count = 0

            self._schedule_and_track(_produced_pass)

            while self.get_idle_posts():
                _loop_count += 1
                if _loop_count > self._MAX_FILL_LOOP:
                    logger.warning(f"[Остров — ресторан «Есть рыба»] [Цикл] Достигнут максимум итераций {self._MAX_FILL_LOOP}; принудительный выход")
                    break
                self.current_totals = dict(_orig_totals)
                for name, qty in _produced_pass.items():
                    self.current_totals[name] = self.current_totals.get(name, 0) + qty

                self._compute_base_demands(force_skip=_force_skip_run)
                if not self.to_post_products:
                    logger.info("[Остров — ресторан «Есть рыба»] Требования всех слотов удовлетворены")
                    break

                self.to_post_products = self.process_meal_requirements(self.to_post_products)
                logger.info(f"[Остров — ресторан «Есть рыба»] План производства базовых требований: {self.to_post_products}")

                prev_pass_total = sum(_produced_pass.values())
                self._schedule_and_track(_produced_pass)

                if sum(_produced_pass.values()) == prev_pass_total and self.to_post_products:
                    logger.info("[Остров — ресторан «Есть рыба»] [Цикл] Не удалось распределить текущий дефицит; переход к строгому сканированию")
                    self.to_post_products = {}
                    self.current_totals = dict(_orig_totals)
                    for name, qty in _produced_pass.items():
                        self.current_totals[name] = self.current_totals.get(name, 0) + qty
                    self._compute_base_demands(check_materials=True)
                    if not self.to_post_products:
                        break
                    self.to_post_products = self.process_meal_requirements(self.to_post_products)
                    logger.info(f"[Остров — ресторан «Есть рыба»] План базового производства (строгий режим): {self.to_post_products}")

                    strict_prev_total = sum(_produced_pass.values())
                    self._schedule_and_track(_produced_pass)

                    if sum(_produced_pass.values()) == strict_prev_total and self.to_post_products:
                        stuck_now = set(self.to_post_products.keys())
                        logger.info(f"[Остров — ресторан «Есть рыба»] [Цикл] Строгий режим также ничего не произвёл; принудительный пропуск: {stuck_now}")
                        _force_skip_run.update(stuck_now)
                        self.to_post_products = {}
                    continue

            # ============ Проверяем оставшиеся свободные посты и назначаем постоянное блюдо ============
            idle_posts_after_basic = self.get_idle_posts()

            # Получаем конфигурацию постоянного блюда; special_food больше не используется, поскольку сезонные блюда управляются отдельно
            away_cook = getattr(self.config, self.config_away_cook, None)

            # Проверяем, что постоянное блюдо имеет допустимое значение
            has_away_cook = (away_cook and away_cook != "None" and
                             away_cook in self.name_to_config)

            if idle_posts_after_basic and has_away_cook:
                logger.info(f"[Остров — ресторан «Есть рыба»] После базовых требований осталось свободных позиций: {len(idle_posts_after_basic)}")

                for post_id in idle_posts_after_basic:
                    post_num = post_id[-1]
                    time_var_name = f'{self.time_prefix}{post_num}'

                    logger.info(f"[Остров — ресторан «Есть рыба»] Попытка произвести постоянное блюдо {away_cook}")

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
                            logger.info(f"[Остров — ресторан «Есть рыба»] Недостаточно ингредиентов для постоянного блюда {away_cook}; позиция остаётся свободной")
                            break
                        else:
                            logger.info(f"[Остров — ресторан «Есть рыба»] Для позиции {post_id} назначено постоянное блюдо {away_cook} x{batch_size}")
                    else:
                        logger.info(f"[Остров — ресторан «Есть рыба»] Недостаточно материалов для {away_cook}; позиция {post_id} пропущена")
                        break

            elif idle_posts_after_basic:
                logger.info(f"[Остров — ресторан «Есть рыба»] Есть свободные позиции ({len(idle_posts_after_basic)}), но постоянное блюдо не задано; позиции остаются свободными")

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

    def test(self):
        chef_config = getattr(self.config, "IslandRestaurant_Chef", "WorkerJuu")
        logger.info(chef_config)


if __name__ == "__main__":
    az = IslandRestaurant('alas', task='Alas')
    az.device.screenshot()
    az.test()
