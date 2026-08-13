"""活动商店模块。

提供碧蓝航线活动商店的自动化购买功能，包括：
- 活动点数（pt/URpt）的获取与余额管理
- UR 舰船购买逻辑（含 URpt 不足时的自动兑换）
- 未获取物品（unobtained）的优先购买
- 基于预设或自定义过滤器的批量购买策略
- 自定义过滤器的消耗量追踪与自动禁用

支持多个活动商店标签页的自动切换与遍历。
"""
from typing import List, Tuple

from module.base.decorator import del_cached_property
from module.base.timer import Timer
from module.logger import logger
from module.shop.assets import NAV_GENERAL, NAV_EVENT
from module.shop_event.assets import NO_NAV_EVENT_CHECK
from module.shop_event.clerk import EventShopClerk, ItemNotFoundError
from module.shop_event.item import EventShopItem, UR_SHIP_PRICES_IN_URPT, COIN_PRICE_IN_URPT, URPT_PRICE_IN_PT
from module.shop_event.selector import (
    EVENT_SHOP_PRESET_FILTER,
    FILTER,
    parse_filter_amount,
    strip_filter_amount,
    parse_filter_tokens,
    rebuild_filter_tokens,
)
from module.ui.assets import SHOP_GOTO_MUNITIONS
from module.ui.page import page_shop, page_munitions


