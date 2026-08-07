"""
代币商店处理器（大世界商店）。

通过模板匹配定位代币图标，动态计算商品网格布局，
识别并过滤代币商店中的商品，按配置购买优先级执行购买。
支持单次购买日志档案商品的 run_once() 方法。
支持 Operation Siren Data Logger 的独立购买和售罄确认。
"""

import cv2
import numpy as np

from module.base.button import ButtonGrid
from module.base.decorator import cached_property, del_cached_property
from module.base.timer import Timer
from module.config.opsi_data_logger import (
    DATA_LOGGER_ITEM_NAME,
    DATA_LOGGER_NAME,
    DataLoggerShopResult,
    DataLoggerShopState,
)
from module.config.redirect_utils.shop_filter import voucher_redirect
from module.handler.assets import POPUP_CANCEL, POPUP_CONFIRM
from module.logger import logger
from module.map_detection.utils import Points
from module.ocr.ocr import DigitYuv
from module.shop.assets import *
from module.shop.base import ShopItemGrid
from module.shop.clerk import ShopClerk
from module.shop.shop_status import ShopStatus
from module.ui.assets import BACK_ARROW
from module.ui.scroll import Scroll

PRICE_OCR = DigitYuv([], letter=(255, 223, 57), threshold=128, name='Price_ocr')
VOUCHER_SHOP_SCROLL = Scroll(VOUCHER_SHOP_SCROLL_AREA, color=(255, 255, 255))
TEMPLATE_VOUCHER_ICON = Template('./assets/shop/cost/Voucher.png')
DATA_LOGGER_TEMPLATE_SIMILARITY = 0.82
DATA_LOGGER_PURCHASE_SECONDS = 45


