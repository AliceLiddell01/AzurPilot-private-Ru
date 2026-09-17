"""Модуль обработчика портов Операции «Сирена».

Предоставляет автоматизацию действий в портах Операции «Сирена» (Operation Siren) Azur Lane:
- Вход и выход из порта
- Принятие портовых заданий (устарело: с 13.01.2022 задания отображаются только в общей сводке)
- Вход и выход из магазина порта (с логикой восстановления при случайном попадании на экран сведений)
- Ремонт кораблей на верфи порта

Порты являются ключевыми узлами в Операции «Сирена», где игрок может пополнять запасы,
ремонтировать корабли, принимать задания и посещать магазин. Модуль наследует OSShop
и разделяет возможности взаимодействия с магазином.
"""
from module.base.timer import Timer
from module.logger import logger
from module.os_handler.assets import *
from module.os_shop.assets import PORT_SUPPLY_CHECK
from module.os_shop.shop import OSShop
from module.ui.assets import BACK_ARROW

# В портах Лазурного пути есть PORT_GOTO_MISSION, PORT_GOTO_SUPPLY и PORT_GOTO_DOCK
# В портах Багровой оси есть PORT_GOTO_SUPPLY
# Используем PORT_GOTO_SUPPLY как проверочный элемент
PORT_CHECK = PORT_GOTO_SUPPLY


