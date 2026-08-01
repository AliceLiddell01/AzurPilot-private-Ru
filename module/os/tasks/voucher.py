"""Operation Siren voucher shop and Data Logger monthly lifecycle."""

from module.base.timer import Timer
from module.base.utils import random_rectangle_point, rgb2gray
from module.combat.assets import GET_ITEMS_1, GET_ITEMS_2
from module.config.opsi_data_logger import (
    DATA_LOGGER_MAX_FAILURES_PER_CYCLE,
    DATA_LOGGER_NAME,
    DataLoggerShopResult,
    DataLoggerShopState,
    DataLoggerStorageState,
    data_logger_clear_retry,
    data_logger_intent_enabled,
    data_logger_is_active,
    data_logger_mark_active,
    data_logger_retry_count,
    data_logger_retry_pending,
    data_logger_set_retry,
)
from module.config.utils import get_os_next_reset
from module.handler.assets import GET_MISSION
from module.logger import logger
from module.os.map import OSMap
from module.os_handler.assets import (
    AUTO_SEARCH_REWARD,
    CLICK_SAFE_AREA,
    EXCHANGE_CHECK,
    EXCHANGE_ENTER,
    GET_ADAPTABILITY,
    MISSION_CHECK,
    MISSION_QUIT,
    STORAGE_CHECK,
    STORAGE_ENTER,
    STORAGE_USE,
    TEMPLATE_STORAGE_LOGGER_UNLOCK,
)
from module.os_handler.storage import SCROLL_STORAGE
from module.shop.shop_voucher import VoucherShop
from module.storage.assets import BOX_USE
from module.ui.assets import BACK_ARROW

DATA_LOGGER_RETRY_MINUTES = 360
DATA_LOGGER_STORAGE_ENTER_SECONDS = 15
DATA_LOGGER_STORAGE_USE_SECONDS = 25
DATA_LOGGER_ACTIVATION_ABSENT_FRAMES = 3


