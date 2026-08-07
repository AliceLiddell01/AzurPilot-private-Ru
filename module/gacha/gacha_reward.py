"""建造（Gacha）系统模块，处理舰船建造的完整流程。
包括建造页面导航、资源消耗预计算、订单提交、
自动收菜（获取建造结果）以及建造队列的清理管理。"""

# 此文件处理建造（Gacha/Build）相关的操作。
# 包括多级建造页面的导航、资源消耗预计算、提交建造订单以及自动化收菜和队列清理逻辑。
from module.base.timer import Timer
from module.campaign.campaign_status import CampaignStatus
from module.combat.assets import GET_SHIP
from module.exception import ScriptError
from module.gacha.assets import *
from module.gacha.ui import GachaUI
from module.handler.assets import POPUP_CONFIRM, STORY_SKIP
from module.logger import logger
from module.ocr.ocr import Digit
from module.retire.retirement import Retirement
from module.log_res import LogRes

RECORD_GACHA_OPTION = ('RewardRecord', 'gacha')
RECORD_GACHA_SINCE = (0,)
OCR_BUILD_CUBE_COUNT = Digit(BUILD_CUBE_COUNT, letter=(255, 247, 247), threshold=64)
OCR_BUILD_TICKET_COUNT = Digit(BUILD_TICKET_COUNT, letter=(255, 247, 247), threshold=64)
OCR_BUILD_SUBMIT_COUNT = Digit(BUILD_SUBMIT_COUNT, letter=(255, 247, 247), threshold=64)
OCR_BUILD_SUBMIT_WW_COUNT = Digit(BUILD_SUBMIT_WW_COUNT, letter=(255, 247, 247), threshold=64)


