"""Модуль магазинов Operation Siren.

Обеспечивает автоматические покупки в магазинах Operation Siren (Azur Lane), включая:
- сканирование, фильтрацию и пакетную покупку в портовых магазинах (Port Shop);
- взаимодействие и покупку в магазинах Акаши (Akashi Shop) на карте;
- интеллектуальный расчёт количества покупки (с учётом баланса валюты и лимитов);
- управление балансом желтых и фиолетовых монет с контролем резерва;
- обработку диалогов подтверждения покупки и выбора количества;
- адаптацию стратегии валюты под цикл сброса Operation Siren;
- фиксацию покупки очков действия у Акаши в режиме прокачки коррозии 1.

Модуль объединяет функции PortShop и AkashiShop, предоставляя единый интерфейс покупок.
"""
from module.application.errors import StorageError
from module.base.decorator import cached_property
from module.base.timer import Timer
from module.combat.assets import GET_ITEMS_1
from module.config.utils import get_os_reset_remain
from module.exception import ScriptError
from module.logger import logger
from module.os_shop.akashi_shop import AkashiShop
from module.os_shop.assets import PORT_SUPPLY_CHECK, SHOP_BUY_CONFIRM
from module.os_shop.port_shop import PortShop
from module.os_shop.ui import OS_SHOP_SCROLL
from module.shop.assets import AMOUNT_MAX, AMOUNT_MINUS, AMOUNT_PLUS, SHOP_BUY_CONFIRM_AMOUNT, SHOP_BUY_CONFIRM as OS_SHOP_BUY_CONFIRM, SHOP_CLICK_SAFE_AREA
from module.shop.clerk import OCR_SHOP_AMOUNT