class OpsiVoucher(OSMap):
    def _create_voucher_shop(self):
        return VoucherShop(self.config, self.device)

    def _os_voucher_enter(self):
        self.os_map_goto_globe(unpin=False)
        self.ui_click(
            click_button=EXCHANGE_ENTER,
            check_button=EXCHANGE_CHECK,
            offset=(200, 20),
            retry_wait=3,
            skip_first_screenshot=True,
        )

    def _os_voucher_exit(self):
        self.ui_back(
            check_button=EXCHANGE_ENTER,
            appear_button=EXCHANGE_CHECK,
            offset=(200, 20),
            retry_wait=3,
            skip_first_screenshot=True,
        )
        self.os_globe_goto_map()

    def _data_logger_schedule_failure_pause(self, reason, failure_count):
        next_reset = get_os_next_reset()
        logger.error(
            f'[{DATA_LOGGER_NAME}] lifecycle remained unverifiable after '
            f'{failure_count} attempts; paused until the next monthly reset: '
            f'{reason}'
        )
        self.config.task_delay(target=next_reset)

    def _data_logger_schedule_retry(self, reason):
        failure_count = data_logger_set_retry(self.config, reason=reason)
        if failure_count >= DATA_LOGGER_MAX_FAILURES_PER_CYCLE:
            self._data_logger_schedule_failure_pause(reason, failure_count)
            return

        logger.warning(
            f'[{DATA_LOGGER_NAME}] lifecycle incomplete; unresolved attempt '
            f'{failure_count}/{DATA_LOGGER_MAX_FAILURES_PER_CYCLE}, retry in no '
            f'more than {DATA_LOGGER_RETRY_MINUTES} minutes: {reason}'
        )
        # Retry after six hours, but never later than the next daily server
        # update. task_delay converts the configured server update to local
        # time and selects the nearest target.
        self.config.task_delay(
            minute=DATA_LOGGER_RETRY_MINUTES,
            server_update=True,
        )

    def _data_logger_schedule_month_reset(self):
        data_logger_clear_retry(self.config)
        next_reset = get_os_next_reset()
        logger.info('白票商店已完成，延迟到下次重置')
        logger.attr('大世界下次重置', next_reset)
        self.config.task_delay(target=next_reset)

    def _data_logger_ensure_port_map(self):
        """Ensure a stable local map in an allied port before Storage input."""
        logger.info(f'[{DATA_LOGGER_NAME}] validating allied-port prerequisite')
        if self.is_in_globe():
            self.os_globe_goto_map()
        if not self.is_in_map():
            logger.warning(f'[{DATA_LOGGER_NAME}] not on an Operation Siren local map')
            return False

        self.zone_init()
        if not self.zone.is_azur_port:
            target = self.zone_nearest_azur_port(self.zone)
            logger.info(f'[{DATA_LOGGER_NAME}] navigating to allied port {target}')
            self.globe_goto(target)
            self.zone_init()
            if not self.zone.is_azur_port:
                logger.warning(
                    f'[{DATA_LOGGER_NAME}] allied-port navigation was not confirmed'
                )
                return False

        stable_frames = 0
        timeout = Timer.from_seconds(8).start()
        while not timeout.reached():
            self.device.screenshot()
            if self.is_in_map() and self.zone.is_azur_port:
                stable_frames += 1
                self.device.sleep(0.5)
                if stable_frames >= 3:
                    logger.info(f'[{DATA_LOGGER_NAME}] allied-port local map confirmed')
                    return True
            else:
                stable_frames = 0
        logger.warning(f'[{DATA_LOGGER_NAME}] allied-port local map was not stable')
        return False

    def _data_logger_storage_enter(self):
        """Enter real Storage with a local timeout and strict button matching."""
        if not self._data_logger_ensure_port_map():
            return False

        logger.info(f'[{DATA_LOGGER_NAME}] entering Storage')
        timeout = Timer.from_seconds(DATA_LOGGER_STORAGE_ENTER_SECONDS).start()
        while not timeout.reached():
            self.device.screenshot()
            if self.is_in_storage():
                self.handle_info_bar()
                logger.info(f'[{DATA_LOGGER_NAME}] Storage confirmed')
                return True

            if self.appear(MISSION_CHECK, offset=(20, 20)):
                logger.warning(
                    f'[{DATA_LOGGER_NAME}] Overview opened instead of Storage; closing it'
                )
                self.device.click(MISSION_QUIT)
                continue

            if not self.is_in_map() or not self.zone.is_azur_port:
                logger.warning(
                    f'[{DATA_LOGGER_NAME}] lost allied-port local-map prerequisite'
                )
                return False

            if self.appear(STORAGE_ENTER, offset=(20, 20), interval=2):
                self.device.click(STORAGE_ENTER)
                continue
            if self.appear_then_click(AUTO_SEARCH_REWARD, offset=(50, 50), interval=2):
                continue

        logger.warning(
            f'[{DATA_LOGGER_NAME}] Storage entry timed out after '
            f'{DATA_LOGGER_STORAGE_ENTER_SECONDS} seconds'
        )
        return False

    def _data_logger_storage_quit(self):
        timeout = Timer.from_seconds(8).start()
        while not timeout.reached():
            self.device.screenshot()
            if self.is_in_map():
                return True
            if self.is_in_storage():
                self.device.click(BACK_ARROW)
                continue
        logger.warning(f'[{DATA_LOGGER_NAME}] failed to confirm Storage exit')
        return False

    def _data_logger_storage_items(self):
        image = rgb2gray(self.device.image)
        return TEMPLATE_STORAGE_LOGGER_UNLOCK.match_multi(
            image,
            similarity=0.75,
        )

    def _data_logger_storage_scroll_bottom(self):
        timeout = Timer.from_seconds(6).start()
        drag_count = 0
        while not timeout.reached() and drag_count < 4:
            self.device.screenshot()
            if not self.is_in_storage():
                return False
            if not SCROLL_STORAGE.appear(main=self):
                return True
            current = SCROLL_STORAGE.cal_position(main=self)
            if current > 1 - SCROLL_STORAGE.edge_threshold:
                return True
            if not SCROLL_STORAGE.length:
                return False
            start = random_rectangle_point(
                SCROLL_STORAGE.position_to_screen(current),
                n=1,
            )
            end = random_rectangle_point(
                SCROLL_STORAGE.position_to_screen(
                    1.0,
                    random_range=SCROLL_STORAGE.edge_add,
                ),
                n=1,
            )
            self.device.swipe(
                start,
                end,
                name='DATA_LOGGER_STORAGE_SCROLL',
                distance_check=False,
            )
            drag_count += 1

        self.device.screenshot()
        return (
            self.is_in_storage()
            and SCROLL_STORAGE.appear(main=self)
            and SCROLL_STORAGE.at_bottom(main=self)
        )

    def _data_logger_storage_scan(self):
        if not self._data_logger_storage_scroll_bottom():
            logger.warning(
                f'[{DATA_LOGGER_NAME}] Storage bottom could not be confirmed'
            )
            return None

        empty_frames = 0
        timeout = Timer.from_seconds(8).start()
        while not timeout.reached():
            self.device.screenshot()
            if not self.is_in_storage():
                return None
            items = self._data_logger_storage_items()
            logger.attr(f'{DATA_LOGGER_NAME} Storage matches', len(items))
            if items:
                return items
            empty_frames += 1
            if empty_frames >= 3:
                return []
        return None

    def _data_logger_storage_activate_item(self):
        item_selected = False
        use_clicked = False
        success_observed = False
        absent_after_use_frames = 0
        timeout = Timer.from_seconds(DATA_LOGGER_STORAGE_USE_SECONDS).start()
        self.interval_clear(STORAGE_CHECK)
        self.interval_clear(STORAGE_USE)
        self.interval_clear(GET_ITEMS_1)
        self.interval_clear(GET_ITEMS_2)
        self.interval_clear(GET_ADAPTABILITY)
        self.interval_clear(GET_MISSION)

        while not timeout.reached():
            self.device.screenshot()

            if self.appear(GET_MISSION, offset=True, interval=2):
                self.device.click(GET_MISSION)
                continue
            if self.appear_then_click(STORAGE_USE, offset=(180, 30), interval=2):
                use_clicked = True
                absent_after_use_frames = 0
                continue
            if self.appear_then_click(BOX_USE, offset=(180, 30), interval=2):
                use_clicked = True
                absent_after_use_frames = 0
                continue
            if self.appear_then_click(GET_ITEMS_1, interval=2):
                success_observed = True
                continue
            if self.appear_then_click(GET_ITEMS_2, interval=2):
                success_observed = True
                continue
            if self.appear(GET_ADAPTABILITY, offset=5, interval=2):
                self.device.click(CLICK_SAFE_AREA)
                success_observed = True
                continue
            if self.handle_story_skip():
                continue

            if self.is_in_storage():
                items = self._data_logger_storage_items()
                if items:
                    absent_after_use_frames = 0
                    if not item_selected:
                        self.device.click(items[0])
                        item_selected = True
                    continue

                if success_observed:
                    logger.info(
                        f'[{DATA_LOGGER_NAME}] activation confirmed by success UI'
                    )
                    return DataLoggerStorageState.ACTIVATED

                if use_clicked:
                    absent_after_use_frames += 1
                    if (
                        absent_after_use_frames
                        >= DATA_LOGGER_ACTIVATION_ABSENT_FRAMES
                    ):
                        logger.info(
                            f'[{DATA_LOGGER_NAME}] activation confirmed after Use '
                            'and stable item disappearance'
                        )
                        return DataLoggerStorageState.ACTIVATED
                    continue

                if item_selected:
                    # Selecting the item can hide the list behind a modal. The
                    # disappearance is not evidence that Use was clicked.
                    continue

        logger.warning(f'[{DATA_LOGGER_NAME}] Storage activation could not be confirmed')
        return DataLoggerStorageState.UNKNOWN

    def _data_logger_storage_lifecycle(self):
        if not self._data_logger_storage_enter():
            return DataLoggerStorageState.ENTER_TIMEOUT

        try:
            items = self._data_logger_storage_scan()
            if items is None:
                return DataLoggerStorageState.UNKNOWN
            if not items:
                logger.warning(
                    f'[{DATA_LOGGER_NAME}] item is absent in Storage; '
                    'absence alone is not accepted as proof of activation'
                )
                return DataLoggerStorageState.ABSENT
            return self._data_logger_storage_activate_item()
        finally:
            self._data_logger_storage_quit()

    def _data_logger_shop_lifecycle(self, shop):
        try:
            return shop.ensure_data_logger()
        except Exception as exc:
            logger.exception(f'[{DATA_LOGGER_NAME}] shop inspection failed: {exc}')
            return DataLoggerShopResult(
                state=DataLoggerShopState.UNKNOWN,
                reason=f'exception:{type(exc).__name__}',
            )

    def os_voucher(self):
        logger.hr('大世界-白票商店', level=1)
        intent = data_logger_intent_enabled(self.config)
        active = data_logger_is_active(self.config)
        retry_only = data_logger_retry_pending(self.config)
        failure_count = data_logger_retry_count(self.config)
        logger.info(
            f'[{DATA_LOGGER_NAME}] visible intent={intent}, '
            f'monthly active={active}, retry-only={retry_only}, '
            f'failures={failure_count}'
        )

        if (
            intent
            and not active
            and retry_only
            and failure_count >= DATA_LOGGER_MAX_FAILURES_PER_CYCLE
        ):
            self._data_logger_schedule_failure_pause(
                'retry_limit_reached',
                failure_count,
            )
            return

        self._os_voucher_enter()
        shop = self._create_voucher_shop()
        shop_result = None

        if intent and not active:
            shop_result = self._data_logger_shop_lifecycle(shop)
        elif not intent:
            data_logger_clear_retry(self.config)
            retry_only = False
            logger.info(f'[{DATA_LOGGER_NAME}] automation disabled by user')
        else:
            logger.info(f'[{DATA_LOGGER_NAME}] current monthly cycle already confirmed')

        if retry_only:
            logger.info(
                f'[{DATA_LOGGER_NAME}] retry-only run: ordinary voucher filter is skipped'
            )
        else:
            shop.run()

        self._os_voucher_exit()

        if shop_result is not None:
            if shop_result.state is DataLoggerShopState.SOLD_OUT:
                try:
                    storage_state = self._data_logger_storage_lifecycle()
                except Exception as exc:
                    logger.exception(f'[{DATA_LOGGER_NAME}] Storage lifecycle failed: {exc}')
                    storage_state = DataLoggerStorageState.UNKNOWN

                if storage_state is DataLoggerStorageState.ACTIVATED:
                    cycle_key = data_logger_mark_active(self.config)
                    logger.info(
                        f'[{DATA_LOGGER_NAME}] monthly success saved for server cycle '
                        f'{cycle_key}'
                    )
                    self._data_logger_schedule_month_reset()
                    return
                self._data_logger_schedule_retry(
                    f'storage_{storage_state.value}'
                )
                return

            self._data_logger_schedule_retry(
                f'shop_{shop_result.state.value}:{shop_result.reason}'
            )
            return

        self._data_logger_schedule_month_reset()