class EventShop(EventShopClerk):
    """活动商店自动化购买控制器。

    继承 EventShopClerk，实现完整的活动商店购买流程：
    1. 获取当前活动点数（pt / URpt）
    2. 优先处理 URpt 相关物品（UR 舰船、URpt 兑换、金币）
    3. 处理未获取物品（unobtained tag）的购买
    4. 根据过滤器配置批量购买剩余物品
    5. 活动结束后自动消耗自定义过滤器中的购买数量

    支持多个活动商店标签页的自动遍历，每个标签页独立执行购买逻辑。

    Attributes:
        pt (int): 当前活动点数余额。
        urpt (int): 当前 UR 活动点数余额。
        pt_preserved (int): 为后续购买预留的活动点数。
    """
    pt = 0
    urpt = 0
    pt_preserved = 0

    def event_shop_buy_item(self, item_to_buy, amount=None):
        # Fail closed before the click: even a partially successful purchase
        # must never leave the pre-purchase snapshot looking fresh.
        try:
            from module.event_datamine.artifact import load_builtin_artifact
            from module.webui.event_shop_observation import invalidate_event_shop_observation

            spec = load_builtin_artifact()["event_spec"]
            invalidate_event_shop_observation(
                instance=self.config.config_name,
                event_id=str(spec.get("id") or ""),
                server=str(spec.get("server") or "EN"),
            )
        except (OSError, TypeError, ValueError) as exc:
            logger.warning(f"[Магазин события — наблюдение] Не удалось инвалидировать snapshot: {exc}")
        return super().event_shop_buy_item(item_to_buy, amount=amount)

    def get_current_pts(self):
        self.pt = self.event_shop_get_pt()
        if self.event_shop_has_urpt:
            self.urpt = self.event_shop_get_urpt()

    def preserve_pt(self, amount: int):
        """
        Preserve pt for future use.
        """
        self.pt_preserved += amount
        logger.info(f"[Магазин события] Зарезервировано {amount} PT для последующего использования. Всего зарезервировано PT: {self.pt_preserved}")

    def handle_items_related_with_urpt(self, items: List[EventShopItem], num_of_ships_to_buy: int = 2) \
            -> Tuple[List[EventShopItem], List[EventShopItem]]:
        """
        Buy items (currently only ships) with URpt and buy URpt if necessary.

        Should be called first before buying other items, and should not be called again after buying other items.

        Returns:
            Tuple[List[EventShopItem], List[EventShopItem]]:
            A tuple of two lists:
            - The first list contains other normal items that are not related to URpt.
            - The second list contains special items that are related to URpt (URpt, coins),
            that should be dealt with at last.
        """
        if not self.event_shop_has_urpt:
            logger.info("[Магазин события] В магазине события нет UR-очков; обработка связанных с UR-очками товаров пропущена")
            return items, []

        ship_items = []
        urpt_items = []
        coin_items = []
        other_items = []

        for item in items:
            if item.price in UR_SHIP_PRICES_IN_URPT and item.cost == "URpt":
                ship_items.append(item)
            elif item.price == COIN_PRICE_IN_URPT and item.cost == "URpt":
                coin_items.append(item)
            elif item.price == URPT_PRICE_IN_PT and item.cost == "pt":
                urpt_items.append(item)
            else:
                other_items.append(item)

        # Buy ships first.
        urpt_preserve = False
        ship_items.sort(key=lambda item: item.price)
        if ship_items and num_of_ships_to_buy > 0:
            if len(ship_items) == 1 and num_of_ships_to_buy == 1:
                logger.info("[Магазин события] Найден один корабль и задана покупка одного корабля; покупка корабля пропущена")
            else:
                ships_to_buy = ship_items[:num_of_ships_to_buy]
                logger.info(f"[Магазин события] Попытка купить корабли: {[str(item) for item in ships_to_buy]}")
                current_urpt = self.event_shop_get_urpt()
                while ships_to_buy:
                    urpt_needed = sum([item.price for item in ships_to_buy])
                    if current_urpt >= urpt_needed:
                        for item in ships_to_buy:
                            self.event_shop_buy_item(item)
                        logger.info(f"[Магазин события] Корабли успешно куплены: {[str(item) for item in ships_to_buy]}")
                        break
                    else:
                        if self.is_event_ended:
                            urpt_in_stock = urpt_items[0].count if urpt_items else 0
                            if current_urpt + urpt_in_stock >= urpt_needed:
                                if urpt_in_stock > 0:
                                    self.event_shop_buy_item(urpt_items[0], amount=urpt_needed - current_urpt)
                                    urpt_items[0].count -= (urpt_needed - current_urpt)
                                for item in ships_to_buy:
                                    self.event_shop_buy_item(item)
                                logger.info(f"[Магазин события] Корабли успешно куплены: {[str(item) for item in ships_to_buy]}")
                                break
                            else:
                                logger.warning(
                                    f"[Магазин события] Недостаточно UR-очков для покупки кораблей: {[str(item) for item in ships_to_buy]}; "
                                    f"самый дорогой пропущен, повторная попытка")
                                ships_to_buy.pop()
                        else:
                            urpt_in_stock = urpt_items[0].count if urpt_items else 0
                            if current_urpt + urpt_in_stock >= urpt_needed:
                                pt_needed = (urpt_needed - current_urpt) * URPT_PRICE_IN_PT
                                self.preserve_pt(pt_needed)
                                logger.info(f"[Магазин события] Зарезервировано {pt_needed} PT для UR-очков на покупку кораблей")
                                urpt_preserve = True
                                while ships_to_buy and sum([item.price for item in ships_to_buy]) > current_urpt:
                                    ships_to_buy.pop()
                                if ships_to_buy:
                                    for item in ships_to_buy:
                                        self.event_shop_buy_item(item)
                                    logger.info(
                                        f"[Магазин события] Корабли успешно куплены: {[str(item) for item in ships_to_buy]}")
                                    break
                                else:
                                    logger.warning("[Магазин события] Текущих UR-очков недостаточно для покупки кораблей; покупка пропущена")
                                    break
                            else:
                                logger.warning("[Магазин события] Даже после покупки всех UR-очков средств недостаточно; самый дорогой корабль пропущен")
                                ships_to_buy.pop()

        if urpt_preserve:
            logger.info("[Магазин события] Из-за резерва UR-очков на корабли покупка UR-очков и ресурсов пропущена")
            return other_items, []
        else:
            logger.info("[Магазин события] UR-очки и ресурсы за UR-очки будут куплены в последнюю очередь")
            return other_items, urpt_items + coin_items

    def handle_unobtained_items(self, items: List[EventShopItem], buy_unobtained_items=False) \
            -> Tuple[List[EventShopItem], List[EventShopItem]]:
        """
        Buy all items (ships) with tag "unobtained" in the event shop.
        This should be done after handling URpt-related items but before buying other items.

        For items with stock more than 1, should buy only one and let filter string decide whether to buy more.
        The second return value will contain items with stock more than 1 that have been bought once,
        so that the caller can (and maybe should) rescan the shop.

        Args:
            items (List[EventShopItem]): List of items to buy.
            buy_unobtained_items (bool): Whether to buy unobtained items. Default is False.
        """
        if not buy_unobtained_items:
            return items, []
        unobtained_items = []
        other_items = []
        for item in items:
            if item.tag == "unobtained":
                unobtained_items.append(item)
            else:
                other_items.append(item)
        if not unobtained_items:
            return other_items, []
        if not self.is_event_ended:
            logger.info("[Магазин события] Событие ещё не завершено; PT резервируются для неполученных товаров. Можно также дождаться выпадения на карте события")
            self.preserve_pt(sum(item.price for item in unobtained_items))
            return other_items, []

        multiple_items = []
        logger.info(f"[Магазин события] Попытка купить неполученные товары: {[str(item) for item in unobtained_items]}")
        for item in unobtained_items:
            self.event_shop_buy_item(item)
            logger.info(f"[Магазин события] Неполученный товар успешно куплен: {str(item)}")
            if item.count > 1:
                item.count -= 1
                multiple_items.append(item)
            else:
                # If the item has stock 1, it won't appear in the rescan.
                pass

        return items, multiple_items

    def calculate_affordable_amount(self, item: EventShopItem) -> int:
        if item.name == "Oil":
            current_oil = self.get_oil()
            return min(item.count, (self.pt - self.pt_preserved) // item.price, (25000 - current_oil) // 1000)
        if item.cost == 'URpt':
            return min(item.count, self.urpt // item.price)
        elif item.cost == 'pt':
            return min(item.count, (self.pt - self.pt_preserved) // item.price)
        else:
            logger.error(f"[Магазин события] Неизвестный тип стоимости: {item.cost}, товар: {str(item)}")
            return 0

    @staticmethod
    def item_filter_key(item: EventShopItem) -> str:
        return ''.join(str(value or '') for value in (item.group, item.sub_genre, item.tier))

    @staticmethod
    def item_filter_amount_key(item: EventShopItem, filter_amount: dict) -> str:
        keys = [
            ''.join(str(value or '') for value in (item.group, item.sub_genre, item.tier)),
            ''.join(str(value or '') for value in (item.group, item.sub_genre)),
            str(item.group or ''),
        ]
        for key in keys:
            if key in filter_amount:
                return key
        return ''

    def _run(self):
        """
        Pages:
            in: shop_event
        """
        self.event_shop_load_ensure()
        items = self.scan_all()
        try:
            from module.event_datamine.artifact import load_builtin_artifact
            from module.webui.event_shop_observation import persist_event_shop_observation

            observation_spec = load_builtin_artifact()["event_spec"]
            persist_event_shop_observation(
                instance=self.config.config_name,
                spec=observation_spec,
                runtime_items=items,
            )
        except (OSError, TypeError, ValueError) as exc:
            # Observation is additive evidence. Its storage failure must not
            # silently change the established EventShop purchase policy.
            logger.warning(f"[Магазин события — наблюдение] Не удалось сохранить snapshot: {exc}")
        if not len(items):
            logger.warning("[Магазин события] Товары в магазине события не найдены")
            return True
        logger.hr("Покупки в магазине события", level=2)
        self.get_current_pts()
        items, urpt_related_items = self.handle_items_related_with_urpt(items, self.config.EventShop_BuyURShip)
        self.get_current_pts()
        items, unobtained_multiple_stock_items = self.handle_unobtained_items(items, self.config.EventShop_UnlockSSRShip)
        items += unobtained_multiple_stock_items

        if self.config.EventShop_PresetFilter == 'custom':
            filter = self.config.EventShop_CustomFilter
        else:
            filter = EVENT_SHOP_PRESET_FILTER[self.config.EventShop_PresetFilter]
        filter_amount = parse_filter_amount(filter)
        filter_tokens = parse_filter_tokens(filter)
        FILTER.load(strip_filter_amount(filter))
        items = FILTER.apply(items)
        items += urpt_related_items
        if not len(items):
            logger.info("[Магазин события] После фильтрации нет доступных для покупки товаров")
            return True
        logger.attr('Сортировка товаров', ' > '.join([str(item) for item in items]))
        self.get_current_pts()
        logger.attr("Зарезервировано PT", self.pt_preserved)
        bought_amount = {}
        for item in items:
            logger.hr(f"Попытка купить товар: {str(item)}", level=3)
            filter_amount_key = self.item_filter_amount_key(item, filter_amount)
            amount_limit = filter_amount.get(filter_amount_key)
            already_bought = bought_amount.get(filter_amount_key, 0) if filter_amount_key else 0
            remaining_limit = (
                None
                if amount_limit is None
                else max(amount_limit - already_bought, 0)
            )
            if remaining_limit is not None and remaining_limit <= 0:
                logger.info(f"[Магазин события] Достигнут лимит количества по фильтру: {str(item)}")
                continue

            affordable_amount = self.calculate_affordable_amount(item)
            target_amount = item.count if remaining_limit is None else min(item.count, remaining_limit)
            buy_amount = min(affordable_amount, target_amount)
            if buy_amount <= 0:
                logger.warning(f"[Магазин события] Невозможно купить товар: {str(item)}")
                if self.is_event_ended:
                    logger.info("[Магазин события] Событие завершено; товар пропущен, продолжается попытка купить другие товары")
                    continue
                else:
                    logger.info("[Магазин события] Событие ещё не завершено; дальнейшие покупки остановлены во избежание перерасхода")
                    break
            elif buy_amount < target_amount:
                logger.warning(f"[Магазин события] Можно купить только {buy_amount} шт.: {str(item)}")
                self.event_shop_buy_item(item, amount=buy_amount)
                if filter_amount_key:
                    bought_amount[filter_amount_key] = already_bought + buy_amount
                if self.is_event_ended:
                    logger.info("[Магазин события] Событие завершено; продолжается попытка купить другие товары")
                    self.get_current_pts()
                    continue
                else:
                    logger.info("[Магазин события] Событие ещё не завершено; дальнейшие покупки остановлены во избежание перерасхода")
                    break
            else:
                if buy_amount < item.count:
                    self.event_shop_buy_item(item, amount=buy_amount)
                else:
                    self.event_shop_buy_item(item)
                if filter_amount_key:
                    bought_amount[filter_amount_key] = already_bought + buy_amount
                logger.info(f"[Магазин события] Товар успешно куплен: {str(item)}")
                self.get_current_pts()

        # Consume custom filter amounts based on actual purchased quantities.
        if self.config.EventShop_PresetFilter == 'custom' and filter_tokens:
            changed = False
            for token in filter_tokens:
                amount = token.get('amount')
                key = token.get('key')
                if amount is None or not key:
                    continue
                consumed = int(bought_amount.get(key, 0))
                if consumed <= 0:
                    continue
                token['amount'] = max(int(amount) - consumed, 0)
                changed = True
            if changed:
                new_filter = rebuild_filter_tokens(filter_tokens)
                logger.attr('Израсходованный фильтр магазина события', new_filter if new_filter else '(пусто)')
                self.config.EventShop_CustomFilter = new_filter
                if not new_filter.strip():
                    logger.info('[Магазин события] Пользовательский фильтр полностью израсходован; задача магазина события отключена')
                    self.config.Scheduler_Enable = False
                    self.config.task_stop()
        return True

    def run(self):
        """
        There may be multiple event shops.
        This function will iterate through all of them and perform the necessary operations.
        """
        self.ui_goto_main()
        self.ui_ensure(page_shop)
        timeout = Timer(2, count=4)
        for _ in self.loop():
            if self.appear(page_munitions.check_button, threshold=20):
                break
            if timeout.reached():
                self.device.click(SHOP_GOTO_MUNITIONS)
                timeout.reset()

        if self.appear(NAV_GENERAL, offset=(5, 5)):
            if self.appear(NO_NAV_EVENT_CHECK, offset=(5, 5)):
                logger.info("[Магазин события] Активный магазин события отсутствует; задача завершена")
                self.config.task_delay(server_update=True)
                return False
            else:
                self.ui_click(NAV_EVENT, check_button=NAV_EVENT, appear_button=NAV_GENERAL)

        count, navbar = self.event_shop_tab_count_and_navbar
        logger.info(f"[Магазин события] Обнаружено магазинов события: {count}; начало обработки")
        for i in range(count):
            navbar.set(main=self, left=i + 1)
            for _ in range(7):  # Refresh up to 7 times to deal with buying failures
                try:
                    self.pt_preserved = 0
                    self._run()
                    if self.config.task_switched():
                        return True
                    break
                except ItemNotFoundError:
                    if count >= 2:
                        navbar.set(main=self, left=((i + 1) % count) + 1)
                        navbar.set(main=self, left=i + 1)
                    else:
                        self.ui_click(NAV_GENERAL, check_button=NAV_GENERAL, appear_button=NAV_EVENT)
                        self.ui_click(NAV_EVENT, check_button=NAV_EVENT, appear_button=NAV_GENERAL)
                    continue
            del_cached_property(self, 'is_event_ended')
            del_cached_property(self, 'event_shop_has_urpt')
            if self.config.task_switched():
                return True
        self.config.task_delay(server_update=True)
        return True
