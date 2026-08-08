"""岛屿每日采集模块。

管理岛屿资源采集的自动化流程，包括采集奖励领取、目标选择与角色派遣。
根据角色体力阈值和工作状态进行智能选择，按生活等级排序优先派遣空闲角色。
"""
from module.island.island import *
from datetime import timedelta

from module.config.time_source import now as current_time
from module.logger import logger

# 采集安全区域（无截图，固定坐标点击用于关闭弹窗）
ISLAND_GATHER_SAFE_AREA = Button(
    area={'cn': (1080, 650, 1180, 700), 'en': (1080, 650, 1180, 700), 'jp': (1080, 650, 1180, 700), 'tw': (1080, 650, 1180, 700)},
    color={'cn': (0, 0, 0), 'en': (0, 0, 0), 'jp': (0, 0, 0), 'tw': (0, 0, 0)},
    button={'cn': (1080, 650, 1180, 700), 'en': (1080, 650, 1180, 700), 'jp': (1080, 650, 1180, 700), 'tw': (1080, 650, 1180, 700)},
    file={'cn': './assets/cn/island/ISLAND_GATHER_SAFE_AREA.png', 'en': './assets/cn/island/ISLAND_GATHER_SAFE_AREA.png', 'jp': './assets/cn/island/ISLAND_GATHER_SAFE_AREA.png', 'tw': './assets/cn/island/ISLAND_GATHER_SAFE_AREA.png'}
)

GATHER_STAMINA_THRESHOLD = 100


