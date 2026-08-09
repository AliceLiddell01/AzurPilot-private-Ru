"""Operation Siren voucher shop and Data Logger monthly lifecycle."""

from module.base.timer import Timer
from module.base.utils import random_rectangle_point, rgb2gray
from module.combat.assets import GET_ITEMS_1, GET_ITEMS_2
from module.config.opsi_data_logger import (
    DATA_LOGGER_MAX_FAILURES_PER_CYCLE,
    DATA_LOGGER_NAME,
    DataLoggerLifecycleEvidence,
    DataLoggerPurchaseEvidence,
    DataLoggerShopResult,
    DataLoggerShopState,
    DataLoggerStorageState,
    data_logger_clear_evidence,
    data_logger_clear_retry,
    data_logger_intent_enabled,
    data_logger_is_active,
    data_logger_lifecycle_evidence,
    data_logger_mark_active,
    data_logger_mark_evidence,
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
DATA_LOGGER_STORAGE_TRANSITION_SECONDS = 3
DATA_LOGGER_STORAGE_USE_SECONDS = 25
DATA_LOGGER_STORAGE_NO_SCROLLBAR_FRAMES = 3
DATA_LOGGER_ACTIVATION_ABSENT_FRAMES = 3
DATA_LOGGER_REWARD_GRACE_SECONDS = 4


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
            f'[{DATA_LOGGER_NAME}] Жизненный цикл не удалось подтвердить после '
            f'{failure_count} попыток; задача приостановлена до следующего ежемесячного сброса: '
            f'{reason}'
        )
        self.config.task_delay(target=next_reset)

    def _data_logger_schedule_retry(self, reason):
        failure_count = data_logger_set_retry(self.config, reason=reason)
        if failure_count >= DATA_LOGGER_MAX_FAILURES_PER_CYCLE:
            self._data_logger_schedule_failure_pause(reason, failure_count)
            return

        logger.warning(
            f'[{DATA_LOGGER_NAME}] Жизненный цикл не завершён; неподтверждённая попытка '
            f'{failure_count}/{DATA_LOGGER_MAX_FAILURES_PER_CYCLE}, повтор не '
            f'позднее чем через {DATA_LOGGER_RETRY_MINUTES} мин: {reason}'
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
        logger.info('Магазин ваучеров завершён, задача отложена до следующего сброса')
        logger.attr('Следующий сброс Операции «Сирена»', next_reset)
        self.config.task_delay(target=next_reset)

    def _data_logger_ensure_port_map(self):
        """Ensure a stable local map in an allied port before Storage input."""
        logger.info(f'[{DATA_LOGGER_NAME}] Проверка обязательного условия: союзный порт')
        if self.is_in_globe():
            self.os_globe_goto_map()
        if not self.is_in_map():
            logger.warning(f'[{DATA_LOGGER_NAME}] Локальная карта Операции «Сирена» не открыта')
            return False

        self.zone_init()
        if not self.zone.is_azur_port:
            target = self.zone_nearest_azur_port(self.zone)
            logger.info(f'[{DATA_LOGGER_NAME}] Переход в союзный порт {target}')
            self.globe_goto(target)
            self.zone_init()
            if not self.zone.is_azur_port:
                logger.warning(
                    f'[{DATA_LOGGER_NAME}] Переход в союзный порт не подтверждён'
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
                    logger.info(f'[{DATA_LOGGER_NAME}] Локальная карта союзного порта подтверждена')
                    return True
            else:
                stable_frames = 0
        logger.warning(f'[{DATA_LOGGER_NAME}] Локальная карта союзного порта нестабильна')
        return False

    def _data_logger_storage_enter(self):
        """Enter real Storage with a local timeout and map-state invariant."""
        if not self._data_logger_ensure_port_map():
            return False

        logger.info(f'[{DATA_LOGGER_NAME}] Вход в Storage')
        timeout = Timer.from_seconds(DATA_LOGGER_STORAGE_ENTER_SECONDS).start()
        transition = None
        self.interval_clear(STORAGE_ENTER)
        while not timeout.reached():
            self.device.screenshot()
            if self.is_in_storage():
                self.handle_info_bar()
                logger.info(f'[{DATA_LOGGER_NAME}] Storage подтверждён')
                return True

            if self.appear(MISSION_CHECK, offset=(20, 20)):
                logger.warning(
                    f'[{DATA_LOGGER_NAME}] Вместо Storage открыта сводка; закрытие сводки'
                )
                self.device.click(MISSION_QUIT)
                transition = None
                continue

            if self.appear_then_click(
                AUTO_SEARCH_REWARD,
                offset=(50, 50),
                interval=3,
            ):
                continue
            if self.handle_map_event():
                continue

            in_allied_port_map = self.is_in_map() and self.zone.is_azur_port
            if not in_allied_port_map:
                if transition is not None and not transition.reached():
                    continue
                logger.warning(
                    f'[{DATA_LOGGER_NAME}] Во время входа в Storage нарушен инвариант '
                    'карты союзного порта'
                )
                return False
            if transition is not None:
                if not transition.reached():
                    continue
                transition = None

            # EN port layouts can shift the Storage button by roughly one menu
            # slot. Use the same horizontal tolerance as the inherited Storage
            # handler while keeping this lifecycle bounded by a local timeout.
            if self.appear_then_click(
                STORAGE_ENTER,
                offset=(200, 5),
                interval=3,
            ):
                transition = Timer.from_seconds(
                    DATA_LOGGER_STORAGE_TRANSITION_SECONDS
                ).start()
                continue

        logger.warning(
            f'[{DATA_LOGGER_NAME}] Истекло время входа в Storage: '
            f'{DATA_LOGGER_STORAGE_ENTER_SECONDS} с'
        )
        return False

    def _data_logger_storage_quit(self):
        """Drain reward popups and leave Storage without click-spam."""
        timeout = Timer.from_seconds(8).start()
        map_stable_frames = 0
        self.interval_clear(BACK_ARROW)
        while not timeout.reached():
            self.device.screenshot()

            # Data Logger activation can enqueue several adaptability rewards.
            # They must be dismissed before the Storage back button is used.
            if self.handle_map_event():
                map_stable_frames = 0
                continue

            if self.is_in_map():
                map_stable_frames += 1
                if map_stable_frames >= 3:
                    return True
                self.device.sleep(0.2)
                continue

            map_stable_frames = 0
            if self.is_in_storage() and self.appear_then_click(
                BACK_ARROW,
                offset=(20, 20),
                interval=2,
            ):
                continue

        logger.warning(f'[{DATA_LOGGER_NAME}] Не удалось подтвердить выход из Storage')
        return False

    def _data_logger_storage_items(self):
        image = rgb2gray(self.device.image)
        return TEMPLATE_STORAGE_LOGGER_UNLOCK.match_multi(
            image,
            similarity=0.75,
        )

    def _data_logger_record_evidence(self, evidence):
        """Persist exact-item evidence when running with a real task config."""
        config = getattr(self, 'config', None)
        if config is None:
            return evidence
        return data_logger_mark_evidence(config, evidence)

    def _data_logger_storage_scroll_bottom(self):
        timeout = Timer.from_seconds(6).start()
        drag_count = 0
        no_scrollbar_frames = 0
        while not timeout.reached() and drag_count < 4:
            self.device.screenshot()
            if not self.is_in_storage():
                return False
            if not SCROLL_STORAGE.appear(main=self):
                no_scrollbar_frames += 1
                if no_scrollbar_frames >= DATA_LOGGER_STORAGE_NO_SCROLLBAR_FRAMES:
                    return True
                self.device.sleep(0.2)
                continue

            no_scrollbar_frames = 0
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
                f'[{DATA_LOGGER_NAME}] Не удалось подтвердить нижнюю границу Storage'
            )
            return None

        empty_frames = 0
        timeout = Timer.from_seconds(8).start()
        while not timeout.reached():
            self.device.screenshot()
            if not self.is_in_storage():
                return None
            items = self._data_logger_storage_items()
            logger.attr(f'{DATA_LOGGER_NAME}: совпадения в Storage', len(items))
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
        reward_grace = None
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
                self._data_logger_record_evidence(
                    DataLoggerLifecycleEvidence.USE_CLICKED
                )
                absent_after_use_frames = 0
                reward_grace = Timer.from_seconds(
                    DATA_LOGGER_REWARD_GRACE_SECONDS
                ).start()
                continue
            if self.appear_then_click(BOX_USE, offset=(180, 30), interval=2):
                use_clicked = True
                self._data_logger_record_evidence(
                    DataLoggerLifecycleEvidence.USE_CLICKED
                )
                absent_after_use_frames = 0
                reward_grace = Timer.from_seconds(
                    DATA_LOGGER_REWARD_GRACE_SECONDS
                ).start()
                continue
            if self.appear_then_click(GET_ITEMS_1, interval=2):
                if use_clicked:
                    success_observed = True
                else:
                    logger.warning(
                        f'[{DATA_LOGGER_NAME}] Общий экран награды до Use проигнорирован'
                    )
                continue
            if self.appear_then_click(GET_ITEMS_2, interval=2):
                if use_clicked:
                    success_observed = True
                else:
                    logger.warning(
                        f'[{DATA_LOGGER_NAME}] Общий экран награды до Use проигнорирован'
                    )
                continue
            if self.appear(GET_ADAPTABILITY, offset=5, interval=2):
                self.device.click(CLICK_SAFE_AREA)
                if use_clicked:
                    success_observed = True
                else:
                    logger.warning(
                        f'[{DATA_LOGGER_NAME}] Награда адаптивности до Use проигнорирована'
                    )
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
                        f'[{DATA_LOGGER_NAME}] Активация подтверждена экраном успеха '
                        'после Use'
                    )
                    return DataLoggerStorageState.ACTIVATED

                if use_clicked:
                    absent_after_use_frames += 1
                    # The item disappears immediately, while the three
                    # adaptability reward popups arrive after a short animation.
                    # Keep observing until that transition had time to appear.
                    if reward_grace is None or not reward_grace.reached():
                        continue
                    if (
                        absent_after_use_frames
                        >= DATA_LOGGER_ACTIVATION_ABSENT_FRAMES
                    ):
                        logger.info(
                            f'[{DATA_LOGGER_NAME}] Активация подтверждена после Use: '
                            'истёк интервал ожидания награды, а предмет стабильно отсутствует'
                        )
                        return DataLoggerStorageState.ACTIVATED
                    continue

                if item_selected:
                    # Selecting the item can hide the list behind a modal. The
                    # disappearance is not evidence that Use was clicked.
                    continue

        logger.warning(f'[{DATA_LOGGER_NAME}] Не удалось подтвердить активацию в Storage')
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
                    f'[{DATA_LOGGER_NAME}] Предмет отсутствует в Storage; '
                    'само отсутствие не считается доказательством активации'
                )
                return DataLoggerStorageState.ABSENT
            self._data_logger_record_evidence(
                DataLoggerLifecycleEvidence.STORAGE_OBSERVED
            )
            return self._data_logger_storage_activate_item()
        finally:
            # Cleanup must never overwrite a confirmed activation. A delayed
            # reward popup can make Storage exit fail even though Use succeeded.
            try:
                self._data_logger_storage_quit()
            except Exception as exc:
                logger.exception(
                    f'[{DATA_LOGGER_NAME}] Не удалось очистить Storage после определения '
                    f'результата жизненного цикла: {exc}'
                )

    def _data_logger_shop_lifecycle(self, shop):
        try:
            return shop.ensure_data_logger()
        except Exception as exc:
            logger.exception(f'[{DATA_LOGGER_NAME}] Не удалось проверить магазин: {exc}')
            return DataLoggerShopResult(
                state=DataLoggerShopState.UNKNOWN,
                reason=f'exception:{type(exc).__name__}',
            )

    @staticmethod
    def _data_logger_should_probe_storage(shop_result, retry_only):
        """Probe Storage when shop evidence cannot settle exact-item ownership."""
        if shop_result.state is DataLoggerShopState.SOLD_OUT:
            return True
        if (
            shop_result.purchase_evidence
            is not DataLoggerPurchaseEvidence.NONE
        ):
            return True
        return (
            shop_result.state is DataLoggerShopState.UNKNOWN
            and shop_result.reason in {
                'full_scan_inconclusive',
                'target_not_observed',
            }
        )

    @staticmethod
    def _data_logger_full_absence_confirms_activation(
        shop_result,
        storage_state,
        lifecycle_evidence=DataLoggerLifecycleEvidence.NONE,
    ):
        """Recover only from persisted evidence that the exact item existed.

        Fresh shop absence plus fresh Storage absence is not proof of ownership
        or activation. Recovery is allowed only when the same server cycle has
        already persisted an exact Storage observation or a confirmed Use click,
        and the current shop result is compatible with the item having left the
        purchase lifecycle.
        """
        if storage_state is not DataLoggerStorageState.ABSENT:
            return False
        if lifecycle_evidence not in {
            DataLoggerLifecycleEvidence.STORAGE_OBSERVED,
            DataLoggerLifecycleEvidence.USE_CLICKED,
        }:
            return False
        if shop_result.state is DataLoggerShopState.SOLD_OUT:
            return True
        if shop_result.state is not DataLoggerShopState.UNKNOWN:
            return False
        return shop_result.reason in {
            'full_scan_inconclusive',
            'purchase_not_confirmed:full_scan_inconclusive',
        }

    def os_voucher(self):
        logger.hr('Операция «Сирена» — магазин ваучеров', level=1)
        intent = data_logger_intent_enabled(self.config)
        active = data_logger_is_active(self.config)
        retry_only = data_logger_retry_pending(self.config)
        failure_count = data_logger_retry_count(self.config)
        lifecycle_evidence = data_logger_lifecycle_evidence(self.config)
        logger.info(
            f'[{DATA_LOGGER_NAME}] видимое намерение={intent}, '
            f'активен в текущем месяце={active}, только повтор={retry_only}, '
            f'ошибок={failure_count}, '
            f'доказательство={lifecycle_evidence.value}'
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
            data_logger_clear_evidence(self.config)
            retry_only = False
            logger.info(f'[{DATA_LOGGER_NAME}] Автоматизация отключена пользователем')
        else:
            logger.info(f'[{DATA_LOGGER_NAME}] Текущий ежемесячный цикл уже подтверждён')

        if retry_only:
            logger.info(
                f'[{DATA_LOGGER_NAME}] Запуск только для повтора: обычный фильтр ваучеров пропущен'
            )
        else:
            shop.run()

        self._os_voucher_exit()

        if shop_result is not None:
            if self._data_logger_should_probe_storage(shop_result, retry_only):
                logger.info(
                    f'[{DATA_LOGGER_NAME}] Продолжение проверкой Storage: '
                    f'состояние магазина={shop_result.state.value}, '
                    f'причина={shop_result.reason}, '
                    f'доказательство покупки={shop_result.purchase_evidence.value}, '
                    f'только повтор={retry_only}'
                )
                try:
                    storage_state = self._data_logger_storage_lifecycle()
                except Exception as exc:
                    logger.exception(f'[{DATA_LOGGER_NAME}] Ошибка жизненного цикла Storage: {exc}')
                    storage_state = DataLoggerStorageState.UNKNOWN

                lifecycle_evidence = data_logger_lifecycle_evidence(self.config)
                recovered_from_absence = (
                    self._data_logger_full_absence_confirms_activation(
                        shop_result,
                        storage_state,
                        lifecycle_evidence,
                    )
                )
                if recovered_from_absence:
                    logger.info(
                        f'[{DATA_LOGGER_NAME}] Активация восстановлена по сохранённому '
                        f'доказательству точного предмета={lifecycle_evidence.value}, полному '
                        'сканированию магазина и подтверждённому отсутствию в Storage'
                    )

                if (
                    storage_state is DataLoggerStorageState.ACTIVATED
                    or recovered_from_absence
                ):
                    cycle_key = data_logger_mark_active(self.config)
                    logger.info(
                        f'[{DATA_LOGGER_NAME}] Ежемесячный успех сохранён для серверного цикла '
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
