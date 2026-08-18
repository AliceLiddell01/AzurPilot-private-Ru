"""Автоматизация покупок в магазине события.

Модуль управляет балансами PT/URpt, покупкой UR-кораблей и обменом URpt,
приоритетом неполученных товаров, пакетными покупками по предустановленному или
пользовательскому фильтру и обходом нескольких вкладок магазина события.
"""
from typing import List, Tuple

from module.base.decorator import del_cached_property
from module.base.timer import Timer
from module.config.time_source import now as current_time
from module.event_datamine.registry import EventArtifactRegistry
from module.logger import logger
from module.shop.assets import NAV_EVENT, NAV_GENERAL
from module.shop_event.assets import NO_NAV_EVENT_CHECK
from module.shop_event.clerk import EventShopClerk, ItemNotFoundError
from module.shop_event.item import (
    COIN_PRICE_IN_URPT,
    UR_SHIP_PRICES_IN_URPT,
    URPT_PRICE_IN_PT,
    EventShopItem,
)
from module.shop_event.selector import (
    EVENT_SHOP_PRESET_FILTER,
    FILTER,
    parse_filter_amount,
    parse_filter_tokens,
    rebuild_filter_tokens,
    strip_filter_amount,
)
from module.ui.assets import SHOP_GOTO_MUNITIONS
from module.ui.page import page_munitions, page_shop


class EventShop(EventShopClerk):
    """Контроллер полного цикла покупок в магазине события.

    Последовательно получает текущие PT/URpt, обрабатывает связанные с URpt
    товары, неполученные позиции, обычный фильтр покупок и при необходимости
    расходует лимиты пользовательского фильтра. Поддерживает несколько вкладок
    магазина, обрабатывая каждую отдельно.
    """
    pt = 0
    urpt = 0
    pt_preserved = 0
    _event_shop_current_artifact = None
    _event_shop_current_artifact_resolved = False

    def _begin_event_shop_pass_context(self):
        """Разрешить current Event artifact один раз для текущего полного прохода."""
        self._event_shop_current_artifact_resolved = True
        try:
            self._event_shop_current_artifact = EventArtifactRegistry().resolve_current(
                "EN", current_time()
            )
        except (OSError, TypeError, ValueError) as exc:
            self._event_shop_current_artifact = None
            logger.warning(
                f"[Магазин события — контекст] Не удалось разрешить current Event artifact: {exc}"
            )
        return self._event_shop_current_artifact

    def _current_event_artifact(self):
        """Вернуть artifact текущего прохода, лениво создав контекст вне _run()."""
        if not getattr(self, "_event_shop_current_artifact_resolved", False):
            return self._begin_event_shop_pass_context()
        return self._event_shop_current_artifact

    def event_shop_buy_item(self, item_to_buy, amount=None):
        # До клика работаем fail-closed: даже частично успешная покупка
        # не должна оставлять снимок до покупки помеченным как свежий.
        try:
            from module.webui.event_shop_observation import (
                invalidate_event_shop_observation,
            )

            artifact = self._current_event_artifact()
            if artifact is not None:
                spec = artifact["event_spec"]
                invalidate_event_shop_observation(
                    instance=self.config.config_name,
                    event_id=str(spec.get("id") or ""),
                    server=str(spec.get("server") or "EN"),
                    source_revision=str(
                        spec.get("provenance", {}).get("revision") or ""
                    ),
                )
        except (OSError, TypeError, ValueError) as exc:
            logger.warning(f"[Магазин события — наблюдение] Не удалось инвалидировать snapshot: {exc}")
        return super().event_shop_buy_item(item_to_buy, amount=amount)

    def get_current_pts(self):
        self.pt = self.event_shop_get_pt()
        if self.event_shop_has_urpt:
            self.urpt = self.event_shop_get_urpt()

        try:
            from module.log_res.log_res import LogRes

            LogRes(config=self.config).Pt = self.pt
        except Exception as exc:
            logger.warning(
                f"[Магазин события — ресурсы] Не удалось обновить PT в журнале: {exc}"
            )

        try:
            from module.webui.event_observation_update import (
                persist_current_pt_observation,
            )

            artifact = self._current_event_artifact()
            if artifact is not None:
                spec = artifact["event_spec"]
                persist_current_pt_observation(
                    instance=self.config.config_name,
                    event_id=str(spec.get("id") or ""),
                    server=str(spec.get("server") or "EN"),
                    source_revision=str(
                        spec.get("provenance", {}).get("revision") or ""
                    ),
                    value=self.pt,
                    source="event_shop_ocr",
                )
        except (OSError, TypeError, ValueError) as exc:
            logger.warning(
                f"[Магазин события — наблюдение] Не удалось сохранить OCR PT: {exc}"
            )

    def preserve_pt(self, amount: int):
        """Зарезервировать PT для последующих покупок."""
        self.pt_preserved += amount
        logger.info(f"[Магазин события] Зарезервировано {amount} PT для последующего использования. Всего зарезервировано PT: {self.pt_preserved}")

    def handle_items_related_with_urpt(self, items: List[EventShopItem], num_of_ships_to_buy: int = 2) \
            -> Tuple[List[EventShopItem], List[EventShopItem]]:
        """Обработать URpt-товары до обычных покупок.

        При необходимости резервирует PT или покупает URpt, чтобы приобрести
        заданное число UR-кораблей. Возвращает обычные товары и отдельный список
        URpt/монет, который следует обрабатывать последним.
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

        # Сначала покупаем корабли.
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
        """Купить товары с меткой ``unobtained`` перед обычными покупками.

        Если у товара запас больше единицы, покупается только одна единица, а
        дальнейшее количество определяет фильтр. Второй список содержит такие
        частично обработанные товары, чтобы вызывающий код мог пересканировать
        магазин.
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
                # Товар с запасом 1 исчезнет после покупки и не попадёт в повторный скан.
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
        """Выполнить один полный проход текущей вкладки магазина события."""
        # Все чтения EventSpec в одном проходе используют один и тот же artifact.
        # Это исключает повторное чтение registry и расхождение identity внутри прохода.
        self._begin_event_shop_pass_context()
        self.event_shop_load_ensure()
        # PT — полноценное наблюдение каждого прохода EventShop, включая
        # проверочный проход, в котором новых кандидатов на покупку уже нет.
        self.get_current_pts()
        items = self.scan_all()
        try:
            from module.webui.event_shop_observation import (
                persist_event_shop_observation,
            )

            artifact = self._current_event_artifact()
            if artifact is not None:
                persist_event_shop_observation(
                    instance=self.config.config_name,
                    spec=artifact["event_spec"],
                    runtime_items=items,
                )
        except (OSError, TypeError, ValueError) as exc:
            # Наблюдение является дополнительным evidence: сбой его хранилища не
            # должен незаметно менять уже установленную политику покупок EventShop.
            logger.warning(f"[Магазин события — наблюдение] Не удалось сохранить snapshot: {exc}")
        if not len(items):
            observation_items = getattr(items, "observation_items", items)
            if observation_items:
                logger.info(
                    "[Магазин события] Нет товаров, требующих покупки по текущим целям и приоритетам"
                )
            else:
                logger.warning("[Магазин события] Товары в магазине события не найдены")
            return True
        logger.hr("Покупки в магазине события", level=2)
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

        # Уменьшаем лимиты пользовательского фильтра на реально купленное количество.
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
        """Обойти все вкладки магазина события и выполнить необходимые операции."""
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
            for _ in range(7):  # До семи обновлений на случай неудачных покупок.
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