class OSShop(PortShop, AkashiShop):
    """Исполнитель покупок в магазинах Operation Siren.

    Наследует функциональность портовых магазинов (PortShop) и магазина Акаши (AkashiShop),
    предоставляя единый интерфейс исполнения покупок и стратегию управления валютой.

    Основные возможности:
    - исполнение покупки отдельного предмета (с подтверждениями, выбором количества и повторами);
    - цикл пакетной покупки предметов;
    - интеллектуальный расчёт количества (по балансу, складу и резерву);
    - расчёт доступного баланса монет (с учётом цикла сброса Operation Siren);
    - полный процесс покупки в портовом магазине (сканирование -> фильтрация -> покупка);
    - взаимодействие с магазином Акаши (вход -> покупка -> возврат на карту).

    Attributes:
        _shop_yellow_coins (int): Текущий баланс жёлтых монет (устанавливается os_shop_get_coins).
        _shop_purple_coins (int): Текущий баланс фиолетовых монет (устанавливается os_shop_get_coins).
    """

    def os_shop_buy_execute(self, button, skip_first_screenshot=True) -> bool:
        """Выполнить покупку одного предмета.

        Обрабатывает подтверждение покупки, выбор количества и всплывающие окна.

        Args:
            button: Кнопка покупаемого предмета.
            skip_first_screenshot: Пропускать ли первый снимок экрана.

        Returns:
            bool: True при успешной покупке, иначе False.

        Pages:
            in: PORT_SUPPLY_CHECK
        """
        success = False
        amount_finish = False
        self.interval_clear([
            PORT_SUPPLY_CHECK, SHOP_BUY_CONFIRM_AMOUNT,
            SHOP_BUY_CONFIRM, OS_SHOP_BUY_CONFIRM, GET_ITEMS_1,
            SHOP_CLICK_SAFE_AREA
        ])
        set_amount_retry = 0
        # Счётчик повторных попыток покупки: предотвращает бесконечные нажатия товара и подтверждения при нехватке валюты
        buy_retry = 0
        buy_retry_limit = 3

        while True:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.handle_map_get_items(interval=3):
                self.interval_clear(PORT_SUPPLY_CHECK)
                success = True
                continue

            if self.appear_then_click(SHOP_BUY_CONFIRM, offset=(20, 20), interval=3):
                self.interval_reset(SHOP_BUY_CONFIRM)
                continue

            if self.appear_then_click(OS_SHOP_BUY_CONFIRM, offset=(20, 20), interval=3):
                self.interval_reset(OS_SHOP_BUY_CONFIRM)
                continue

            if not amount_finish and self.appear(SHOP_BUY_CONFIRM_AMOUNT, offset=(20, 20)):
                amount_finish = self.shop_buy_amount_handler(button)
                set_amount_retry += 1
                if not amount_finish and set_amount_retry > 3:
                    logger.warning(f'[Магазин Операции «Сирена»] Не удалось распознать количество для покупки предмета {button.name}.')
                    self.close_shop_buy_confirm_amount(skip_first_screenshot)
                    break
                continue

            if amount_finish and self.appear_then_click(SHOP_BUY_CONFIRM_AMOUNT, offset=(20, 20), interval=3):
                self.interval_reset(SHOP_BUY_CONFIRM_AMOUNT)
                continue

            if self.handle_popup_confirm('SHOP_BUY'):
                continue

            if not success and self.appear(PORT_SUPPLY_CHECK, offset=(20, 20), interval=5):
                buy_retry += 1
                if buy_retry > buy_retry_limit:
                    logger.warning(f'[Магазин Операции «Сирена»] Достигнут предел попыток покупки предмета {button.name}; возможно, недостаточно валюты')
                    break
                amount_finish = False
                self.device.click(button)
                continue

            # Условие завершения
            if success and self.appear(PORT_SUPPLY_CHECK, offset=(20, 20)):
                break

        return success

    def os_shop_buy(self, select_func) -> int:
        """Выполнить пакетную покупку предметов.

        Последовательно вызывает функцию выбора предметов и выполняет покупки до опустошения очереди или достижения лимита.

        Args:
            select_func: Функция выбора предмета, возвращающая объект покупки или None.

        Returns:
            int: Количество успешно купленных предметов.

        Pages:
            in: PORT_SUPPLY_CHECK
        """
        count = 0
        for _ in range(12):
            button = select_func()
            if button is None:
                logger.info('[Магазин Операции «Сирена»] Покупки в магазине+ завершены')
                return count
            else:
                self.os_shop_buy_execute(button)
                try:
                    if not getattr(self, 'is_running_cl1_leveling', False):
                        logger.debug('[Магазин Operation Siren] Фарм в зоне коррозии 1 не запущен; покупка очков действия у Акаси не учитывается')
                    else:
                        name = str(getattr(button, 'name', '') or '')
                        name_l = name.lower()
                        if 'actionpoint' in name_l or ('action' in name_l and 'point' in name_l):
                            import re

                            m = re.search(r"(\d+)", name)
                            base = int(m.group(1)) if m else 0
                            amount = int(getattr(button, 'amount', 1) or 1)
                            bought_ap = base * amount

                            instance_name = getattr(self.config, 'config_name', 'default')
                            from module.application.runtime_storage import get_runtime_storage

                            get_runtime_storage().record_ap_purchase(
                                instance_name,
                                amount=int(bought_ap),
                                base_amount=int(base),
                                purchase_count=int(amount),
                                source='akashi'
                            )
                            logger.info('[Магазин Операции «Сирена»] Данные о покупке очков действия у Акаши записаны в PostgreSQL')
                except StorageError:
                    raise
                except Exception:
                    logger.exception('[Магазин Операции «Сирена»] Ошибка при записи данных о покупке у Акаши')

                count += 1
                continue

        logger.warning('[Магазин Операции «Сирена»] Слишком много предметов в очереди покупки, покупки остановлены')
        return count

    def close_shop_buy_confirm_amount(self, skip_first_screenshot=True):
        """Закрыть интерфейс подтверждения количества покупки.

        Закрывает всплывающее окно выбора количества кликом по безопасной зоне.

        Args:
            skip_first_screenshot: Пропускать ли первый снимок экрана.

        Pages:
            in: SHOP_BUY_CONFIRM_AMOUNT
        """
        self.interval_clear([PORT_SUPPLY_CHECK, SHOP_BUY_CONFIRM_AMOUNT])
        while True:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(PORT_SUPPLY_CHECK, offset=(20, 20)):
                self.interval_clear(SHOP_BUY_CONFIRM_AMOUNT)
                break

            if self.appear(SHOP_BUY_CONFIRM_AMOUNT, offset=(20, 20), interval=3):
                self.device.click(SHOP_CLICK_SAFE_AREA)

    def shop_buy_amount_handler(self, item, skip_first_screenshot=True):
        """Обработать выбор количества покупки.

        Рассчитывает оптимальное количество по балансу монет и доступному запасу,
        после чего настраивает целевое число кнопками интерфейса.

        Args:
            item: Покупаемый предмет.
            skip_first_screenshot: Пропускать ли первый снимок экрана.

        Returns:
            bool: True при успешной установке количества, иначе False.

        Raises:
            ScriptError: Если не удалось распознать лимит через OCR.
        """
        limit = -1
        retry = Timer(0, count=3)
        retry.start()
        while True:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()
            limit = OCR_SHOP_AMOUNT.ocr(self.device.image)

            if limit == 0:
                logger.warning('[Магазин Операции «Сирена»] OCR_SHOP_AMOUNT распознано как 0, повторная попытка')
                self.close_shop_buy_confirm_amount()
                return False

            if limit > 0:
                break

            if retry.reached():
                logger.critical('[Магазин Операции «Сирена»+] Ошибка распознавания OCR_SHOP_AMOUNT, проверьте файл ресурсов')
                raise ScriptError
        retry.reset()


        currency = self.get_currency_coins(item)
        count = min(int(currency // item.price), item.count)

        if count == 1:
            return True

        coins = self.get_coins_no_limit(item)
        total_count = min(int(coins // item.price), item.count)

        set_to_max = False
        # Среднее количество всех товаров (кроме покупаемых за фиолетовые монеты) около 8.9, поэтому используем порог 10
        if count <= 10:
            if count - 1 > total_count - count:
                set_to_max = True
            limit = count
        elif total_count - count <= 10:
            set_to_max = True
            limit = count
        elif count >= total_count >> 1:
            set_to_max = True
            limit = total_count - 10
        else:
            limit = 10

        self.interval_clear(AMOUNT_MAX)
        # amount_max_stall: число случаев, когда количество не изменилось после нажатия AMOUNT_MAX; предотвращает бесконечный цикл при неработающей кнопке
        amount_max_stall = 0
        amount_max_stall_limit = 5
        while set_to_max:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear_then_click(AMOUNT_MAX, offset=(50, 50), interval=3):
                continue

            current_amount = OCR_SHOP_AMOUNT.ocr(self.device.image)
            if current_amount > 1:
                break

            # Если после нажатия AMOUNT_MAX количество остаётся равным 1, кнопка, вероятно, отключена игрой (например, товар можно покупать только по одному)
            amount_max_stall += 1
            if amount_max_stall >= amount_max_stall_limit:
                logger.info(f'[Магазин Операции «Сирена»] После {amount_max_stall} нажатий AMOUNT_MAX количество осталось равным {current_amount}; переход к AMOUNT_PLUS')
                break

        # Только если AMOUNT_MAX уже нажали и количество успешно увеличилось, можно считать фактический игровой максимум
        if set_to_max:
            game_max = OCR_SHOP_AMOUNT.ocr(self.device.image)
            if game_max > 1 and limit > game_max:
                logger.info(f'Расчётный предел покупки {limit} превышает игровой предел {game_max}; используется игровой предел')
                limit = game_max

        self.ui_ensure_index(limit, letter=OCR_SHOP_AMOUNT, prev_button=AMOUNT_MINUS, next_button=AMOUNT_PLUS,
                             skip_first_screenshot=True)
        return True

    def handle_port_supply_buy(self) -> bool:
        """Обработать покупки в портовом магазине.

        Сканирует все вкладки магазина, фильтрует доступные товары и последовательно выполняет покупку.

        Returns:
            bool: True при успешных покупках или отсутствии подходящих товаров, False при нехватке валюты.

        Pages:
            in: PORT_SUPPLY_CHECK
        """
        self.os_shop_get_coins()
        items = self.scan_all()
        if not len(items):
            logger.warning('Магазин Операции «Сирена»+ пуст')
            return False
        items = self.items_filter_in_os_shop(items)
        if not len(items):
            logger.warning('В магазине Операции «Сирена»+ нет доступных для покупки предметов')
            return False
        skip_get_coins = True
        items.reverse()
        count = 0
        while len(items):
            logger.hr('Покупки в магазине Операции «Сирена»+', level=2)
            item = items.pop()
            if not skip_get_coins:
                self.os_shop_get_coins()
            if item.price > self.get_currency_coins(item):
                logger.info(f'Недостаточно валюты для покупки предмета {item.name}, пропуск')
                if self.is_coins_both_not_enough():
                    logger.info('Валюты недостаточно для покупки любых предметов, покупки остановлены')
                    break
                continue
            logger.info(f'Покупка предмета {item.name}: магазин {item.shop_index + 1}, позиция {item.scroll_pos:.2f}')
            self.os_shop_side_navbar_ensure(upper=item.shop_index + 1)
            OS_SHOP_SCROLL.set(item.scroll_pos, main=self, skip_first_screenshot=False)
            _item = self.os_shop_get_items_to_buy(name=item.name, price=item.price)
            if _item is None:
                logger.warning(f'В магазине {item.shop_index + 1} на позиции {item.scroll_pos:.2f} не найден предмет {item.name}, пропуск')
                continue
            if not self.check_item_count(_item):
                logger.warning(f'Ошибка распознавания количества предмета {_item.name}, пропуск')
                continue
            if self.os_shop_buy_execute(_item):
                logger.info(f'Приобретён предмет {_item.name}')
                skip_get_coins = False
                count += 1
            else:
                logger.warning(f'Не удалось приобрести предмет {_item.name}, пропуск')
            self.device.click_record.clear()
        logger.info(f'В магазине порта куплено предметов: {count} шт.' if count else 'В магазине порта ничего не куплено')
        return True

    def handle_akashi_supply_buy(self, grid):
        """Обработать покупки в магазине Акаши.

        Кликает по ячейке с Акаши для перехода в магазин, выполняет покупки и возвращается на карту.

        Args:
            grid: Координаты сетки, где находится Акаши.

        Pages:
            in: is_in_map
            out: is_in_map
        """
        self.ui_click(grid, appear_button=self.is_in_map, check_button=PORT_SUPPLY_CHECK,
                      additional=self.handle_story_skip, skip_first_screenshot=True)
        self.os_shop_buy(select_func=self.os_shop_get_item_to_buy_in_akashi)
        self.ui_back(appear_button=PORT_SUPPLY_CHECK, check_button=self.is_in_map, skip_first_screenshot=True)

    @cached_property
    def yellow_coins_preserve(self):
        """Получить настройку резерва жёлтых монет."""
        if self.is_cl1_mode_enabled:
            return self.config.OpsiHazard1Leveling_OperationCoinsPreserve
        else:
            return self.config.OS_NORMAL_YELLOW_COINS_PRESERVE

    def get_currency_coins(self, item):
        """Получить доступное для покупок количество валюты.

        Определяет, нужно ли вычитать резерв, основываясь на времени до сброса Operation Siren.

        Args:
            item: Покупаемый предмет.

        Returns:
            int: Доступное количество валюты.
        """
        if item.cost == 'YellowCoins':
            if get_os_reset_remain() == 0:
                return self._shop_yellow_coins - 100
            else:
                return self._shop_yellow_coins - self.yellow_coins_preserve

        elif item.cost == 'PurpleCoins':
            if get_os_reset_remain() == 0:
                return self._shop_purple_coins
            else:
                return self._shop_purple_coins - self.config.OS_NORMAL_PURPLE_COINS_PRESERVE

    def get_coins_no_limit(self, item):
        """Получить полное количество валюты (без вычета резерва).

        Args:
            item: Покупаемый предмет.

        Returns:
            int: Общий баланс валюты.
        """
        if item.cost == 'YellowCoins':
            return self._shop_yellow_coins
        elif item.cost == 'PurpleCoins':
            return self._shop_purple_coins

    def is_coins_both_not_enough(self):
        """Проверить, недостаточны ли обе валюты (жёлтые и фиолетовые монеты).

        Returns:
            bool: True, если обе валюты ниже допустимого порога, иначе False.
        """
        if get_os_reset_remain() == 0:
            return False
        else:
            yellow = self._shop_yellow_coins < self._shop_purple_coins
            purple = self._shop_purple_coins < self.config.OS_NORMAL_PURPLE_COINS_PRESERVE
            return yellow and purple
