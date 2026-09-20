"""岛屿 JUU 咖啡店模块。

继承 IslandShopBase，配置咖啡店的商品列表与岗位参数。
包含冰咖啡、蛋卷、拿铁、柑橘咖啡等饮品的模板、选择与制作流程定义。
"""
from module.island_juu_coffee.assets import *
from module.island.island_shop_base import IslandShopBase
from module.island.assets import *
from module.ui.page import *
from module.logger import logger


class IslandJuuCoffee(IslandShopBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Задаём тип магазина
        self.shop_type = "juu_coffee"
        self.time_prefix = "time_coffee"
        self.chef_config = self.config.IslandJuuCoffee_ChefFilter
        self.special_character = self.config.IslandJuuCoffee_Friedrich

        # Задаём список товаров
        self.shop_items = [
            {'name': 'iced_coffee', 'template': TEMPLATE_ICED_COFFEE, 'var_name': 'iced_coffee',
             'selection': SELECT_ICED_COFFEE, 'selection_check': SELECT_ICED_COFFEE_CHECK,
             'post_action': POST_ICED_COFFEE},
            {'name': 'omelette', 'template': TEMPLATE_OMELETTE, 'var_name': 'omelette',
             'selection': SELECT_OMELETTE, 'selection_check': SELECT_OMELETTE_CHECK,
             'post_action': POST_OMELETTE},
            {'name': 'latte', 'template': TEMPLATE_LATTE, 'var_name': 'latte',
             'selection': SELECT_LATTE, 'selection_check': SELECT_LATTE_CHECK,
             'post_action': POST_LATTE},
            {'name': 'citrus_coffee', 'template': TEMPLATE_CITRUS_COFFEE, 'var_name': 'citrus_coffee',
             'selection': SELECT_CITRUS_COFFEE, 'selection_check': SELECT_CITRUS_COFFEE_CHECK,
             'post_action': POST_CITRUS_COFFEE},
            {'name': 'strawberry_milkshake', 'template': TEMPLATE_STRAWBERRY_MILKSHAKE,
             'var_name': 'strawberry_milkshake',
             'selection': SELECT_STRAWBERRY_MILKSHAKE, 'selection_check': SELECT_STRAWBERRY_MILKSHAKE_CHECK,
             'post_action': POST_STRAWBERRY_MILKSHAKE},
            {'name': 'morning_light', 'template': TEMPLATE_MORNING_LIGHT, 'var_name': 'morning_light',
             'selection': SELECT_MORNING_LIGHT, 'selection_check': SELECT_MORNING_LIGHT_CHECK,
             'post_action': POST_MORNING_LIGHT},
            {'name': 'wake_up_call', 'template': TEMPLATE_WAKE_UP_CALL, 'var_name': 'wake_up_call',
             'selection': SELECT_WAKE_UP_CALL, 'selection_check': SELECT_WAKE_UP_CALL_CHECK,
             'post_action': POST_WAKE_UP_CALL},
            {'name': 'fruity_fruitier', 'template': TEMPLATE_FRUITY_FRUITIER, 'var_name': 'fruity_fruitier',
             'selection': SELECT_FRUITY_FRUITIER, 'selection_check': SELECT_FRUITY_FRUITIER_CHECK,
             'post_action': POST_FRUITY_FRUITIER},
            {'name': 'cheese', 'template': TEMPLATE_CHEESE, 'var_name': 'cheese',
             'selection': SELECT_CHEESE, 'selection_check': SELECT_CHEESE_CHECK,
             'post_action': POST_CHEESE},
        ]

        # Задаём составы наборов
        self.meal_compositions = {
            'morning_light': {
                'required': ['latte', 'omelette'],
                'quantity_per': 1
            },
            'wake_up_call': {
                'required': ['cheese', 'iced_coffee'],
                'quantity_per': 1
            },
            'fruity_fruitier': {
                'required': ['citrus_coffee', 'strawberry_milkshake'],
                'quantity_per': 1
            }
        }

        # Задаём кнопки постов
        self.post_buttons = {
            'ISLAND_JUU_COFFEE_POST1': ISLAND_JUU_COFFEE_POST1,
            'ISLAND_JUU_COFFEE_POST2': ISLAND_JUU_COFFEE_POST2
        }

        # Задаём ресурс фильтра
        self.filter_asset = 'juu_coffee'

        # Задаём префиксы конфигурации
        self.setup_config(
            config_meal_prefix="IslandJuuCoffee_Meal",
            config_number_prefix="IslandJuuCoffee_MealNumber",
            config_away_cook="IslandJuuCoffeeNextTask_AwayCook",
            config_post_number="IslandJuuCoffee_PostNumber"
        )

        # Задаём число прокруток (для JuuCoffee требуется две прокрутки)
        self.post_manage_swipe_count = 2  # В методе run выполняются две прокрутки по 450

        # Особый материал: молоко
        self.milk_stock = 0
        self.special_materials = {'milk': 0}

        # Инициализируем магазин
        self.initialize_shop()

    def get_warehouse_counts(self):
        """覆盖：获取仓库数量，包括牛奶"""
        # Сначала вызываем родительский метод для получения базовых запасов
        super().get_warehouse_counts()

        # Дополнительно получаем количество milk с ранчо
        self.warehouse_filter('ranch')
        image = self.device.screenshot()
        self.milk_stock = self.ocr_item_quantity(image, TEMPLATE_MILK)
        self.special_materials['milk'] = self.milk_stock
        logger.info(f"[Остров — Juu Coffee] Количество milk: {self.milk_stock}")

        # Сохраняем запас молока в warehouse_counts для унифицированной обработки
        self.warehouse_counts['milk'] = self.milk_stock

        return self.warehouse_counts

    def check_special_materials(self, product, batch_size):
        """覆盖：检查特殊材料（牛奶）限制"""
        if batch_size <= 0:
            return 0

        # Для latte требуется 2 единицы молока
        elif product == 'latte':
            milk_needed_per_batch = 2
            milk_available = self.milk_stock
            max_by_milk = milk_available // milk_needed_per_batch
            batch_size = min(batch_size, max_by_milk)
            logger.info(f"[Остров — Juu Coffee] {product}: ограничение milk — доступно {milk_available}, на партию {milk_needed_per_batch}, максимум {max_by_milk}")

        # Для strawberry_milkshake требуется 1 единица молока
        elif product == 'strawberry_milkshake':
            milk_needed_per_batch = 1
            milk_available = self.milk_stock
            max_by_milk = milk_available // milk_needed_per_batch
            batch_size = min(batch_size, max_by_milk)
            logger.info(f"[Остров — Juu Coffee] {product}: ограничение milk — доступно {milk_available}, на партию {milk_needed_per_batch}, максимум {max_by_milk}")

        # Для cheese требуется 8 единиц молока
        if product == 'cheese':
            milk_needed_per_batch = 8
            milk_available = self.milk_stock
            max_by_milk = milk_available // milk_needed_per_batch
            batch_size = min(batch_size, max_by_milk)
            logger.info(f"[Остров — Juu Coffee] {product}: ограничение milk — доступно {milk_available}, на партию {milk_needed_per_batch}, максимум {max_by_milk}")
        return batch_size

    def _is_friedrich_available(self):
        """
        只读检查大帝(Friedrich)是否可用于生产（空闲且有体力），不进行任何选择操作。
        仅匹配 Friedrich 的模板，不做全量角色扫描。
        """
        screenshot = self.device.screenshot()
        target_characters = self.recognize_target_characters(screenshot, ["Friedrich"])
        for char_info in target_characters:
            if char_info["character_name"] == "Friedrich":
                available = not char_info["is_working"] and char_info["has_stamina"]
                logger.info(f"[Остров — Juu Coffee] Friedrich: working={char_info['is_working']}, stamina={char_info['has_stamina']}, доступен={available}")
                return available
        logger.info("[Остров — Juu Coffee] Friedrich отсутствует в списке персонажей")
        return False

    def post_produce(self, post_id, product, number, time_var_name, product2=None):
        """
        覆盖父类 post_produce：
        醒神套餐(wake_up_call)若大帝(Friedrich)不可用则跳过烹饪。
        """
        if product == 'wake_up_call' and self.special_character:
            post_button = self.posts[post_id]['button']
            self.post_close()
            self.post_open(post_button)
            self.device.sleep(0.5)
            while 1:
                self.device.screenshot()
                if self.appear(ISLAND_SELECT_CHARACTER_CHECK, offset=1):
                    break
                if self.appear_then_click(ISLAND_POST_SELECT, offset=1):
                    self.device.sleep(0.5)
                    continue
            if not self._is_friedrich_available():
                logger.warning(f"[Остров — Juu Coffee] Для wake_up_call ({product}) требуется Friedrich, но он недоступен; производство пропущено")
                self.device.click(ISLAND_BACK)
                self.device.sleep(0.5)
                self.post_close()
                return 0
            self.device.click(ISLAND_BACK)
            self.device.sleep(0.5)
            self.post_close()
        return super().post_produce(post_id, product, number, time_var_name, product2)

    def select_special_character(self,product):
        if product in ['cheese','wake_up_call',]:
            return self.select_character("Friedrich")
        else:
            return self.select_character(self.chef_config)
    def deduct_materials(self, product, number):
        """覆盖：扣除前置材料，包括牛奶和套餐原材料"""
        # Сначала вызываем родительский метод для списания сырья наборов
        super().deduct_materials(product, number)

        # Для latte списываем молоко
        if product == 'latte':
            milk_needed = number * 2
            self.milk_stock = max(0, self.milk_stock - milk_needed)
            self.special_materials['milk'] = self.milk_stock
            if 'milk' in self.warehouse_counts:
                self.warehouse_counts['milk'] = self.milk_stock
            logger.info(f"[Остров — Juu Coffee] Списание milk: milk -{milk_needed} (для производства {product})")

        # Для strawberry_milkshake списываем молоко
        elif product == 'strawberry_milkshake':
            milk_needed = number * 1
            self.milk_stock = max(0, self.milk_stock - milk_needed)
            self.special_materials['milk'] = self.milk_stock
            if 'milk' in self.warehouse_counts:
                self.warehouse_counts['milk'] = self.milk_stock
            logger.info(f"[Остров — Juu Coffee] Списание milk: milk -{milk_needed} (для производства {product})")
        # Для cheese списываем молоко
        elif product == 'cheese':
            milk_needed = number * 8
            self.milk_stock = max(0, self.milk_stock - milk_needed)
            self.special_materials['milk'] = self.milk_stock
            if 'milk' in self.warehouse_counts:
                self.warehouse_counts['milk'] = self.milk_stock
            logger.info(f"[Остров — Juu Coffee] Списание milk: milk -{milk_needed} (для производства {product})")

    def apply_special_material_constraints(self, requirements):
        """覆盖：根据牛奶库存调整需求"""
        result = requirements.copy()

        # Вычисляем общую потребность всех продуктов в молоке
        milk_demand = 0

        # Потребность latte: 2 единицы молока на штуку
        if 'latte' in result and result['latte'] > 0:
            milk_demand += result['latte'] * 2

        # Потребность strawberry_milkshake: 1 единица молока на штуку
        if 'strawberry_milkshake' in result and result['strawberry_milkshake'] > 0:
            milk_demand += result['strawberry_milkshake'] * 1

        # Потребность cheese: 8 единиц молока на штуку
        if 'cheese' in result and result['cheese'] > 0:
            milk_demand += result['cheese'] * 8

        # Проверяем, достаточно ли молока
        milk_available = self.milk_stock

        if milk_demand > milk_available:
            logger.info(f"[Остров — Juu Coffee] Недостаточно milk: общая потребность {milk_demand}, доступно {milk_available}")

            # Корректируем потребности по приоритету
            # Например: сначала latte, затем strawberry_milkshake, в последнюю очередь cheese
            remaining_milk = milk_available

            # Корректируем потребность latte
            if 'latte' in result and result['latte'] > 0:
                latte_needed = result['latte']
                milk_for_latte = latte_needed * 2

                if remaining_milk < milk_for_latte:
                    max_latte = remaining_milk // 2
                    result['latte'] = max_latte
                    logger.info(f"[Остров — Juu Coffee] Недостаточно milk для latte: потребность скорректирована с {latte_needed} до {max_latte}, остаток milk с {remaining_milk} до {remaining_milk - max_latte * 2}")
                    remaining_milk -= max_latte * 2
                else:
                    remaining_milk -= milk_for_latte
                    logger.info(f"[Остров — Juu Coffee] Milk достаточно для latte: списано {milk_for_latte}, остаток {remaining_milk}")
                    

            # Корректируем потребность strawberry_milkshake
            if 'strawberry_milkshake' in result and result['strawberry_milkshake'] > 0:
                milkshake_needed = result['strawberry_milkshake']
                milk_for_milkshake = milkshake_needed * 1

                if remaining_milk < milk_for_milkshake:
                    max_milkshake = remaining_milk // 1
                    result['strawberry_milkshake'] = max_milkshake
                    logger.info(f"[Остров — Juu Coffee] Недостаточно milk для strawberry_milkshake: потребность скорректирована с {milkshake_needed} до {max_milkshake}, остаток milk с {remaining_milk} до {remaining_milk - max_milkshake * 1}")
                    remaining_milk -= max_milkshake * 1
                else:
                    remaining_milk -= milk_for_milkshake
                    logger.info(f"[Остров — Juu Coffee] Milk достаточно для strawberry_milkshake: списано {milk_for_milkshake}, остаток {remaining_milk}")

            # Корректируем потребность cheese
            if 'cheese' in result and result['cheese'] > 0:
                cheese_needed = result['cheese']
                milk_for_cheese = cheese_needed * 8

                if remaining_milk < milk_for_cheese:
                    max_cheese = remaining_milk // 8
                    result['cheese'] = max_cheese
                    logger.info(f"[Остров — Juu Coffee] Недостаточно milk для cheese: потребность скорректирована с {cheese_needed} до {max_cheese}, остаток milk с {remaining_milk} до {remaining_milk - max_cheese * 8}")
                    remaining_milk -= max_cheese * 8
                else:
                    remaining_milk -= milk_for_cheese
                    logger.info(f"[Остров — Juu Coffee] Milk достаточно для cheese: списано {milk_for_cheese}, остаток {remaining_milk}")

        return result

    def process_meal_requirements(self, source_products):
        """覆盖：处理套餐需求，添加调试信息"""
        logger.info(f"=== IslandJuuCoffee: обработка требований к блюдам ===")
        logger.info(f"[Остров — Juu Coffee] Входные потребности: {source_products}")

        # Вызываем родительский метод
        result = super().process_meal_requirements(source_products)

        logger.info(f"[Остров — Juu Coffee] Результат: {result}")
        logger.info(f"[Остров — Juu Coffee] === Завершение IslandJuuCoffee.process_meal_requirements ===")

        return result


if __name__ == "__main__":
    az = IslandJuuCoffee('alas', task='Alas')
    az.device.screenshot()
    az.run()