class PortHandler(OSShop):
    """Обработчик портов Операции «Сирена».

    Наследует OSShop и обеспечивает все взаимодействия внутри порта.

    Функции порта:
    - Вход / выход из порта (переход с карты Операции «Сирена» и обратно)
    - Принятие заданий (устарело)
    - Вход и выход из портового магазина (с механизмом восстановления при непредвиденных экранах)
    - Массовый ремонт кораблей на верфи

    Примечание: в портах Лазурного пути и Багровой оси кнопки различаются,
    в качестве контрольной точки проверки используется PORT_GOTO_SUPPLY.
    """
    def port_enter(self):
        """
        Войти в порт.

        Pages:
            in: IN_MAP
            out: PORT_CHECK
        """
        logger.info('Вход в порт')
        for _ in self.loop():
            if self.appear(PORT_CHECK, offset=(20, 20)):
                break
            if self.appear_then_click(PORT_ENTER, offset=(20, 20), interval=5):
                continue
            if self.handle_map_event():
                continue
        # У нижних кнопок есть анимация появления
        pass  # Это уже обеспечивается в ui_click

    def port_quit(self, skip_first_screenshot=True):
        """
        Выйти из порта.

        Pages:
            in: PORT_CHECK
            out: IN_MAP
        """
        logger.info('Выход из порта')
        self.ui_back(appear_button=PORT_CHECK, check_button=self.is_in_map,
                     skip_first_screenshot=skip_first_screenshot)
        # У нижних кнопок есть анимация появления
        self.wait_os_map_buttons()

    def port_mission_accept(self):
        """
        Принять все задания в порту.

        Устарело с 13.01.2022: задания отображаются только в общем обзоре и больше не показываются в порту.

        Returns:
            bool: True, если все задания приняты или задания не найдены; False, если больше невозможно принять задания.

        Pages:
            in: PORT_CHECK
            out: PORT_CHECK
        """
        if not self.appear(PORT_MISSION_RED_DOT):
            logger.info('[Операция «Сирена» — порт] В этом порту нет доступных заданий')
            return True

        self.ui_click(PORT_GOTO_MISSION, appear_button=PORT_CHECK, check_button=PORT_MISSION_CHECK,
                      skip_first_screenshot=True)

        confirm_timer = Timer(1.5, count=3).start()
        success = True
        for _ in self.loop():
            if self.appear_then_click(PORT_MISSION_ACCEPT, offset=(20, 20), interval=0.2):
                confirm_timer.reset()
                continue
            else:
                # Завершение
                if confirm_timer.reached():
                    success = True
                    break

            if self.info_bar_count():
                logger.info('[Операция «Сирена» — порт] Невозможно принять задание: достигнут предел количества заданий')
                success = False
                break

        self.ui_back(appear_button=PORT_MISSION_CHECK, check_button=PORT_CHECK, skip_first_screenshot=True)
        return success

    def port_shop_enter(self):
        """
        Войти в магазин порта.

        Pages:
            in: PORT_CHECK
            out: PORT_SUPPLY_CHECK
        """
        self.ui_click(PORT_GOTO_SUPPLY, appear_button=PORT_CHECK, check_button=PORT_SUPPLY_CHECK,
                      skip_first_screenshot=True)
        # У предметов в порту есть анимация появления
        self.device.sleep(0.5)
        self.device.screenshot()

    def port_shop_quit(self, skip_first_screenshot=True):
        """
        Выйти из магазина порта.

        Pages:
            in: PORT_SUPPLY_CHECK
            out: PORT_CHECK
        """
        logger.info('Выход из портового магазина')
        
        self.interval_clear([PORT_SUPPLY_CHECK, PORT_CHECK, ORDER_CHECK])
        
        # Защита по тайм-ауту: Timer(10, count=30) ограничивает выполнение 10 секундами / 30 вызовами reached()
        timeout = Timer(10, count=30).start()
        order_quit_used = False
        
        while True:
            # Защита по тайм-ауту: должны одновременно пройти 10 секунд и произойти более 30 вызовов reached()
            if timeout.reached():
                logger.warning('[Операция «Сирена» — порт] Истекло время выхода из портового магазина, попытка использовать стрелку «Назад»')
                self.ui_back(appear_button=PORT_SUPPLY_CHECK, check_button=PORT_CHECK, skip_first_screenshot=True)
                break
            
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # Успешно вернулись на экран порта
            if self.appear(PORT_CHECK, offset=(20, 20)):
                logger.info('[Операция «Сирена» — порт] Выполнен возврат на экран порта')
                break

            # При случайном входе на экран разведданных (обзор операции) корректно закрываем его через order_quit
            if self.appear(ORDER_CHECK, offset=(20, 20)):
                logger.warning('[Операция «Сирена» — порт] Случайно открыт экран разведданных, выполняется order_quit')
                self.order_quit()
                order_quit_used = True
                self.interval_clear([PORT_SUPPLY_CHECK, PORT_CHECK, ORDER_CHECK])
                timeout.reset()
                continue

            # После выхода с экрана разведданных можем оказаться на глобальной карте; повторно входим в порт
            if order_quit_used and self.is_in_map():
                logger.info('[Операция «Сирена» — порт] После выхода с экрана разведданных открыта глобальная карта, повторный вход в порт')
                self.port_enter()
                order_quit_used = False
                self.interval_reset(PORT_CHECK)
                continue

            # Обычное нажатие стрелки «Назад»
            if self.appear(PORT_SUPPLY_CHECK, offset=(20, 20), interval=3):
                self.device.click(BACK_ARROW)
                self.interval_reset(PORT_SUPPLY_CHECK)
                continue

    def port_dock_repair(self):
        """
        Отремонтировать все корабли.

        Pages:
            in: PORT_CHECK
            out: PORT_CHECK
        """
        self.ui_click(PORT_GOTO_DOCK, appear_button=PORT_CHECK, check_button=PORT_DOCK_CHECK,
                      skip_first_screenshot=True)

        repaired = False
        for _ in self.loop():
            # Завершение
            if self.info_bar_count():
                break
            if repaired and self.appear(PORT_DOCK_CHECK, offset=(20, 20)):
                break

            # PORT_DOCK_CHECK — кнопка «Починить всё»
            if self.appear_then_click(PORT_DOCK_CHECK, offset=(20, 20), interval=2):
                continue
            if self.handle_popup_confirm('DOCK_REPAIR'):
                repaired = True
                continue

        self.ui_back(appear_button=PORT_DOCK_CHECK, check_button=PORT_CHECK, skip_first_screenshot=True)
