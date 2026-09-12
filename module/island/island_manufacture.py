"""岛屿制造工坊模块。

继承 IslandShopBase，实现制造工坊的产品配置与岗位管理。
支持固定位置按钮（如荠菜）、滑动配置及制造时间前缀设置，管理工坊自动化生产流程。
"""
from module.island.island import *
from module.island_manufacture.assets import *
from module.island.island_shop_base import IslandShopBase
from module.island.assets import *
from module.ui.page import *
from datetime import timedelta

from module.config.time_source import now as current_time
from module.logger import logger
from module.base.button import Button


# Кнопка с фиксированной позицией — позиция shepherd_purse в интерфейсе выбора продукта без прокрутки
FIXED_SELECT_SHEPHERD_PURSE = Button(
    area=(), color=(), button=(224, 151, 274, 209),
    file={'cn': '', 'en': '', 'jp': '', 'tw': ''}
)


class IslandManufacture(IslandShopBase):
    def __init__(self, *args, **kwargs):
        # Сначала инициализируем базовый класс
        IslandShopBase.__init__(self, *args, **kwargs)

        # Задаём тип магазина
        self.shop_type = "manufacture"
        self.time_prefix = "time_manufacture"

        # Задаём конфигурацию прокрутки (в управлении постами нужны две прокрутки)
        self.post_manage_swipe_count = 2

        # === Инициализируем глобальную сезонную конфигурацию ===
        self._init_season_config()

        # Задаём ресурс фильтра
        self.filter_asset = 'factory'

        # Конфигурация продуктов производства
        self.manufacture = {
            'wood_processing': {
                'items': [
                    {'name': 'file_cabinet', 'template': TEMPLATE_FILE_CABINET,
                     'var_name': 'file_cabinet', 'selection': SELECT_FILE_CABINET,
                     'selection_check': SELECT_FILE_CABINET_CHECK, 'post_action': POST_FILE_CABINET},
                ]
            },
            # TEMPLATE_FILTER_ELEMENT не добавлен
            'electronic_processing': {
                'items': [
                    {'name': 'filter_element', 'template': TEMPLATE_FILE_CABINET,
                     'var_name': 'filter_element', 'selection': SELECT_FILTER_ELEMENT,
                     'selection_check': SELECT_FILTER_ELEMENT_CHECK, 'post_action': POST_FILTER_ELEMENT},
                ]
            },
            'industrial_production': {
                'items': [
                    {'name': 'iron_nail', 'template': TEMPLATE_IRON_NAIL,
                     'var_name': 'iron_nail', 'selection': SELECT_IRON_NAIL,
                     'selection_check': SELECT_IRON_NAIL_CHECK, 'post_action': POST_IRON_NAIL},
                    {'name': 'cutlery', 'template': TEMPLATE_CUTLERY,
                     'var_name': 'cutlery', 'selection': SELECT_CUTLERY,
                     'selection_check': SELECT_CUTLERY_CHECK, 'post_action': POST_CUTLERY},
                ]
            },
            'handmade': {
                'items': [
                    {'name': 'leather', 'template': TEMPLATE_LEATHER,
                     'var_name': 'leather', 'selection': SELECT_LEATHER,
                     'selection_check': SELECT_LEATHER_CHECK, 'post_action': POST_LEATHER},
                    {'name': 'boot', 'template': TEMPLATE_BOOT,
                     'var_name': 'boot', 'selection': SELECT_BOOT,
                     'selection_check': SELECT_BOOT_CHECK, 'post_action': POST_BOOT},
                    {'name': 'peanut_oil', 'template': TEMPLATE_PEANUT_OIL,
                     'var_name': 'peanut_oil', 'selection': SELECT_PEANUT_OIL,
                     'selection_check': SELECT_PEANUT_OIL_CHECK, 'post_action': POST_PEANUT_OIL},
                ]
            }
        }
        # Сезонное ограничение: продукты ручного производства (сушёный shepherd_purse и т. п.)
        if self.is_seasonal_item_enabled('shepherd_purse'):
            self.manufacture['handmade']['items'].append(
                {'name': 'shepherd_purse', 'template': TEMPLATE_SHEPHERD_PURSE,
                 'var_name': 'shepherd_purse', 'selection': FIXED_SELECT_SHEPHERD_PURSE,
                 'selection_check': FIXED_SELECT_SHEPHERD_PURSE, 'post_action': POST_SHEPHERD_PURSE}
            )
            logger.info("[Остров — производство] Сезонный товар shepherd_purse добавлен в список ручного производства")

        # Инициализируем кнопки постов по конфигурации
        self.post_buttons = self._init_post_buttons()

        # Разворачиваем все продукты в один список для использования базовым классом
        self.shop_items = []
        for category in self.manufacture.values():
            self.shop_items.extend(category['items'])

        # Инициализируем список потребностей (производству не нужны потребности из внешней конфигурации)
        self.post_products = []

        # Задаём конфигурацию (4 параметра, конфигурация задач удалена)
        self.setup_config(
            config_meal_prefix="IslandManufacture_Meal",
            config_number_prefix="IslandManufacture_MealNumber",
            config_away_cook="IslandManufactureNextTask_AwayCook",
            config_post_number="IslandManufacture_PostNumber"
        )

        # Инициализируем магазин
        self.initialize_shop()

    def _init_post_buttons(self):
        """根据配置初始化岗位按钮"""
        post_buttons = {}
        if self.config.WoodProcessing_Positions >= 1:
            post_buttons['ISLAND_WOOD_PROCESSING_POST1'] = ISLAND_WOOD_PROCESSING_POST1
        if self.config.WoodProcessing_Positions >= 2:
            post_buttons['ISLAND_WOOD_PROCESSING_POST2'] = ISLAND_WOOD_PROCESSING_POST2

        if self.config.ElectronicProcessing_Positions >= 1:
            post_buttons['ISLAND_ELECTRONIC_PROCESSING_POST1'] = ISLAND_ELECTRONIC_PROCESSING_POST1
        if self.config.ElectronicProcessing_Positions >= 2:
            post_buttons['ISLAND_ELECTRONIC_PROCESSING_POST2'] = ISLAND_ELECTRONIC_PROCESSING_POST2

        if self.config.Industrial_Positions >= 1:
            post_buttons['ISLAND_INDUSTRIAL_POST1'] = ISLAND_INDUSTRIAL_POST1
        if self.config.Industrial_Positions >= 2:
            post_buttons['ISLAND_INDUSTRIAL_POST2'] = ISLAND_INDUSTRIAL_POST2

        if self.config.Handmade_Positions >= 1:
            post_buttons['ISLAND_HANDMADE_POST1'] = ISLAND_HANDMADE_POST1
        if self.config.Handmade_Positions >= 2:
            post_buttons['ISLAND_HANDMADE_POST2'] = ISLAND_HANDMADE_POST2

        return post_buttons

    def get_idle_posts_by_category(self, category):
        """获取指定类别的空闲岗位ID列表"""
        category_posts = []
        if category == 'wood_processing':
            category_posts = ['ISLAND_WOOD_PROCESSING_POST1', 'ISLAND_WOOD_PROCESSING_POST2']
        elif category == 'electronic_processing':
            category_posts = ['ISLAND_ELECTRONIC_PROCESSING_POST1', 'ISLAND_ELECTRONIC_PROCESSING_POST2']
        elif category == 'industrial_production':
            category_posts = ['ISLAND_INDUSTRIAL_POST1', 'ISLAND_INDUSTRIAL_POST2']
        elif category == 'handmade':
            category_posts = ['ISLAND_HANDMADE_POST1', 'ISLAND_HANDMADE_POST2']

        # Возвращаем только реально существующие свободные посты
        return [post_id for post_id in category_posts
                if post_id in self.posts and self.posts[post_id]['status'] == 'idle']

    def select_product(self, product_selection, product_selection_check):
        """
        覆盖父类 select_product：
        荠菜使用固定坐标点击，不进行模板匹配和滑动。
        其他产品走父类逻辑（模板匹配 + 滑动查找）。
        """
        # shepherd_purse → нажимаем непосредственно по фиксированной позиции
        if product_selection == FIXED_SELECT_SHEPHERD_PURSE:
            self.device.click(FIXED_SELECT_SHEPHERD_PURSE)
            self.device.sleep(0.5)
            return True

        # Для остальных продуктов используем родительскую логику
        return super().select_product(product_selection, product_selection_check)

    def select_product_with_material_check(self, post_id, product_list):
        """选择产品并检查材料是否充足（覆盖基类方法）"""
        post_button = self.posts[post_id]['button']

        # Открываем пост
        self.post_close()
        self.post_open(post_button)
        self.device.sleep(0.5)

        while True:
            self.device.screenshot()
            if self.appear_then_click(ISLAND_POST_SELECT, offset=1):
                self.device.sleep(0.5)
                continue
            if self.appear(ISLAND_SELECT_CHARACTER_CHECK, offset=1):
                if self.select_character():
                    if not self.confirm_selected_character(f"{post_id} — производственная отправка"):
                        self.back_to_postmanage_from_dispatch()
                        return None
                else:
                    logger.warning(f"[Остров — производство] Для производственной отправки {post_id} нет доступных персонажей")
                    self.back_to_postmanage_from_dispatch()
                    return None
                continue
            if self.appear(ISLAND_SELECT_PRODUCT_CHECK, offset=1):
                selected_product = None
                for product_info in product_list:
                    product_name = product_info['name']
                    selection = product_info['selection']
                    selection_check = product_info['selection_check']
                    logger.info(f"[Остров — производство] Попытка выбрать товар: {product_name}")

                    # Нажимаем кнопку выбора продукта
                    self.select_product(selection, selection_check)
                    self.device.sleep(0.5)

                    # Проверяем состояние кнопки подтверждения
                    image = self.device.screenshot()
                    area = (493, 597, 621, 643)
                    color = get_color(image, area)

                    # Если кнопка подтверждения серая (153, 156, 156), материалов недостаточно
                    if color_similar(color, (153, 156, 156), 80):
                        logger.info(f"[Остров — производство] Недостаточно материалов; товар пропущен: {product_name}")
                        continue
                    else:
                        selected_product = product_info
                        # Устанавливаем максимальное количество производства
                        self.appear_then_click(POST_MAX)
                        # Подтверждаем производство
                        self.device.click(POST_ADD_ORDER)
                        logger.info(f"[Остров — производство] Товар успешно выбран: {product_name}")
                        break  # Выходим из цикла выбора продукта

                if not selected_product:
                    logger.info("[Остров — производство] Материалов недостаточно для всех товаров; возврат")
                    self.device.click(SELECT_UI_BACK)
                    self.device.sleep(0.3)

                    # Очищаем переменную времени этого поста
                    post_num = None
                    for post_key, post_info in self.posts.items():
                        if post_info['button'] == post_button:
                            # Извлекаем номер поста
                            if 'POST1' in post_key:
                                post_num = 1
                            elif 'POST2' in post_key:
                                post_num = 2
                            break

                    if post_num is not None:
                        time_var_name = f'{self.time_prefix}{post_num}'
                        if hasattr(self, time_var_name):
                            setattr(self, time_var_name, None)
                            logger.info(f"[Остров — производство] Очищена переменная времени позиции: {time_var_name}")

                self.wait_until_appear(ISLAND_POSTMANAGE_CHECK)
                self.device.sleep(0.5)
                self.post_close()

                for _ in range(self.post_manage_swipe_count):
                    self.post_manage_up_swipe(450)

                if selected_product:
                    self.post_open(post_button)
                    # Получаем время и количество производства
                    image = self.device.screenshot()
                    ocr_post_number = Digit(OCR_POST_NUMBER, letter=(57, 58, 60), threshold=100,
                                            alphabet='0123456789')
                    actual_number = ocr_post_number.ocr(image)
                    time_work = Duration(ISLAND_WORKING_TIME)
                    time_value = time_work.ocr(self.device.image)
                    finish_time = current_time() + time_value

                    # Задаём переменную времени
                    # Извлекаем число из post_id
                    import re
                    match = re.search(r'POST(\d+)', post_id)
                    if match:
                        post_num = match.group(1)
                        time_var_name = f'{self.time_prefix}{post_num}'
                        setattr(self, time_var_name, finish_time)

                    self.posts[post_id]['status'] = 'working'

                    logger.info(f"[Остров — производство] Производство запланировано: {selected_product['name']} x{actual_number}")
                    self.post_close()
                    return selected_product

                break  # Выходим из цикла

        return None  # В штатном режиме выполнение сюда не доходит

    def schedule_manufacture(self):
        """安排制造业生产（覆盖基类方法）"""
        self.schedule_wood_processing()

        self.schedule_electronic_processing()

        self.schedule_industrial_production()

        self.schedule_handmade()

    def schedule_wood_processing(self):
        """安排木料加工生产"""
        idle_posts = self.get_idle_posts_by_category('wood_processing')
        if not idle_posts:
            return
        # В обработке древесины производится только file_cabinet
        product_list = self.manufacture['wood_processing']['items']
        for post_id in idle_posts:
            self.select_product_with_material_check(post_id, product_list)

    def schedule_electronic_processing(self):
        """安排电子加工生产"""
        idle_posts = self.get_idle_posts_by_category('electronic_processing')
        if not idle_posts:
            return
        # В обработке древесины производится только file_cabinet
        product_list = self.manufacture['electronic_processing']['items']
        for post_id in idle_posts:
            self.select_product_with_material_check(post_id, product_list)

    def schedule_industrial_production(self):
        """安排工业生产"""
        idle_posts = self.get_idle_posts_by_category('industrial_production')
        if not idle_posts:
            return
        # Проверяем запас iron_nail
        iron_nail_stock = self.warehouse_counts.get('iron_nail', 0)
        # Выбираем продукт по правилу
        if iron_nail_stock >= 20:
            product_list = [item for item in self.manufacture['industrial_production']['items']
                            if item['name'] == 'cutlery']
        else:
            product_list = [item for item in self.manufacture['industrial_production']['items']
                            if item['name'] == 'iron_nail']

        for post_id in idle_posts:
            self.select_product_with_material_check(post_id, product_list)

    def schedule_handmade(self):
        """安排手工生产"""
        idle_posts = self.get_idle_posts_by_category('handmade')
        if not idle_posts:
            return
        # Проверяем запас leather
        leather_stock = self.warehouse_counts.get('leather', 0)
        # Формируем список выбора продуктов по приоритету
        product_list = []
        # В первую очередь производим shepherd_purse
        shepherd_purse_item = [item for item in self.manufacture['handmade']['items']
                           if item['name'] == 'shepherd_purse'][0]
        product_list.append(shepherd_purse_item)

        # Если запас leather >= 10, производим boot
        if leather_stock >= 10:
            boot_item = [item for item in self.manufacture['handmade']['items']
                         if item['name'] == 'boot'][0]
            product_list.append(boot_item)

        # В последнюю очередь производим leather
        leather_item = [item for item in self.manufacture['handmade']['items']
                        if item['name'] == 'leather'][0]
        product_list.append(leather_item)

        for post_id in idle_posts:
            self.select_product_with_material_check(post_id, product_list)

    def run(self):
        """运行制造业逻辑（完全覆盖基类方法）"""
        self.island_error = False

        # Шаг 1: проверяем состояние постов
        self.goto_postmanage()
        self.post_manage_mode(POST_MANAGE_PRODUCTION)
        self.post_close()

        # Прокручиваем, чтобы увидеть посты
        for _ in range(self.post_manage_swipe_count):
            self.post_manage_up_swipe(450)

        # Проверяем состояние постов
        time_vars = []
        post_index = 1

        # Последовательно проверяем все посты
        for post_id in self.post_buttons.keys():
            time_var_name = f'{self.time_prefix}{post_index}'
            time_vars.append(time_var_name)
            setattr(self, time_var_name, None)
            self.post_check(post_id, time_var_name)
            post_index += 1


        # Проверяем, есть ли задачи для планирования
        idle_posts = self.get_idle_posts()
        if idle_posts:
            self.get_warehouse_counts()
            # При наличии свободных постов повторно открываем управление постами и планируем производство
            logger.info(f"[Остров — производство] Свободных позиций: {len(idle_posts)}; начинается планирование производства")

            # Повторно открываем управление постами
            self.goto_postmanage()
            self.post_manage_mode(POST_MANAGE_PRODUCTION)
            self.post_close()

            # Прокручиваем, чтобы увидеть посты
            for _ in range(self.post_manage_swipe_count):
                self.post_manage_up_swipe(450)

            # Планируем производство
            self.schedule_manufacture()
        else:
            logger.info("[Остров — производство] Свободных позиций нет; планирование производства пропущено")

        # Задаём задержку задачи
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
            raise GameBugError("Обнаружен Island ERROR1; требуется перезапуск")

    # Следующие методы переопределены для совместимости с базовым классом
    def process_meal_requirements(self, source_products):
        """制造业不需要处理套餐需求"""
        return source_products

    def schedule_production(self):
        """覆盖：制造业使用自己的生产调度"""
        self.schedule_manufacture()

    def process_away_cook(self):
        """覆盖：制造业不需要常驻餐品模式"""
        # Производство использует собственные правила и не зависит от режима постоянного блюда
        self.to_post_products = {}
        logger.info("[Остров — производство] Используются встроенные правила производства; постоянные блюда не задаются")

    def get_max_producible(self, product, requested_quantity, skip_zero_materials=False):
        """覆盖：制造业的生产数量由材料检查决定"""
        return requested_quantity

    def check_special_materials(self, product, batch_size):
        """覆盖：制造业没有特殊材料检查"""
        return batch_size

    def apply_special_material_constraints(self, requirements):
        """覆盖：制造业没有特殊材料限制"""
        return requirements

    def test(self):
        if self.config.Industrial_Positions > 1:
            logger.info(2)


if __name__ == "__main__":
    az = IslandManufacture('alas', task='Alas')
    az.device.screenshot()
    az.run()