class RewardGacha(GachaUI, Retirement, CampaignStatus):
    build_coin_count = 0
    build_cube_count = 0
    build_ticket_count = 0

    def gacha_prep(self, target, skip_first_screenshot=True):
        """
        准备提交建造订单。

        Args:
            target (int): 要提交的建造订单数量。
            skip_first_screenshot (bool): 是否跳过首次截图。

        Returns:
            bool: 准备完成返回 True，否则返回 False。

        Pages:
            in: page_build（任意子页面）
            out: 提交确认弹窗

        Raises:
            ScriptError: 无法识别 OCR 资源时抛出。
        """
        if not target:
            return False

        if not self.appear(BUILD_SUBMIT_ORDERS) \
                and not self.appear(BUILD_SUBMIT_WW_ORDERS):
            return False

        confirm_timer = Timer(1, count=2).start()
        ocr_submit = None
        index_offset = (60, 20)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear_then_click(BUILD_SUBMIT_ORDERS, interval=3):
                ocr_submit = OCR_BUILD_SUBMIT_COUNT
                confirm_timer.reset()
                continue

            if self.appear_then_click(BUILD_SUBMIT_WW_ORDERS, interval=3):
                ocr_submit = OCR_BUILD_SUBMIT_WW_COUNT
                confirm_timer.reset()
                continue
            if self.handle_popup_confirm('GACHA_PREP'):
                confirm_timer.reset()
                continue

            if self.appear(BUILD_PLUS, offset=index_offset) \
                    and self.appear(BUILD_MINUS, offset=index_offset):
                if confirm_timer.reached():
                    break

        if ocr_submit is None:
            raise ScriptError('[Строительство — подготовка] Не удалось распознать OCR-ресурс; '
                              'подготовка не может быть продолжена')
        area = ocr_submit.buttons[0]
        ocr_submit.buttons = [(BUILD_MINUS.button[2] + 3, area[1], BUILD_PLUS.button[0] - 3, area[3])]
        self.ui_ensure_index(target, letter=ocr_submit, prev_button=BUILD_MINUS,
                             next_button=BUILD_PLUS, skip_first_screenshot=True)

        return True

    def gacha_calculate(self, target_count, gold_cost, cube_cost):
        """
        根据当前资源计算实际可提交的建造数量。

        Args:
            target_count (int): 期望提交的建造订单数量。
            gold_cost (int): 金币消耗。
            cube_cost (int): 魔方消耗。

        Returns:
            int: 根据当前资源可实际提交的数量。
        """
        while 1:
            gold_total = gold_cost * target_count
            cube_total = cube_cost * target_count

            if not target_count:
                logger.warning('Недостаточно монет и/или Кубов мудрости для строительства')
                break

            if gold_total > self.build_coin_count or cube_total > self.build_cube_count:
                target_count -= 1
                continue

            break

        logger.info(f'Можно отправить не более {target_count} заказов на строительство')
        self.build_coin_count -= gold_total
        self.build_cube_count -= cube_total
        LogRes(self.config).Cube = self.build_cube_count
        self.config.update()
        return target_count

    def gacha_goto_pool(self, target_pool):
        """
        导航到指定的建造池页面。

        Args:
            target_pool (str): 建造池名称，超出范围时默认使用 'light' 池。

        Returns:
            str: 当前可用的建造池名称。

        Pages:
            in: page_build（建造池选择）
            out: page_build（建造池操作页面）

        Raises:
            ScriptError: 选择 'wishing_well' 但未完成配置时抛出。
        """
        self.gacha_bottom_navbar_ensure(right=3, is_build=True)

        if target_pool == 'wishing_well':
            if self._gacha_side_navbar.get_total(main=self) != 5:
                logger.warning('\'wishing_well\' недоступен; '
                               'используется пул \'light\'')
                target_pool = 'light'
            else:
                self.gacha_side_navbar_ensure(upper=2)
                if self.appear(BUILD_WW_CHECK):
                    raise ScriptError('\'wishing_well\' должен быть настроен '
                                      'пользователем вручную; продолжить '
                                      'gacha_goto_pool невозможно')
        elif target_pool == 'event':
            gacha_bottom_navbar = self._gacha_bottom_navbar(is_build=True)
            total = gacha_bottom_navbar.get_total(main=self)
            if total == 3:
                logger.warning('\'event\' недоступен; используется '
                               'пул \'light\'')
                target_pool = 'light'
            else:
                self.gacha_bottom_navbar_ensure(left=1, is_build=True)
        elif target_pool in ['heavy', 'special']:
            if target_pool == 'heavy':
                self.gacha_bottom_navbar_ensure(right=2, is_build=True)
            else:
                self.gacha_bottom_navbar_ensure(right=1, is_build=True)

        return target_pool

    def gacha_flush_queue(self, skip_first_screenshot=True):
        """
        清空建造订单队列，确保提交前队列为空。

        Args:
            skip_first_screenshot (bool): 是否跳过首次截图。

        Pages:
            in: page_build（任意子页面）
            out: page_build（建造池选择）

        Raises:
            ScriptError: 无法完全清空队列时退出（船坞可能已满）。
        """
        self.gacha_side_navbar_ensure(bottom=3)

        confirm_timer = Timer(1, count=2).start()
        confirm_mode = True
        STORY_SKIP.clear_offset()
        queue_clean = True
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(BUILD_QUEUE_EMPTY, offset=(20, 20)) and queue_clean:
                self.gacha_side_navbar_ensure(upper=1)
                break
            else:
                queue_clean = False

            if self.appear_then_click(BUILD_FINISH_ORDERS, interval=3):
                confirm_timer.reset()
                continue

            if self.handle_retirement():
                confirm_timer.reset()
                continue

            if self.handle_popup_confirm('FINISH_ORDERS'):
                if confirm_mode:
                    self.device.sleep((0.5, 0.8))
                    self.device.click(BUILD_FINISH_ORDERS)
                    confirm_mode = False
                confirm_timer.reset()
                continue

            if self.appear(GET_SHIP, interval=1):
                self.device.click(STORY_SKIP)
                confirm_timer.reset()
                continue
            if self.handle_get_items_ship():
                continue

            if self.appear(BUILD_FINISH_RESULTS, offset=(20, 150), interval=3):
                self.device.click(BUILD_FINISH_ORDERS)
                confirm_timer.reset()
                continue

            if self.appear(BUILD_SUBMIT_ORDERS) or self.appear(BUILD_SUBMIT_WW_ORDERS):
                if confirm_timer.reached():
                    break

        if self.appear(BUILD_SUBMIT_WW_ORDERS):
            logger.info('Находимся в Колодце желаний; возврат к обычному пулу')
            self.gacha_side_navbar_ensure(upper=1)

    def gacha_submit(self, skip_first_screenshot=True):
        """
        提交建造订单。

        Args:
            skip_first_screenshot (bool): 是否跳过首次截图。

        Pages:
            in: POPUP_CONFIRM
            out: BUILD_FINISH_ORDERS
        """
        logger.info('Отправка заказов на строительство')
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(POPUP_CONFIRM, offset=(20, 80), interval=3):
                POPUP_CONFIRM.name = POPUP_CONFIRM.name + '_' + 'GACHA_ORDER'
                self.device.click(POPUP_CONFIRM)
                POPUP_CONFIRM.name = POPUP_CONFIRM.name[:-len('GACHA_ORDER') - 1]
                continue

            if self.appear(BUILD_FINISH_ORDERS):
                break

    def gacha_run(self):
        """
        执行建造操作，提交建造订单。

        Returns:
            bool: 执行成功返回 True，否则返回 False。

        Pages:
            in: 任意页面
            out: page_build
        """
        self.ui_goto_gacha()
        self.gacha_flush_queue()

        self.build_coin_count = self.get_coin()
        self.build_cube_count = OCR_BUILD_CUBE_COUNT.ocr(self.device.image)

        actual_pool = self.gacha_goto_pool(self.config.Gacha_Pool)

        gold_cost = 600
        cube_cost = 1
        if actual_pool in ['heavy', 'special', 'event', 'wishing_well']:
            gold_cost = 1500
            cube_cost = 2

        buy = [self.config.Gacha_Amount, 0]
        if actual_pool == "event" and self.config.Gacha_UseTicket:
            if self.appear(BUILD_TICKET_CHECK, offset=(30, 30)):
                self.build_ticket_count = OCR_BUILD_TICKET_COUNT.ocr(self.device.image)
            else:
                logger.info('Билет на строительство не обнаружен; используются Кубы мудрости и монеты')
        if self.config.Gacha_Amount > self.build_ticket_count:
            buy[0] = self.build_ticket_count
            buy[1] = self.gacha_calculate(self.config.Gacha_Amount - self.build_ticket_count, gold_cost, cube_cost)
        else:
            LogRes(self.config).Cube = self.build_cube_count
            self.config.update()

        result = False
        for buy_count in buy:
            if self.gacha_prep(buy_count):
                self.gacha_submit()

                if self.config.Gacha_UseDrill:
                    self.gacha_flush_queue()
                result = True

        return result

    def run(self):
        """
        根据配置执行建造操作。

        Pages:
            in: 任意页面
            out: page_build
        """
        self.gacha_run()
        self.config.task_delay(server_update=True)
