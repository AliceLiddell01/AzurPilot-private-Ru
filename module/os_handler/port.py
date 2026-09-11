"""大世界港口处理器模块。

提供碧蓝航线大世界（Operation Siren）港口相关的自动化操作，包括：
- 港口的进入与退出
- 港口任务的接取（已弃用，自 2022.01.13 起任务仅在总览中显示）
- 港口商店的进入与退出（含意外进入情报界面的恢复逻辑）
- 港口船坞的舰船修复

港口是大世界中的核心节点，玩家可以在此补给、维修、接取任务和访问商店。
本模块继承自 OSShop，与大世界商店模块共享商店交互能力。
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
    """大世界港口处理器。

    继承 OSShop，提供港口内的全部交互操作。

    港口功能：
    - 进入 / 退出港口（从大世界地图进出）
    - 任务接取（已弃用）
    - 港口商店的进入与退出（含意外页面的恢复机制）
    - 船坞舰船的批量修复

    注意：碧蓝航线和红轴港口的可用按钮不同，统一使用 PORT_GOTO_SUPPLY 作为检查点。
    """
    def port_enter(self):
        """
        进入港口。

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
        退出港口。

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
        接受港口中的所有任务。

        自 2022.01.13 起已弃用，任务仅在总览中显示，不再在港口中显示。

        Returns:
            bool: 所有任务已接受或未找到任务时返回 True，无法接受更多任务时返回 False。

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
        进入港口商店。

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
        退出港口商店。

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
        修复所有舰船。

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
