"""岛屿矿山与森林生产模块。

负责矿山和森林区域的自动生产管理，包括采集、派遣角色、工作时间追踪与任务调度。

主要功能：
- 检查矿山/森林岗位状态（空闲/工作中）
- 自动派遣角色进行采集
- 支持岗位数量配置（矿山 0-3 个，森林 0-3 个）
- 跟踪各岗位完成时间并设置下次任务执行时间
- 支持按优先级配置采集角色
"""
from datetime import timedelta

from module.base.button import ButtonGrid
from module.config.time_source import now as current_time
from module.island.assets import *
from module.island.island import Island
from module.island_mine_forest.assets import *
from module.logger import logger
from module.ocr.ocr import Duration


class IslandMineForest(Island):
    """岛屿矿山与森林生产管理器。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.mine_positions = self.config.IslandMine_Positions
        self.forest_positions = self.config.IslandForest_Positions
        self.mine_worker_filter = self.config.IslandMine_WorkerFilter
        self.forest_worker_filter = self.config.IslandForest_WorkerFilter

        self.mine_time_vars = [None] * self.mine_positions
        self.forest_time_vars = [None] * self.forest_positions

        self.mine_posts = {
            'ISLAND_MINE_POST1': ISLAND_MINE_POST1,
            'ISLAND_MINE_POST2': ISLAND_MINE_POST2,
            'ISLAND_MINE_POST3': ISLAND_MINE_POST3,
        }
        self.forest_posts = {
            'ISLAND_FOREST_POST1': ISLAND_FOREST_POST1,
            'ISLAND_FOREST_POST2': ISLAND_FOREST_POST2,
            'ISLAND_FOREST_POST3': ISLAND_FOREST_POST3,
        }

        self.active_mine_posts = [
            self.mine_posts[f'ISLAND_MINE_POST{i + 1}']
            for i in range(min(self.mine_positions, len(self.mine_posts)))
        ]
        self.active_forest_posts = [
            self.forest_posts[f'ISLAND_FOREST_POST{i + 1}']
            for i in range(min(self.forest_positions, len(self.forest_posts)))
        ]

        self.mine_states = ['unknown'] * len(self.active_mine_posts)
        self.forest_states = ['unknown'] * len(self.active_forest_posts)

    def _prepare_post_management(self):
        """进入生产岗位管理并回到页面顶部。"""
        self.goto_postmanage()
        self.post_manage_mode(POST_MANAGE_PRODUCTION)
        self.post_close()
        self.post_manage_swipe(0)
        self.device.sleep(0.5)

    def _mine_buttons_visible(self):
        """通过模板判断矿山岗位区域是否已出现在当前视野。"""
        if not self.active_mine_posts:
            return True
        visible = 0
        for button in self.active_mine_posts:
            if self.appear(button, offset=(20, 20)):
                visible += 1
        return visible >= max(1, min(2, len(self.active_mine_posts)))

    def _forest_buttons_visible(self):
        """通过模板判断森林岗位区域是否已出现在当前视野。"""
        if not self.active_forest_posts:
            return True
        visible = 0
        for button in self.active_forest_posts:
            if self.appear(button, offset=(20, 20)):
                visible += 1
        return visible >= max(1, min(2, len(self.active_forest_posts)))

    def _locate_mine_section(self):
        """从页面顶部通过模板定位矿山岗位区域。"""
        self.post_close()
        self.post_manage_swipe(0)
        self.device.sleep(0.5)
        for _ in range(6):
            self.device.screenshot()
            if self._mine_buttons_visible():
                return True
            self.post_manage_up_swipe(450)
            self.device.sleep(0.5)
        return False

    def _locate_forest_section(self):
        """从页面顶部通过模板定位森林岗位区域。"""
        self.post_close()
        self.post_manage_swipe(0)
        self.device.sleep(0.5)
        for _ in range(8):
            self.device.screenshot()
            if self._forest_buttons_visible():
                return True
            self.post_manage_up_swipe(450)
            self.device.sleep(0.5)
        return False

    def _read_post_state(self, post_button):
        """
        打开岗位并读取当前状态。

        Returns:
            tuple[str, datetime | None]: state, finish_time
        """
        self.post_close()
        self.post_open(post_button)
        self.device.sleep(0.3)
        self.device.screenshot()

        state = 'unknown'
        finish_time = None
        if self.appear(ISLAND_WORK_COMPLETE, offset=1):
            self.post_get_stay()
            self.device.screenshot()

        if self.appear(ISLAND_WORKING):
            state = 'working'
            time_work = Duration(ISLAND_WORKING_TIME)
            time_value = time_work.ocr(self.device.image)
            if time_value.total_seconds() > 0:
                finish_time = current_time() + time_value
        elif self.appear(ISLAND_POST_SELECT, offset=1):
            state = 'idle'

        self.post_close()
        return state, finish_time

    def _scan_posts(self, post_buttons, time_vars, states, label):
        """扫描岗位状态并记录工作完成时间。"""
        for i, post_button in enumerate(post_buttons):
            state, finish_time = self._read_post_state(post_button)
            states[i] = state
            time_vars[i] = finish_time
            logger.info(f'[Остров — {label}] Позиция {i + 1}: состояние={state}, завершение={finish_time}')

    def _dispatch_worker(self, post_button, character_filter, context):
        """打开空闲岗位并派遣角色。"""
        self.post_close()
        self.post_open(post_button)
        self.device.sleep(0.3)
        for _ in self.loop(timeout=120, skip_first=False):
            if self.appear_then_click(ISLAND_POST_SELECT, offset=1):
                self.device.sleep(0.5)
                continue
            if self.appear(ISLAND_SELECT_CHARACTER_CHECK, offset=1):
                if not character_filter:
                    logger.warning(f'[Остров — добыча] Список персонажей для {context} пуст')
                    self.back_to_postmanage_from_dispatch()
                    return False
                if self.select_character(character_list=character_filter):
                    if not self.confirm_selected_character(context):
                        self.back_to_postmanage_from_dispatch()
                        return False
                    break
                logger.warning(f'[Остров — добыча] Для {context} нет доступных персонажей: {character_filter}')
                self.back_to_postmanage_from_dispatch()
                return False
        else:
            logger.warning(f'[Остров — добыча] Тайм-аут отправки: {context}')
            self.back_to_postmanage_from_dispatch()
            return False

        self.post_open(post_button)
        self.device.sleep(0.5)
        self.device.screenshot()
        time_work = Duration(ISLAND_WORKING_TIME)
        time_value = time_work.ocr(self.device.image)
        finish_time = current_time() + time_value if time_value.total_seconds() > 0 else None
        self.post_close()
        return finish_time

    def _dispatch_idle_posts(self, post_buttons, time_vars, states, character_filter, label):
        """对所有空闲岗位执行派遣。"""
        for i, post_button in enumerate(post_buttons):
            if states[i] != 'idle':
                continue
            context = f'{label}, позиция {i + 1}'
            logger.info(f'[Остров — {label}] Попытка отправки на позицию {i + 1}')
            finish_time = self._dispatch_worker(post_button, character_filter, context)
            if finish_time is not False:
                states[i] = 'working'
                time_vars[i] = finish_time
                logger.info(f'[Остров — {label}] Позиция {i + 1} отправлена, завершение={finish_time}')

    def _collect_finish_times(self):
        finish_times = [
            value for value in self.mine_time_vars + self.forest_time_vars
            if value is not None
        ]
        finish_times.append(current_time() + timedelta(hours=6))
        finish_times.sort()
        return finish_times

    def run(self):
        """执行矿山和森林岗位管理。"""
        self.island_error = False
        self.ui_ensure(page_island)
        self._prepare_post_management()

        if self.active_mine_posts:
            if self._locate_mine_section():
                logger.info('[Остров — рудник] Проверка состояний позиций рудника')
                self._scan_posts(
                    self.active_mine_posts,
                    self.mine_time_vars,
                    self.mine_states,
                    'рудник',
                )
                self._dispatch_idle_posts(
                    self.active_mine_posts,
                    self.mine_time_vars,
                    self.mine_states,
                    self.mine_worker_filter,
                    'рудник',
                )
            else:
                logger.warning('[Остров — рудник] Не удалось найти область позиций рудника')

        if self.active_forest_posts:
            if self._locate_forest_section():
                logger.info('[Остров — лес] Проверка состояний позиций леса')
                self._scan_posts(
                    self.active_forest_posts,
                    self.forest_time_vars,
                    self.forest_states,
                    'лес',
                )
                self._dispatch_idle_posts(
                    self.active_forest_posts,
                    self.forest_time_vars,
                    self.forest_states,
                    self.forest_worker_filter,
                    'лес',
                )
            else:
                logger.warning('[Остров — лес] Не удалось найти область позиций леса')

        finish_times = self._collect_finish_times()
        self.config.task_delay(target=finish_times)
        logger.info(f'[Остров — добыча] Управление рудником и лесом завершено; следующий запуск: {finish_times[0]}')

        if self.island_error:
            from module.exception import GameBugError
            raise GameBugError('Обнаружен Island ERROR1; требуется перезапуск')