class IslandDailyGather(Island):
    """
    每日采集逻辑（UI模拟点击版）
    
    执行时间：每日凌晨3:10 和 下午6:00
    流程：
      1. 进入管理界面 → 切换到"采集"页签
      2. 领取已有采集奖励（如果有）
      3. 选择采集目标（_handle_target_selection内依次完成：点击选择目标按钮 → 切换两个toggle开关 → 点击确定）
      4. 依次点击三个"+"按钮，每个点击后：
         a. 等待角色选择界面出现
         b. 对角色列表按"生活等级"升序排序
         c. 优先选择体力达标且非工作中的角色
      5. 点击"出发"按钮完成采集
    """

    def run(self):
        """
        任务入口：执行采集流程，并根据当前时间调度下次运行
        每日自动运行两次：凌晨3:10 和 下午6:00
        """
        now = current_time()

        # 无论何时运行，都执行采集
        logger.info(f"[Остров — ежедневный сбор] Начало ежедневного сбора (текущее время: {now.strftime('%H:%M')})")
        self.dispatch_collection()

        # 调度下次运行时间
        next_run = self._schedule_next_run(now)
        self.config.task_delay(target=next_run)
        logger.info(f"[Остров — ежедневный сбор] Следующий запуск: {next_run}")

    def _schedule_next_run(self, now):
        """
        计算下次运行时间
        规则：凌晨执行后调度到下午6:00，下午执行后调度到次日凌晨3:10

        Args:
            now: 当前时间

        Returns:
            datetime: 下次运行时间
        """
        morning = now.replace(hour=3, minute=10, second=0, microsecond=0)
        evening = now.replace(hour=18, minute=0, second=0, microsecond=0)

        if now < morning:
            # 凌晨3:10之前 → 下次下午6:00
            target = evening
        elif now < evening:
            # 凌晨3:10 ~ 下午6:00之间 → 下次下午6:00
            target = evening
        else:
            # 下午6:00之后 → 下次次日凌晨3:10
            target = morning + timedelta(days=1)

        return target

    def dispatch_collection(self):
        """
        完整的UI模拟点击采集流程
        """
        logger.info("[Остров — ежедневный сбор] === Начало процесса сбора через UI ===")

        # 1. 进入管理界面
        logger.info("[Остров — ежедневный сбор] Шаг 1: вход в управление")
        self.goto_management()

        # 2. 进入管理 → 切换到"采集"页签
        logger.info("[Остров — ежедневный сбор] Шаг 2: переключение на вкладку сбора")
        self.ui_goto(page_island_postmanage, get_ship=False)
        self.post_manage_mode_collection()

        # 3. 检查并领取已有采集奖励（如果有的话）
        logger.info("[Остров — ежедневный сбор] Шаг 3: проверка и получение доступных наград")
        self._claim_existing_rewards()

        # 4. 点击"选择采集目标"按钮 → 切换开关 → 点击确定
        #    如果所有采集物已采集完毕，确定时会弹出提示弹窗
        logger.info("[Остров — ежедневный сбор] Шаг 4: подтверждение выбора целей сбора")
        if self._handle_target_selection():
            worker_list = self._daily_gather_worker_list()
            # 如果成功选择了采集目标，继续后续流程
            # 5. 依次点击三个"+"按钮并选择角色
            character_selected = True
            for i in range(3):
                logger.info(f"[Остров — ежедневный сбор] Шаг 5-{i+1}: нажатие кнопки + в слоте {i+1} и выбор персонажа")
                if not self._click_plus_and_select_character(i, worker_list):
                    logger.warning(f"[Остров — ежедневный сбор] Не удалось выбрать персонажа для слота {i + 1}; текущее назначение сбора прекращено")
                    character_selected = False
                    break

            if character_selected:
                # 6. 点击"出发"按钮
                logger.info("[Остров — ежедневный сбор] Шаг 6: нажатие кнопки отправления")
                self._click_depart()

                # 7. 处理采集完成页面
                logger.info("[Остров — ежедневный сбор] Шаг 7: ожидание завершения сбора и закрытие страницы")
                self._handle_collection_complete()
        else:
            logger.info("[Остров — ежедневный сбор] Все ресурсы уже собраны; последующие шаги пропущены")

        # 8. 退出管理界面
        self._exit_management()
        logger.info("[Остров — ежедневный сбор] === Процесс сбора через UI завершён ===")

    # ==================== 步骤方法 ====================

    def _claim_existing_rewards(self):
        """
        检查并领取已有采集奖励（到达时间后领取完成）
        """
        max_attempts = 5
        for _ in range(max_attempts):
            self.device.screenshot()
            if self.appear(ISLAND_GET, offset=30):
                logger.info("[Остров — ежедневный сбор] Обнаружена доступная награда; получение")
                self.device.click(ISLAND_POST_SAFE_AREA)
                self.device.sleep(0.5)
                continue
            if self.appear_then_click(POST_GET, offset=(50, 0)):
                self.device.sleep(0.5)
                self.device.click(ISLAND_POST_SAFE_AREA)
                self.device.sleep(0.5)
                continue
            # 不再有可领取内容
            break

    def _click_select_target(self):
        """
        点击"选择采集目标"按钮并等待弹窗出现
        """
        while True:
            self.device.screenshot()
            # 检查弹窗是否已经出现
            if self.appear(ISLAND_GATHER_TARGET_POPUP, offset=30):
                logger.info("[Остров — ежедневный сбор] Окно выбора целей сбора появилось")
                break
            # 点击选择采集目标按钮
            if self.appear_then_click(ISLAND_GATHER_SELECT_TARGET, offset=30):
                self.device.sleep(0.5)
                continue
            self.device.sleep(0.3)

    def _toggle_switches(self):
        """
        将弹窗中的两个toggle开关切换至激活状态
        """
        for toggle_name, toggle_on, toggle_off in [
            ("Переключатель A", ISLAND_GATHER_TOGGLE_A_ON, ISLAND_GATHER_TOGGLE_A_OFF),
            ("Переключатель B", ISLAND_GATHER_TOGGLE_B_ON, ISLAND_GATHER_TOGGLE_B_OFF),
        ]:
            self._ensure_toggle_active(toggle_name, toggle_on, toggle_off)

    def _ensure_toggle_active(self, toggle_name, toggle_on, toggle_off):
        """
        确保指定的toggle开关处于激活状态

        Args:
            toggle_name: 开关名称（日志用）
            toggle_on: 开关已激活状态的Button
            toggle_off: 开关未激活状态的Button
        """
        max_attempts = 5
        for attempt in range(max_attempts):
            self.device.screenshot()

            # 检查是否已激活
            if self.appear(toggle_on, offset=10):
                logger.info(f"[Остров — ежедневный сбор] {toggle_name} уже активен")
                return True

            # 检查是否未激活，点击切换
            if self.appear(toggle_off, offset=10):
                logger.info(f"[Остров — ежедневный сбор] {toggle_name} не активен; переключение")
                self.device.click(toggle_off)
                self.device.sleep(0.3)
                continue

            # 没有检测到任何状态，尝试点击开关默认位置
            self.device.click(toggle_off)
            self.device.sleep(0.3)

        logger.warning(f"[Остров — ежедневный сбор] Истекло время переключения: {toggle_name}")
        return False

    def _confirm_selection(self):
        """
        点击弹窗中的"确定"按钮关闭弹窗
        最多尝试3次，如果出现已全部采集弹窗则停止
        """
        max_attempts = 3
        for attempt in range(max_attempts):
            self.device.screenshot()

            # 检查是否已全部采集弹窗出现
            if self.appear(ISLAND_GATHER_ALREADY_COLLECTED, offset=30):
                logger.info("[Остров — ежедневный сбор] Обнаружено окно «всё уже собрано»")
                return False

            if self.appear_then_click(ISLAND_GATHER_CONFIRM, offset=30):
                self.device.sleep(0.5)
                continue

            # 弹窗关闭后检测采集页签是否恢复
            if not self.appear(ISLAND_GATHER_TARGET_POPUP, offset=30):
                logger.info("[Остров — ежедневный сбор] Окно закрыто; подтверждение успешно")
                return True

            self.device.sleep(0.3)

        # 尝试3次后还未关闭，可能是已全部采集弹窗遮盖了目标弹窗
        self.device.screenshot()
        if self.appear(ISLAND_GATHER_ALREADY_COLLECTED, offset=30):
            logger.info("[Остров — ежедневный сбор] Обнаружено окно «всё уже собрано»")
            return False
        # 也可能是目标弹窗仍然开着但确定按钮失效
        if self.appear(ISLAND_GATHER_TARGET_POPUP, offset=30):
            logger.warning("[Остров — ежедневный сбор] Истекло время нажатия подтверждения; окно выбора целей всё ещё открыто")
            # 尝试通过安全区域关闭
            self.device.click(ISLAND_GATHER_SAFE_AREA)
            self.device.sleep(0.3)
        return False

    def _daily_gather_worker_list(self):
        """
        解析每日采集自定义角色配置。

        每日采集界面没有 WorkerJuu，配置中出现时必须忽略。
        """
        config = self.config.IslandDailyGather_WorkerFilter
        characters = self.parse_character_filter(config)
        if not characters:
            return []

        filtered = []
        ignored_worker = False
        for character in characters:
            if character == "WorkerJuu":
                ignored_worker = True
                continue
            filtered.append(character)

        if ignored_worker:
            logger.warning("[Остров — ежедневный сбор] WorkerJuu недоступен для ежедневного сбора и автоматически проигнорирован")

        if len(filtered) > 3:
            logger.warning(f"[Остров — ежедневный сбор] Можно указать не более 3 действительных персонажей; последующие проигнорированы: {filtered[3:]}")
            filtered = filtered[:3]

        logger.info(f"[Остров — ежедневный сбор] Пользовательские персонажи: {filtered}")
        return filtered

    def _click_plus_and_select_character(self, index, worker_list=None):
        """
        点击第index个"+"按钮并选择角色

        Args:
            index: 槽位索引 (0, 1, 2)
            worker_list: 每日采集自定义角色列表。
        """
        plus_buttons = [ISLAND_GATHER_PLUS_A, ISLAND_GATHER_PLUS_B, ISLAND_GATHER_PLUS_C]
        plus_button = plus_buttons[index]
        worker_list = worker_list or []

        # 点击"+"按钮，若页面动画或点击未生效则重试，避免无限等待。
        for attempt in range(3):
            logger.info(f"[Остров — ежедневный сбор] Нажатие кнопки + для слота {index + 1}")
            self.device.click(plus_button)
            if self._wait_for_character_select(timeout=6):
                break
            logger.warning(f"[Остров — ежедневный сбор] Экран выбора персонажа для слота {index + 1} не появился; повтор {attempt + 1}/3")
        else:
            logger.warning(f"[Остров — ежедневный сбор] Не удалось открыть экран выбора персонажа для слота {index + 1}")
            return False

        # 按"生活等级"进行升序排序
        self._sort_by_life_level()

        # 选择体力达标且非工作中的角色
        selected = False
        if index < len(worker_list):
            character = worker_list[index]
            logger.info(f"[Остров — ежедневный сбор] Слот {index + 1}: попытка выбрать заданного персонажа: {character}")
            selected = self.select_specific_character(character, min_stamina=GATHER_STAMINA_THRESHOLD)
            if not selected:
                logger.warning(f"[Остров — ежедневный сбор] Слот {index + 1}: заданный персонаж недоступен; возврат к прежней логике: {character}")

        if not selected and not self._select_character_with_stamina_check():
            logger.warning(f"[Остров — ежедневный сбор] Для слота {index + 1} не найден доступный персонаж; пропуск")
            self.device.click(SELECT_UI_BACK)
            self.device.sleep(0.3)
            return False

        # 点击确认按钮完成选择
        self.device.sleep(0.3)
        if not self.confirm_selected_character_closed(f"Ежедневный сбор, слот {index + 1}"):
            self.device.click(SELECT_UI_BACK)
            self.device.sleep(0.3)
            return False
        logger.info(f"[Остров — ежедневный сбор] Выбор персонажа для слота {index + 1} завершён")
        return True

    def _wait_for_character_select(self, timeout=8):
        """
        等待角色选择界面出现
        """
        for _ in self.loop(timeout=timeout, skip_first=False):
            if self.appear(ISLAND_SELECT_CHARACTER_CHECK, offset=1):
                logger.info("[Остров — ежедневный сбор] Экран выбора персонажа появился")
                return True

        return False

    def _sort_by_life_level(self):
        """
        通过模板匹配检测升序箭头，确保按生活等级升序排序

        按钮颜色不变仅箭头不同，截取按钮区域图像后匹配升序箭头模板来判断
        """
        # 截取排序按钮区域，检测当前是否为升序
        self.device.screenshot()
        sort_area = crop(self.device.image, ISLAND_GATHER_SORT_LIFE.area)
        if TEMPLATE_GATHER_SORT_LIFE_ASC.match(sort_area, similarity=0.8):
            logger.info("[Остров — ежедневный сбор] Уже используется сортировка по уровню жизни по возрастанию")
            return

        # 不是升序，点击排序按钮切换到升序
        logger.info("[Остров — ежедневный сбор] Нажатие сортировки по уровню жизни для переключения на возрастание")
        self.device.click(ISLAND_GATHER_SORT_LIFE)
        self.device.sleep(0.3)

        # 验证是否成功切换为升序
        self.device.screenshot()
        sort_area = crop(self.device.image, ISLAND_GATHER_SORT_LIFE.area)
        if TEMPLATE_GATHER_SORT_LIFE_ASC.match(sort_area, similarity=0.8):
            logger.info("[Остров — ежедневный сбор] Сортировка по уровню жизни по возрастанию включена")
        else:
            logger.warning("[Остров — ежедневный сбор] Переключение на возрастание могло не сработать; повторное нажатие")
            self.device.click(ISLAND_GATHER_SORT_LIFE)
            self.device.sleep(0.3)

        logger.info("[Остров — ежедневный сбор] Сортировка по уровню жизни по возрастанию завершена")

    def _select_character_with_stamina_check(self):
        """
        从当前角色列表中选择体力达标且非工作中的角色。

        若没有达到阈值的角色，则回退选择当前页体力最高的空闲角色，避免流程卡住。
        注：采集界面的角色选择没有黄鸡角色

        Returns:
            bool: 是否成功选择角色
        """
        screenshot = self.device.screenshot()
        detected = False
        available = []

        for row, col, button in self.select_character_grid.generate():
            character_status = self._recognize_character_status(screenshot, button)
            if not character_status:
                continue

            detected = True
            char_info = {
                "grid_position": (row, col),
                "button_area": button.area,
                **character_status,
            }
            if char_info["is_working"] or char_info["is_selected"]:
                continue

            if char_info.get("stamina", 0) >= GATHER_STAMINA_THRESHOLD:
                return self._click_character(char_info, f"выносливость >= {GATHER_STAMINA_THRESHOLD}")

            available.append(char_info)

        if not detected:
            logger.warning("[Остров — ежедневный сбор] Персонажи не обнаружены")
            return False

        if not available:
            logger.warning("[Остров — ежедневный сбор] Все персонажи работают или уже выбраны; свободного персонажа выбрать нельзя")
            return False

        best_char = max(available, key=lambda item: item.get("stamina", 0))
        logger.warning(
            f"Персонаж с достаточной выносливостью не найден; выбирается персонаж с максимальной выносливостью на текущей странице: "
            f"{best_char['character_name']} ({best_char.get('stamina', 0)})"
        )
        return self._click_character(best_char, "максимальная выносливость")

    def _click_character(self, char_info, reason):
        """
        点击角色选择网格。

        Args:
            char_info: recognize_all_characters() 返回的角色信息。
            reason: 日志中的选择原因。

        Returns:
            bool: 是否成功点击。
        """
        row, col = char_info["grid_position"]
        stamina = char_info.get("stamina", 0)
        logger.info(
            f"Выбор персонажа: {char_info['character_name']} "
            f"(позиция: {row},{col}, выносливость: {stamina}, причина: {reason})"
        )
        button = self.select_character_grid[row, col]
        self.device.click(button)
        self.device.sleep(0.3)
        return True

    def _select_first_idle_character(self):
        """
        兼容旧调用：选择第一个空闲角色。
        """
        screenshot = self.device.screenshot()
        characters = self.recognize_all_characters(screenshot)

        for char_info in characters:
            if not char_info["is_working"] and not char_info["is_selected"]:
                return self._click_character(char_info, "свободен и не выбран")

        logger.warning("[Остров — ежедневный сбор] Все персонажи работают или уже выбраны; свободного персонажа выбрать нельзя")
        return False

    def _click_depart(self):
        """
        点击"出发"按钮开始采集
        """
        max_attempts = 10
        for attempt in range(max_attempts):
            self.device.screenshot()
            if self.appear_then_click(ISLAND_GATHER_DEPART, offset=30):
                self.device.sleep(0.5)
                logger.info("[Остров — ежедневный сбор] Кнопка отправления нажата успешно")
                return True
            self.device.sleep(0.3)

        logger.warning("[Остров — ежедневный сбор] Истекло время нажатия кнопки отправления")
        return False

    def _handle_target_selection(self):
        """
        处理选择采集目标的确认流程。
        如果所有采集物已采集完毕，确定时会弹出提示弹窗，需要关闭后退出。

        Returns:
            bool: True=成功选择目标可继续, False=已全部采集需退出
        """
        # 点击"选择采集目标"按钮
        self._click_select_target()

        # 在弹窗中将两个toggle开关切换至激活状态
        self._toggle_switches()

        # 点击"确定"按钮（内部已检测已全部采集弹窗）
        confirm_result = self._confirm_selection()

        if not confirm_result:
            # 检测到已全部采集弹窗，点击安全区域关闭
            logger.info("[Остров — ежедневный сбор] Нажатие безопасной области для закрытия уведомления")
            self.device.click(ISLAND_GATHER_SAFE_AREA)
            self.device.sleep(0.5)
            return False

        logger.info("[Остров — ежедневный сбор] Цели сбора выбраны успешно")
        return True

    def _handle_collection_complete(self):
        """
        处理采集完成页面：等待采集完成，点击安全区域关闭完成界面
        """
        max_wait = 30  # 最多等待30秒
        for i in range(max_wait):
            self.device.screenshot()
            if self.appear(ISLAND_GATHER_COMPLETE, offset=30):
                logger.info("[Остров — ежедневный сбор] Страница завершения сбора появилась; закрытие через безопасную область")
                self.device.click(ISLAND_GATHER_SAFE_AREA)
                self.device.sleep(0.5)
                return True
            self.device.sleep(1)

        logger.warning("[Остров — ежедневный сбор] Истекло время ожидания страницы завершения сбора")
        return False

    def _exit_management(self):
        """
        退出管理界面，返回到小岛主界面
        """
        logger.info("[Остров — ежедневный сбор] Выход из управления")
        # 先点击安全区域关闭可能残留的弹窗
        for _ in range(3):
            self.device.screenshot()
            if self.appear(ISLAND_GATHER_ALREADY_COLLECTED, offset=30) or \
               self.appear(ISLAND_GATHER_COMPLETE, offset=30) or \
               self.appear(ISLAND_GATHER_TARGET_POPUP, offset=30):
                self.device.click(ISLAND_GATHER_SAFE_AREA)
                self.device.sleep(0.3)
            else:
                break
        # 使用ui_goto导航回小岛页面
        self.ui_goto(page_island, get_ship=False)
        logger.info("[Остров — ежедневный сбор] Возврат на главную страницу острова завершён")
        return True


if __name__ == "__main__":
    az = IslandDailyGather('alas', task='Alas')
    az.device.screenshot()
    az.run()
