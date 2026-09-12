"""大世界地图事件处理器。

处理大世界地图探索过程中触发的各类事件，包括战斗奖励弹窗、
故事跳过、舰队锁定开关、余烬信标弹窗、海域清除奖励以及
自动搜索奖励等，是大世界战斗和探索流程的基础事件层。
"""
from typing import Optional

from module.base.timer import Timer
from module.combat.assets import *
from module.exception import CampaignEnd
from module.handler.assets import POPUP_CANCEL, POPUP_CONFIRM, STORY_SKIP_3
from module.logger import logger
from module.os.assets import GLOBE_GOTO_MAP
from module.os_handler.assets import *
from module.os_handler.enemy_searching import EnemySearchingHandler
from module.statistics.azurstats import DropImage
from module.ui.assets import BACK_ARROW
from module.ui.switch import Switch


class FleetLockSwitch(Switch):
    def handle_additional(self, main):
        # Баг игры: появляется AUTO_SEARCH_REWARD из предыдущей уже очищенной области
        if main.appear_then_click(AUTO_SEARCH_REWARD, offset=(50, 50), interval=3):
            return True
        return False


fleet_lock = FleetLockSwitch('Fleet_Lock', offset=(10, 120))
fleet_lock.add_state('on', check_button=OS_FLEET_LOCKED)
fleet_lock.add_state('off', check_button=OS_FLEET_UNLOCKED)