class VoucherShop(ShopClerk, ShopStatus):
    """代币商店处理器（大世界商店）。

    通过模板匹配定位代币图标来动态计算商品网格，
    结合过滤器配置自动购买代币商店商品。
    支持普通购买流程和单次购买日志档案两种模式。

    Pages: in: page_shop (voucher shop tab)
    """
    @cached_property
    def shop_filter(self):
        """获取凭证商店过滤器。

        Returns:
            str: 过滤器字符串
        """
        return voucher_redirect(self.config.OpsiVoucher_Filter.strip())

    def _get_vouchers(self):
        """检测截图中的凭证图标位置。

        通过模板匹配在商店左侧区域查找凭证图标，
        返回图标左上角的坐标数组。

        Returns:
            np.array: [[x1, y1], [x2, y2]]，凭证图标左上角坐标
        """
        left_column = self.image_crop((305, 306, 1256, 646), copy=False)
        vouchers = TEMPLATE_VOUCHER_ICON.match_multi(left_column, similarity=0.75, threshold=5)
        vouchers = Points([(0., v.area[1]) for v in vouchers]).group(threshold=5)
        logger.attr('Количество значков жетонов', len(vouchers))
        return vouchers

    def wait_until_voucher_appear(self, skip_first_screenshot=True):
        """等待凭证商店页面加载完成。

        进入凭证商店后，商品列表加载需要时间，
        此方法等待任意凭证图标出现。

        Args:
            skip_first_screenshot: 是否跳过首次截图
        """
        timeout = Timer(1, count=3).start()
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            vouchers = self._get_vouchers()

            if timeout.reached():
                break
            if len(vouchers):
                break

    @cached_property
    def shop_grid(self):
        """根据凭证图标位置计算商店网格。

        通过检测到的凭证图标数量和位置动态计算商品网格的
        原点、间距和行数，适配不同服务器布局。

        Returns:
            ButtonGrid: 商店商品网格
        """
        vouchers = self._get_vouchers()
        count = len(vouchers)
        if count == 0:
            logger.warning('Значки жетонов не найдены; предполагается, что список товаров находится сверху')
            origin_y = 200
            delta_y = 191
            row = 2
        elif count == 1:
            y_list = vouchers[:, 1]
            # +306, 裁剪区域顶部偏移 (_get_vouchers)
            # -133, 从凭证图标顶部到商品顶部的偏移
            origin_y = y_list[0] + 306 - 133
            delta_y = 191
            row = 1
        elif count == 2:
            y_list = vouchers[:, 1]
            y1, y2 = y_list[0], y_list[1]
            origin_y = min(y1, y2) + 306 - 133
            delta_y = abs(y1 - y2)
            row = 2
        else:
            logger.warning(f'Неожиданный результат сопоставления значков жетонов: {[v.area for v in vouchers]}')
            origin_y = 200
            delta_y = 191
            row = 2

        # 构建 ButtonGrid
        # 原始网格参数:
        # shop_grid = ButtonGrid(
        #     origin=(463, 200), delta=(156, 191), button_shape=(99, 99), grid_shape=(5, 2), name='SHOP_GRID')
        if self.config.SERVER in ['cn', 'jp', 'tw']:
            shop_grid = ButtonGrid(
                origin=(305, origin_y), delta=(189.5, delta_y), button_shape=(99, 99), grid_shape=(5, row),
                name='SHOP_GRID')
        else:
            shop_grid = ButtonGrid(
                origin=(463, origin_y), delta=(156, delta_y), button_shape=(99, 99), grid_shape=(5, row),
                name='SHOP_GRID')
        return shop_grid

    shop_template_folder = './assets/shop/voucher'

    @cached_property
    def shop_voucher_items(self):
        """加载凭证商店商品模板和配置。

        Returns:
            ShopItemGrid: 商店商品网格对象
        """
        shop_grid = self.shop_grid
        shop_voucher_items = ShopItemGrid(
            shop_grid,
            templates={}, amount_area=(60, 74, 96, 95),
            price_area=(52, 132, 132, 162))
        shop_voucher_items.load_template_folder(self.shop_template_folder)
        shop_voucher_items.load_cost_template_folder('./assets/shop/cost')
        shop_voucher_items.similarity = 0.85
        shop_voucher_items.cost_similarity = 0.5
        shop_voucher_items.price_ocr = PRICE_OCR
        return shop_voucher_items

    def shop_items(self):
        """获取商店商品网格的统一接口。

        所有商店共享相同的属性名，使用 @Config 时需要
        定义唯一的别名作为覆盖。

        Returns:
            ShopItemGrid: 商店商品网格
        """
        return self.shop_voucher_items

    def shop_currency(self):
        """OCR 识别凭证商店货币数量。

        通过状态检测获取当前凭证余额并记录日志。

        Returns:
            int: 凭证数量
        """
        self._currency = self.status_get_voucher()
        logger.info(f'Жетоны: {self._currency}')
        return self._currency

    def shop_interval_clear(self):
        """清除购买界面相关按钮的点击间隔。

        重置购买确认、选择、数量等按钮的 interval 状态，
        防止误触发。
        """
        self.interval_clear(BACK_ARROW)
        self.interval_clear(SHOP_BUY_CONFIRM)
        self.interval_clear([
            SHOP_BUY_CONFIRM_SELECT,
            SHOP_BUY_CONFIRM_AMOUNT,
            POPUP_CONFIRM,
            POPUP_CANCEL,
        ])

    def shop_buy_handle(self, item):
        """处理凭证商店购买界面。

        检测并处理购买确认选择、数量输入、弹窗确认等界面。

        Args:
            item: 待购买的商品对象

        Returns:
            bool: 是否检测到购买界面并进行了处理
        """
        if self.appear(SHOP_BUY_CONFIRM_SELECT, offset=(20, 20), interval=3):
            self.shop_buy_select_execute(item)
            self.interval_reset(SHOP_BUY_CONFIRM_SELECT)
            return True
        if self.appear(SHOP_BUY_CONFIRM_AMOUNT, offset=(20, 20), interval=3):
            self.shop_buy_amount_execute(item)
            self.interval_reset(SHOP_BUY_CONFIRM_AMOUNT)
            return True
        if self.handle_popup_confirm(name='SHOP_BUY_VOUCHER', offset=(20, 50)):
            return True
        if self.config.SERVER in ['cn', 'jp', 'tw']:
            # 购买数量为 1 时显示"兑换"按钮
            if self.appear_then_click(SHOP_BUY_CONFIRM_AMOUNT, offset=(-20, -160, 20, -120), interval=3):
                return True

        return False

    def shop_buy_execute(
        self,
        item,
        skip_first_screenshot=True,
        timeout_seconds=None,
    ):
        """执行凭证商店购买操作。

        通过状态循环完成从点击商品到购买确认的完整流程。
        处理退役、遮挡、信息栏等意外情况。

        普通购买不传 ``timeout_seconds``，保持原有行为。Data Logger
        流程传入有限超时，避免无法识别的弹窗或 UI 状态永久卡住任务。

        Args:
            item: 待购买的商品对象
            skip_first_screenshot: 是否跳过首次截图
            timeout_seconds: 可选的状态机总超时秒数

        Returns:
            bool: 是否观察到购买完成并返回商店页面
        """
        success = False
        timeout = None
        if timeout_seconds is not None:
            timeout = Timer.from_seconds(timeout_seconds).start()
        self.shop_interval_clear()

        while timeout is None or not timeout.reached():
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if self.appear(BACK_ARROW, offset=(30, 30), interval=3):
                self.device.click(item)
                continue
            if self.appear_then_click(SHOP_BUY_CONFIRM, offset=(20, 20), interval=3):
                self.interval_reset(BACK_ARROW)
                continue
            if self.shop_buy_handle(item):
                self.interval_reset(BACK_ARROW)
                continue
            if self.handle_retirement():
                self.interval_reset(BACK_ARROW)
                continue
            if self.shop_obstruct_handle():
                self.interval_reset(BACK_ARROW)
                success = True
                continue
            if self.info_bar_count():
                self.interval_reset(BACK_ARROW)
                success = True
                continue

            # 结束条件
            if success and self.appear(BACK_ARROW, offset=(30, 30)):
                return True

        logger.warning(
            f'[{DATA_LOGGER_NAME}] тайм-аут автомата покупки через '
            f'{timeout_seconds} секунд'
        )
        return False

    def _reset_page_cache(self):
        del_cached_property(self, 'shop_grid')
        del_cached_property(self, 'shop_voucher_items')

    def _data_logger_page_inspection(self, items):
        for item in items:
            if item.name != DATA_LOGGER_ITEM_NAME:
                continue
            item_image = getattr(item, 'image', None)
            if item_image is not None:
                is_available = bool(
                    np.mean(np.max(item_image, axis=2) > 139) > 0.3
                )
                if not is_available:
                    return (
                        DataLoggerShopState.SOLD_OUT,
                        None,
                        'recognized_dimmed_target',
                    )
            if item.price > 0:
                return (
                    DataLoggerShopState.AVAILABLE,
                    item,
                    'recognized_with_positive_price',
                )
            return DataLoggerShopState.UNKNOWN, None, 'recognized_without_price'

        shop_items = self.shop_items()
        target_template = shop_items.templates.get(DATA_LOGGER_ITEM_NAME)
        if target_template is None:
            logger.warning(f'[{DATA_LOGGER_NAME}] отсутствует шаблон товара {DATA_LOGGER_ITEM_NAME}')
            return DataLoggerShopState.UNKNOWN, None, 'target_template_missing'

        best_similarity = 0.0
        best_available_brightness = None
        for button in shop_items.grids.buttons:
            raw_item = shop_items.item_class(self.device.image, button)
            result = cv2.matchTemplate(
                raw_item.image,
                target_template,
                cv2.TM_CCOEFF_NORMED,
            )
            _, similarity, _, _ = cv2.minMaxLoc(result)
            if similarity > best_similarity:
                best_similarity = similarity
                best_available_brightness = bool(
                    np.mean(np.max(raw_item.image, axis=2) > 139) > 0.3
                )

        if best_similarity < DATA_LOGGER_TEMPLATE_SIMILARITY:
            return DataLoggerShopState.UNKNOWN, None, 'target_not_observed'
        if best_available_brightness is False:
            return DataLoggerShopState.SOLD_OUT, None, f'dimmed_target:{best_similarity:.3f}'
        return DataLoggerShopState.UNKNOWN, None, f'target_unreadable:{best_similarity:.3f}'

    def inspect_data_logger(self):
        """Inspect every voucher-shop page for Operation Siren Data Logger."""
        self.wait_until_voucher_appear()
        VOUCHER_SHOP_SCROLL.set_top(main=self)
        saw_sold_out = False
        sold_out_reason = ''

        for _ in range(12):
            items = self.shop_get_items()
            state, item, reason = self._data_logger_page_inspection(items)
            logger.info(f'[{DATA_LOGGER_NAME}] состояние магазина={state.value}, причина={reason}')
            if state is DataLoggerShopState.AVAILABLE:
                return state, item, reason
            if state is DataLoggerShopState.SOLD_OUT:
                self.device.screenshot()
                self._reset_page_cache()
                confirm_items = self.shop_get_items()
                confirm_state, confirm_item, confirm_reason = (
                    self._data_logger_page_inspection(confirm_items)
                )
                logger.info(
                    f'[{DATA_LOGGER_NAME}] повторное состояние магазина='
                    f'{confirm_state.value}, причина={confirm_reason}'
                )
                if confirm_state is DataLoggerShopState.AVAILABLE:
                    return confirm_state, confirm_item, confirm_reason
                if confirm_state is DataLoggerShopState.SOLD_OUT:
                    saw_sold_out = True
                    sold_out_reason = confirm_reason

            if VOUCHER_SHOP_SCROLL.at_bottom(main=self):
                break
            VOUCHER_SHOP_SCROLL.next_page(main=self)
            self._reset_page_cache()

        if saw_sold_out:
            return DataLoggerShopState.SOLD_OUT, None, sold_out_reason
        return DataLoggerShopState.UNKNOWN, None, 'full_scan_inconclusive'

    def ensure_data_logger(self) -> DataLoggerShopResult:
        """Buy and confirm only Operation Siren Data Logger."""
        logger.hr(DATA_LOGGER_NAME, level=2)
        state, item, reason = self.inspect_data_logger()
        if state is DataLoggerShopState.SOLD_OUT:
            return DataLoggerShopResult(state=state, reason=reason)
        if state is DataLoggerShopState.UNKNOWN or item is None:
            return DataLoggerShopResult(
                state=DataLoggerShopState.UNKNOWN,
                reason=reason,
            )

        logger.info(
            f'[{DATA_LOGGER_NAME}] покупка подтверждённо доступного предмета: '
            f'cost={getattr(item, "cost", None)}, price={item.price}'
        )
        purchase_finished = self.shop_buy_execute(
            item,
            timeout_seconds=DATA_LOGGER_PURCHASE_SECONDS,
        )
        if not purchase_finished:
            return DataLoggerShopResult(
                state=DataLoggerShopState.UNKNOWN,
                reason='purchase_timeout',
                purchased=True,
            )

        self.device.screenshot()
        confirmed_state, _, confirmed_reason = self.inspect_data_logger()
        if confirmed_state is not DataLoggerShopState.SOLD_OUT:
            logger.warning(
                f'[{DATA_LOGGER_NAME}] покупка не подтверждена состоянием SOLD_OUT; '
                'возможна нехватка Oil или неопределённое состояние магазина'
            )
            return DataLoggerShopResult(
                state=DataLoggerShopState.UNKNOWN,
                reason=f'purchase_not_confirmed:{confirmed_reason}',
                purchased=True,
            )
        return DataLoggerShopResult(
            state=DataLoggerShopState.SOLD_OUT,
            reason=confirmed_reason,
            purchased=True,
        )

    def run(self):
        """运行凭证商店购买流程。

        Pages: in: page_shop (voucher shop tab)

        按照过滤器配置购买凭证商店商品，自动翻页直到列表底部。
        """
        # 过滤器为空时直接退出
        if not self.shop_filter:
            return

        # 调用时应已在凭证商店界面
        logger.hr('[Магазин — жетоны] Магазин жетонов', level=1)
        self.wait_until_voucher_appear()

        # 执行购买操作
        VOUCHER_SHOP_SCROLL.set_top(main=self)
        while 1:
            self.shop_buy()
            if VOUCHER_SHOP_SCROLL.at_bottom(main=self):
                logger.info('[Магазин — жетоны] Достигнут конец магазина жетонов; остановка')
                break
            else:
                VOUCHER_SHOP_SCROLL.next_page(main=self)
                del_cached_property(self, 'shop_grid')
                del_cached_property(self, 'shop_voucher_items')
                continue

    def run_once(self):
        """单次运行凭证商店，购买一个日志档案类型商品。

        Pages: in: page_shop (voucher shop tab)

        Returns:
            bool: 是否成功购买
        """
        # 替换过滤器
        self.shop_filter = 'LoggerArchive'

        # 调用时应已在凭证商店界面
        logger.hr('[Магазин — жетоны] Разовая покупка в магазине жетонов', level=1)
        self.wait_until_voucher_appear()

        # 执行购买操作
        items = self.shop_get_items()
        self.shop_currency()
        if self._currency <= 0:
            logger.warning(f'[Магазин — жетоны] Текущие средства: {self._currency}; остановка')
            return False

        item = self.shop_get_item_to_buy(items)
        if item is None:
            logger.info('[Магазин — жетоны] Нет архивов регистратора для покупки')
            return False
        self.shop_buy_execute(item)

        logger.info('Куплен один архив регистратора')
        return True