class MapEventHandler(EnemySearchingHandler):
    ash_popup_canceled = False

    def handle_map_get_items(self, interval=2, drop=None):
        if self.is_in_map():
            return False

        if self.appear(GET_ITEMS_1, interval=interval):
            if drop:
                drop.handle_add(main=self, before=2)
            logger.info(f'{GET_ITEMS_1} -> {CLICK_SAFE_AREA}')
            self.device.click(CLICK_SAFE_AREA)
            return True
        if self.appear(GET_ITEMS_2, interval=interval):
            if drop:
                drop.handle_add(main=self, before=2)
            logger.info(f'{GET_ITEMS_2} -> {CLICK_SAFE_AREA}')
            self.device.click(CLICK_SAFE_AREA)
            return True
        if self.appear(GET_ITEMS_3, interval=interval):
            if drop:
                drop.handle_add(main=self, before=2)
            logger.info(f'{GET_ITEMS_3} -> {CLICK_SAFE_AREA}')
            self.device.click(CLICK_SAFE_AREA)
            return True
        if self.appear(GET_ADAPTABILITY, interval=interval):
            if drop:
                drop.handle_add(main=self, before=2)
            logger.info(f'{GET_ADAPTABILITY} -> {CLICK_SAFE_AREA}')
            self.device.click(CLICK_SAFE_AREA)
            return True
        if self.appear(GET_MEOWFFICER_ITEMS_1, interval=interval):
            if drop:
                drop.handle_add(main=self, before=2)
            logger.info(f'{GET_MEOWFFICER_ITEMS_1} -> {CLICK_SAFE_AREA}')
            self.device.click(CLICK_SAFE_AREA)
            return True
        if self.appear(GET_MEOWFFICER_ITEMS_2, interval=interval):
            if drop:
                drop.handle_add(main=self, before=2)
            logger.info(f'{GET_MEOWFFICER_ITEMS_2} -> {CLICK_SAFE_AREA}')
            self.device.click(CLICK_SAFE_AREA)
            return True

        return False

    def handle_map_archives(self, drop=None):
        if self.appear(MAP_ARCHIVES, interval=5):
            if drop:
                drop.add(self.device.image)
            logger.info(f'{MAP_ARCHIVES} -> {CLICK_SAFE_AREA}')
            self.device.click(CLICK_SAFE_AREA)
            return True
        if self.appear_then_click(MAP_WORLD, offset=(20, 20), interval=5):
            return True

        return False

    def handle_os_game_tips(self):
        # Закрываем игровую подсказку при первом включении автопоиска
        if self.appear_then_click(OS_GAME_TIPS, offset=(20, 20), interval=3):
            return True

        return False

    def handle_ash_popup(self):
        name = 'ASH'
        # 2021.12.09
        # В окне META больше нет красного текста; вместо него проверяем текст "Ashes Coordinates"
        if self.appear(POPUP_CONFIRM, offset=self._popup_offset) \
                and self.appear(POPUP_CANCEL, offset=self._popup_offset, interval=2) \
                and self.appear(ASH_POPUP_CHECK, offset=(20, 20)):
            POPUP_CANCEL.name = POPUP_CANCEL.name + '_' + name
            self.device.click(POPUP_CANCEL)
            POPUP_CANCEL.name = POPUP_CANCEL.name[:-len(name) - 1]
            self.ash_popup_canceled = True
            return True
        else:
            return False

    def handle_map_event(self, drop=None):
        """
        处理大世界地图事件。

        Args:
            drop (DropImage): 掉落图像对象。

        Returns:
            str: 已处理的事件名称。
        """
        # Сначала обрабатываем окно маяка META, чтобы handle_popup_confirm не подтвердил переход на экран META по ошибке
        # Окно META также содержит POPUP_CONFIRM и POPUP_CANCEL; если сначала совпадёт DEPART_CONFIRM,
        # подтверждение откроет экран META, который цикл auto search не распознает и зависнет
        if self.handle_ash_popup():
            return 'ash_popup'
        # Обрабатываем окно подтверждения выхода из области при поиске Meowfficer (issue #100)
        # Такие окна блокируют остальные действия, поэтому их нужно обрабатывать в первую очередь
        # Параметр name в handle_popup_confirm используется только для логов; распознавание выполняется общей кнопкой POPUP_CONFIRM
        if self.handle_popup_confirm('DEPART_CONFIRM'):
            return 'depart_confirm'
        if self.handle_map_get_items(drop=drop):
            return 'map_get_items'
        if self.handle_os_game_tips():
            return 'os_game_tips'
        if self.handle_map_archives(drop=drop):
            return 'map_archives'
        if self.handle_guild_popup_cancel():
            return 'guild_popup_cancel'
        if self.handle_urgent_commission(drop=drop):
            return 'urgent_commission'
        if self.handle_story_skip(drop=drop):
            return 'story_skip'

        return ''

    _story_timeout = Timer(60)

    def handle_story_skip(self, drop=None):
        if super().handle_story_skip(drop):
            self._story_timeout.reset()
            return True

        if self.appear(STORY_SKIP_3, offset=(20, 20), interval=0):
            if self._story_timeout.reached():
                logger.warning('[Операция «Сирена» — событие] Истекло время ожидания варианта сюжета')
                self._story_timeout.reset()

                # Перезапускаем приложение
                self.device.app_stop()
                self.device.app_start()

                from module.handler.login import LoginHandler
                LoginHandler(self.config, self.device).handle_app_login()

                from module.ui.page import page_os
                self.ui_ensure(page_os)

                return True

            if not self._story_timeout.started():
                self._story_timeout.start()
        else:
            self._story_timeout.reset()

        return False

    _os_in_map_confirm_timer = Timer(1.5, count=3)

    def handle_os_in_map(self):
        """
        确认是否已返回大世界地图。

        Returns:
            bool: 是否在地图中并已确认。
        """
        if self.is_in_map():
            if self._os_in_map_confirm_timer.reached():
                return True
            else:
                return False
        else:
            self._os_in_map_confirm_timer.reset()
            return False

    def ensure_no_map_event(self):
        self._os_in_map_confirm_timer.reset()

        for _ in self.loop():
            if self.handle_map_event():
                continue
            # End
            if self.handle_os_in_map():
                break

    def os_auto_search_quit(self, drop=None):
        """
        退出大世界自动搜索。

        Args:
            drop (DropImage): 掉落图像对象。

        Returns:
            bool: 当前地图是否已清除。
        """
        confirm_timer = Timer(1.2, count=3).start()
        cleared = False
        for _ in self.loop():
            if self.appear(AUTO_SEARCH_REWARD, offset=(50, 50), interval=2):
                if self.ensure_no_info_bar():
                    cleared = True
                if drop:
                    drop.handle_add(main=self, before=4)
                self.device.click(AUTO_SEARCH_REWARD)
                self.interval_reset([
                    AUTO_SEARCH_REWARD,
                    AUTO_SEARCH_OS_MAP_OPTION_ON,
                    AUTO_SEARCH_OS_MAP_OPTION_OFF,
                    AUTO_SEARCH_OS_MAP_OPTION_OFF_DISABLED,
                ])
                confirm_timer.reset()
                continue
            if self.handle_map_event():
                confirm_timer.reset()
                continue
            if self.appear_then_click(GLOBE_GOTO_MAP, offset=(20, 20), interval=2):
                # Иногда после нажатия AUTO_SEARCH_REWARD неожиданно открывается глобальная карта
                # из-за повторного клика или клика за пределами области карты
                confirm_timer.reset()
                continue
            # По неизвестной причине открыт склад — просто выходим
            # Эквивалентно is_in_storage, но здесь нельзя наследовать StorageHandler
            # Имя STORAGE_CHECK повторяется: здесь это os_handler/STORAGE_CHECK, а не handler/STORAGE_CHECK
            if self.appear(STORAGE_CHECK, offset=(20, 20), interval=5):
                logger.info(f'{STORAGE_CHECK} -> {BACK_ARROW}')
                self.device.click(BACK_ARROW)
                confirm_timer.reset()
                continue

            # Завершение
            if self.is_in_map():
                if confirm_timer.reached():
                    break
            else:
                confirm_timer.reset()

        return cleared

    def handle_os_auto_search_map_option(self, drop=None, enable: Optional[bool] = True):
        """
        处理大世界自动搜索地图选项。

        Args:
            drop (DropImage): 掉落图像对象。
            enable (bool): True/False，或 None 表示不操作。

        Returns:
            bool: 是否点击了选项。
        """
        if self.match_template_color(AUTO_SEARCH_OS_MAP_OPTION_OFF, offset=(5, 120)):
            if self.info_bar_count() >= 2:
                self.device.screenshot_interval_set()
                self.os_auto_search_quit(drop=drop)
                raise CampaignEnd
        if self.match_template_color(AUTO_SEARCH_OS_MAP_OPTION_OFF_DISABLED, offset=(5, 120)):
            if self.info_bar_count() >= 2:
                self.device.screenshot_interval_set()
                self.os_auto_search_quit(drop=drop)
                raise CampaignEnd
        if self.appear(AUTO_SEARCH_REWARD, offset=(50, 50)):
            self.device.screenshot_interval_set()
            if self.os_auto_search_quit(drop=drop):
                # На текущей карте больше нет предметов
                raise CampaignEnd
            else:
                # Автопоиск остановлен, но карта ещё не очищена
                return True

        if enable is None:
            pass
        elif enable:
            if self.match_template_color(AUTO_SEARCH_OS_MAP_OPTION_OFF, offset=(5, 120), interval=3):
                self.device.click(AUTO_SEARCH_OS_MAP_OPTION_OFF)
                self.interval_reset(AUTO_SEARCH_OS_MAP_OPTION_OFF_DISABLED)
                return True
            # Иногда из-за бага клиента AUTO_SEARCH_OS_MAP_OPTION_OFF отображается серым, но всё равно нажимается
            if self.match_template_color(AUTO_SEARCH_OS_MAP_OPTION_OFF_DISABLED, offset=(5, 120), interval=3):
                self.device.click(AUTO_SEARCH_OS_MAP_OPTION_OFF_DISABLED)
                self.interval_reset(AUTO_SEARCH_OS_MAP_OPTION_OFF)
                return True
        else:
            if self.match_template_color(AUTO_SEARCH_OS_MAP_OPTION_ON, offset=(5, 120), interval=3):
                self.device.click(AUTO_SEARCH_OS_MAP_OPTION_ON)
                return True

        return False

    def handle_os_map_fleet_lock(self, enable=None):
        """
        处理大世界地图舰队锁定。

        Args:
            enable (bool): 默认为 None，使用 Campaign_UseFleetLock 配置。

        Returns:
            bool: 是否切换了锁定状态。
        """
        # Фиксация флота зависит от отображения на карте, а не от состояния карты
        # поскольку при уже открытой карте отдельного состояния карты нет
        if not fleet_lock.appear(main=self):
            logger.info('[Операция «Сирена» — событие] Вариант фиксации флота не найден')
            return False

        if enable is None:
            enable = self.config.Campaign_UseFleetLock
        state = 'on' if enable else 'off'
        changed = fleet_lock.set(state, main=self)

        return changed
