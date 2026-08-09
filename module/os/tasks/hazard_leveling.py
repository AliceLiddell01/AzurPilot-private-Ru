"""大世界侵蚀 1 等级提升模块。

在危险等级 1 的海域中反复战斗以提升舰船等级，包括：
- 独立运行和智能调度两种模式
- 作战补给凭证（代币）资源保护检查
- 舰船经验检测和等级追踪
- 海域里程 OCR 记录

继承自 CoinTaskMixin 和 OSMap，提供代币保护和地图导航能力，
是大世界中最常用的舰船经验 farming 方式。
"""

from datetime import timedelta

from module.base.timer import Timer
from module.config.time_source import now as current_time
from module.equipment.assets import EQUIPMENT_OPEN
from module.exception import MapDetectionError, ScriptError
from module.logger import logger
from module.os.assets import FLEET_FLAGSHIP
from module.os.map import OSMap
from module.os.ship_exp import ship_info_get_level_exp
from module.os.ship_exp_data import LIST_SHIP_EXP
from module.os.tasks.scheduling import CoinTaskMixin
from module.statistics.opsi_runtime import record_cl1_akashi_encounter
from module.os.sea_miles_ocr import OCR_SEA_MILES_DIGIT
from module.os_handler.assets import MISSION_ENTER, MISSION_CHECK, MISSION_QUIT


class OpsiHazard1Leveling(CoinTaskMixin, OSMap):
    def _cl1_resource_check(self, yellow_coins):
        """侵蚀 1 独立运行时的资源保护检查。"""
        if self.is_running_smart_scheduling_task():
            return

        cl1_preserve = self.config.OpsiHazard1Leveling_OperationCoinsPreserve
        if yellow_coins < cl1_preserve:
            logger.info(
                f"Недостаточно ваучеров боевого снабжения ({yellow_coins} < {cl1_preserve}), прокачка в зоне коррозии 1 отложена до следующего дня"
            )
            self.config.task_delay(server_update=True)
            self.config.task_stop()

    def _cl1_ap_check(self):
        """最低行动力保留检查"""
        min_reserve = self.config.OS_ACTION_POINT_PRESERVE
        if self._action_point_total < min_reserve:
            logger.warning(
                f"Очки действия ниже минимального резерва ({self._action_point_total} < {min_reserve})"
            )

            _previous_ap_insufficient = getattr(
                self.config, "OpsiHazard1_PreviousApInsufficient", False
            )
            if not _previous_ap_insufficient:
                _previous_ap_insufficient = True
                self.notify_push(
                    title="[AzurPilot info] 侵蚀 1 - 行动力低于最低保留",
                    content=f"Всего очков действия {self._action_point_total} ниже минимального резерва {min_reserve}, задача отложена",
                )
            else:
                logger.info("[Операция «Сирена» — прокачка в зоне коррозии 1] При последней проверке очки действия были ниже минимального резерва, уведомление пропущено")

            logger.info("[Операция «Сирена» — прокачка в зоне коррозии 1] Задача отложена на 50 минут")
            self.config.task_delay(minute=50)
            self.config.OpsiHazard1_PreviousApInsufficient = _previous_ap_insufficient
            self.config.task_stop()
        else:
            _previous_ap_insufficient = False
        self.config.OpsiHazard1_PreviousApInsufficient = _previous_ap_insufficient

    def _cl1_run_battle(self):
        """执行侵蚀 1 战后的战略搜索与扫荡逻辑"""
        search_completed = self.run_strategic_search()

        if not search_completed and search_completed is not None:
            logger.warning("[Операция «Сирена» — прокачка в зоне коррозии 1] Стратегический поиск вернул False: возможно, выполнение было прервано досрочно")

        # 第一次重扫：检查是否还有事件
        self._solved_map_event = set()
        self._solved_fleet_mechanism = False
        self.map_rescan()

        # 强制移动逻辑
        if self.config.OpsiHazard1Leveling_ExecuteFixedPatrolScan:
            if not self._solved_map_event:
                self._execute_fixed_patrol_scan(ExecuteFixedPatrolScan=True)
                # 第二次重扫：舰队移动后再次重扫
                self._solved_map_event = set()
                self.map_rescan()

        self.handle_after_auto_search()

        # 明石遭遇记录
        solved_events = getattr(self, "_solved_map_event", set())
        if "is_akashi" in solved_events:
            # 明石遭遇计数归入运行时指标，任务仅报告明石事件已解决
            record_cl1_akashi_encounter(self.config)

    def os_hazard1_leveling(self):
        """侵蚀 1 练级任务入口。"""
        self.run_hazard1_leveling()

    def run_hazard1_leveling(self):
        """执行大世界侵蚀 1 练级任务。"""
        logger.hr("Операция «Сирена» — прокачка в зоне коррозии 1", level=1)

        while True:
            self.run_hazard1_leveling_once()
            self.config.check_task_switch()

    def run_hazard1_leveling_once(self, ap_preserve=None):
        """执行一轮侵蚀 1 练级，由独立任务或 OpsiScheduling 调用。"""
        # 启用随机事件以获得收益。调度器直接调用单轮时也需要保持该行为。
        self.config.override(
            OpsiGeneral_DoRandomMapEvent=True,
        )

        # 读取行动力保留值
        if ap_preserve is None:
            ap_preserve = getattr(
                self.config, "OpsiHazard1Leveling_MinimumActionPointReserve", 200
            )
        self.config.OS_ACTION_POINT_PRESERVE = int(ap_preserve)

        if (
            self.config.is_task_enabled("OpsiAshBeacon")
            and not self._ash_fully_collected
            and self.config.OpsiAshBeacon_EnsureFullyCollected
        ):
            logger.info("[Операция «Сирена» — прокачка в зоне коррозии 1] Координаты маяка META ещё не собраны полностью, ограничение очков действия временно отключено")
            self.config.OS_ACTION_POINT_PRESERVE = 0
        logger.attr(
            "OS_ACTION_POINT_PRESERVE", self.config.OS_ACTION_POINT_PRESERVE
        )

        # 获取当前区域
        try:
            self.get_current_zone()
        except MapDetectionError as e:
            logger.error("[Операция «Сирена» — прокачка в зоне коррозии 1] Не удалось распознать зону: убедитесь, что в игре открыта карта Операции «Сирена»")
            logger.error(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Ошибка распознавания OCR: {e}")
            raise

        # 侵蚀 1 练级时，行动力优先用于此任务，而非耄耋相接。
        self.action_point_set(
            cost=120, keep_current_ap=True, check_rest_ap=True
        )

        yellow_coins = self.get_yellow_coins()
        if not self.is_running_smart_scheduling_task():
            self._cl1_resource_check(yellow_coins)
            self.check_and_notify_action_point_threshold()
            self._cl1_ap_check()

        # ===== 确保在安全海域地图上（战前导航）=====
        if self.config.OpsiHazard1Leveling_TargetZone != 0:
            zone = self.config.OpsiHazard1Leveling_TargetZone
            if self.zone.zone_id != zone or not self.is_zone_name_hidden:
                self.globe_goto(self.name_to_zone(zone), types="SAFE", refresh=True)
        elif self.zone.hazard_level != 1 or not self.is_zone_name_hidden:
            self.globe_goto(self.name_to_zone(22), types="SAFE", refresh=True)
        self.fleet_set(self.config.OpsiFleet_Fleet)

        # ===== 海里数记录（可开关）=====
        sea_miles = None
        if self.config.OpsiHazard1Leveling_RecordSeaMiles:
            try:
                sea_miles = self.detect_and_record_sea_miles()
                if sea_miles is not None:
                    logger.info(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Морские мили определены: {sea_miles}")
                else:
                    logger.warning("[Операция «Сирена» — прокачка в зоне коррозии 1] Не удалось определить морские мили, дальнейшее выполнение продолжится")
            except Exception as e:
                logger.error(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Ошибка при определении морских миль: {e}; дальнейшее выполнение продолжится")

        # ===== 货币与体力记录（始终执行，包含海里数）=====
        self._record_ap_and_coins(sea_miles=sea_miles)

        # ===== 执行侵蚀 1 战略搜索与战后处理 =====
        self._cl1_run_battle()

    def os_check_leveling(self):
        """检查大世界阵容练级进度。"""
        logger.hr("Проверка прокачки в зоне коррозии 1", level=1)
        logger.attr("Последняя проверка прокачки в Операции «Сирена»", self.config.OpsiCheckLeveling_LastRun)
        
        check_interval = self.config.OpsiCheckLeveling_CheckInterval
        if not isinstance(check_interval, int) or check_interval < 1:
            check_interval = 24
            logger.warning("[Операция «Сирена» — прокачка в зоне коррозии 1] Недопустимый интервал проверки, используется значение по умолчанию: 24 часа")
        
        time_run = self.config.OpsiCheckLeveling_LastRun + timedelta(hours=check_interval)
        logger.info(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Время следующей проверки: {time_run}")
        if current_time().replace(microsecond=0) < time_run:
            logger.info("[Операция «Сирена» — прокачка в зоне коррозии 1] Время запуска ещё не наступило, пропуск")
            return
        target_level = self.config.OpsiCheckLeveling_TargetLevel
        if not isinstance(target_level, int) or target_level < 0 or target_level > 125:
            logger.error(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Недопустимый целевой уровень: {target_level}; требуется целое число от 0 до 125")
            raise ScriptError(f"Invalid opsi ship target level: {target_level}")
        if target_level == 0:
            logger.info("[Операция «Сирена» — прокачка в зоне коррозии 1] Целевой уровень равен 0, пропуск")
            return

        logger.attr("[Операция «Сирена» — прокачка в зоне коррозии 1] Флот для проверки", self.config.OpsiFleet_Fleet)
        
        enable_custom_check = self.config.OpsiCheckLeveling_EnableCustomCheck
        custom_positions_value = self.config.OpsiCheckLeveling_CustomCheckPositions
        custom_positions_str = str(custom_positions_value) if custom_positions_value is not None else ''
        custom_positions = []
        if enable_custom_check and custom_positions_str.strip():
            try:
                custom_positions = [int(p.strip()) for p in custom_positions_str.split(',') if p.strip()]
                invalid_positions = [p for p in custom_positions if p < 1 or p > 6]
                if invalid_positions:
                    logger.warning(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Пользовательский список содержит недопустимые позиции: {invalid_positions}; допустимый диапазон — 1–6, будут проверены все корабли")
                    custom_positions = []
                else:
                    logger.info(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Пользовательские позиции для проверки: {custom_positions}")
            except (ValueError, AttributeError):
                logger.warning(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Неверный формат пользовательских позиций: {custom_positions_str}; будут проверены все корабли")
                custom_positions = []
        
        if not self._check_auto_change_prerequisite(enable_custom_check, custom_positions):
            logger.info("[Операция «Сирена» — прокачка в зоне коррозии 1] Условия автоподбора флота не выполнены, функция отключена")
            self.config.OpsiFleetAutoChange_Enable = False
        
        if enable_custom_check and custom_positions:
            ship_data_result = self._collect_custom_positions_data(target_level, custom_positions)
        else:
            ship_data_result = self._collect_ship_data_with_retry(target_level)
        
        if ship_data_result['ships'] is None:
            error_msg = ship_data_result['error'] or "未知错误"
            logger.error(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Не удалось собрать данные кораблей: {error_msg}")
            report = self._format_check_report(
                None, target_level, self.config.OpsiFleet_Fleet, error_msg=error_msg
            )
            self.notify_push(
                title="舰船经验检测失败",
                content=f"<{self.config.config_name}>\n\n{report}",
            )
            self.config.OpsiCheckLeveling_LastRun = current_time().replace(microsecond=0)
            logger.info("[Операция «Сирена» — прокачка в зоне коррозии 1] Проверка не удалась, следующая назначена через 24 часа")
            return
        
        ships = ship_data_result['ships']
        
        try:
            from module.statistics.ship_exp_stats import save_ship_exp_data
            from module.statistics.opsi_month import get_opsi_stats

            instance_name = (
                self.config.config_name if hasattr(self.config, "config_name") else None
            )

            current_battles = (
                get_opsi_stats(instance_name=instance_name)
                .summary()
                .get("total_battles", 0)
            )

            save_ship_exp_data(
                ships=ships,
                target_level=target_level,
                fleet_index=self.config.OpsiFleet_Fleet,
                battle_count_at_check=current_battles,
                instance_name=instance_name,
            )
        except Exception as e:
            logger.warning(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Не удалось сохранить данные об опыте кораблей: {e}")

        report = self._format_check_report(
            ships, target_level, self.config.OpsiFleet_Fleet, custom_positions=custom_positions if enable_custom_check else None
        )
        self.notify_push(
            title="舰船经验检测报告",
            content=f"<{self.config.config_name}>\n\n{report}",
        )

        if enable_custom_check and custom_positions:
            self._check_custom_positions_full_exp(
                ships, target_level, custom_positions
            )
        else:
            all_full_exp = all(
                ship['total_exp'] >= LIST_SHIP_EXP[target_level - 1]
                for ship in ships
            )
            if all_full_exp:
                logger.info(
                    f"Все корабли флота {self.config.OpsiFleet_Fleet} достигли предела опыта (уровень {target_level} или выше)"
                )
                self.notify_push(
                    title="练级检查通过",
                    content=f"<{self.config.config_name}> {self.config.task} достиг ограничения уровня {target_level}.",
                )
                
                if self.config.OpsiFleetAutoChange_Enable:
                    logger.info("[Операция «Сирена» — прокачка в зоне коррозии 1] Автоподбор флота включён, запуск")
                    try:
                        from module.os.tasks.fleet_auto_change import OpsiFleetAutoChange
                        auto_change = OpsiFleetAutoChange(config=self.config, device=self.device)
                        auto_change.run()
                        logger.info("[Операция «Сирена» — прокачка в зоне коррозии 1] Автоподбор флота завершён")
                    except Exception as e:
                        logger.error(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Не удалось выполнить автоподбор флота: {e}")
                
                if self.config.OpsiCheckLeveling_DelayAfterFull:
                    logger.info("[Операция «Сирена» — прокачка в зоне коррозии 1] Все корабли достигли предела опыта, задача будет отложена")
                    self.delay_opsi_active_task(server_update=True, task='OpsiHazard1Leveling')
                    self.config.task_stop()
        
        self.config.OpsiCheckLeveling_LastRun = current_time().replace(microsecond=0)

    def _check_auto_change_prerequisite(self, enable_custom_check, custom_positions):
        """
        检查自动配队前置条件
        
        Args:
            enable_custom_check: 是否启用自定义舰船检测
            custom_positions: 自定义舰位列表
            
        Returns:
            bool: 是否满足前置条件
        """
        if not self.config.OpsiFleetAutoChange_Enable:
            return True
        
        if not enable_custom_check:
            logger.warning("[Операция «Сирена» — прокачка в зоне коррозии 1] Для автоподбора требуется пользовательская проверка кораблей; автоподбор будет отключён")
            return False
        
        if not custom_positions:
            logger.warning("[Операция «Сирена» — прокачка в зоне коррозии 1] Для автоподбора требуется корректный список позиций кораблей; автоподбор будет отключён")
            return False
        
        logger.info(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Условия автоподбора выполнены: пользовательская проверка включена, позиции: {custom_positions}")
        return True

    def _format_check_report(self, ship_data_list, target_level, fleet_index, error_msg=None, custom_positions=None):
        """
        格式化检测报告，用于推送通知
        
        Args:
            ship_data_list: 舰船数据列表，失败时为None
            target_level: 目标等级
            fleet_index: 舰队索引
            error_msg: 错误信息，成功时为None
            custom_positions: 自定义舰位列表，None时显示所有舰船
            
        Returns:
            str: 格式化的报告文本
        """
        lines = []
        lines.append("【Отчёт о проверке опыта кораблей】")
        lines.append("")
        
        if error_msg:
            lines.append("Статус проверки: ошибка")
            lines.append(f"Ошибка: {error_msg}")
            return "\n".join(lines)
        
        lines.append("Статус проверки: успешно")
        lines.append(f"Проверяемый флот: {fleet_index}.")
        lines.append(f"Целевой уровень: Lv.{target_level}")
        if custom_positions:
            lines.append(f"Проверяемые позиции: {', '.join(map(str, custom_positions))}")
        lines.append("")
        
        target_exp = LIST_SHIP_EXP[target_level - 1] if 1 <= target_level <= 125 else 0
        
        try:
            from module.statistics.ship_exp_stats import get_ship_exp_stats
            stats = get_ship_exp_stats(
                instance_name=self.config.config_name if hasattr(self.config, 'config_name') else None
            )
            exp_per_hour = stats.get_exp_per_hour()
        except Exception:
            exp_per_hour = 22000.0
        
        ships_to_report = ship_data_list
        if custom_positions:
            ships_to_report = [s for s in ship_data_list if s.get('position') in custom_positions]
        
        for ship in ships_to_report:
            position = ship.get('position', 0)
            level = ship.get('level', 0)
            current_exp = ship.get('current_exp', 0)
            total_exp = ship.get('total_exp', 0)
            
            if target_exp > 0:
                progress = min(100, total_exp / target_exp * 100)
                progress_str = f"{progress:.1f}%"
            else:
                progress_str = "100%"
            
            if total_exp >= target_exp:
                status = "Опыт заполнен"
                time_str = "0 мин"
            else:
                status = progress_str
                exp_needed = target_exp - total_exp
                if exp_per_hour > 0:
                    hours_needed = exp_needed / exp_per_hour
                    time_seconds = hours_needed * 3600
                    hours = int(time_seconds // 3600)
                    minutes = int((time_seconds % 3600) // 60)
                    if hours > 0:
                        time_str = f"{hours} ч {minutes} мин"
                    else:
                        time_str = f"{minutes} мин"
                else:
                    time_str = "Неизвестно"
            
            lines.append(f"Позиция {position}: Lv.{level} | Опыт: {current_exp:,} | Прогресс: {status} │ Примерное время: {time_str}")
            lines.append("")
        
        all_full = all(ship.get('total_exp', 0) >= target_exp for ship in ships_to_report)
        if all_full:
            if custom_positions:
                lines.append(f"★ Позиции {', '.join(map(str, custom_positions))} набрали максимальный опыт!")
            else:
                lines.append("★ Все корабли набрали максимальный опыт!")
        else:
            not_full = [s for s in ships_to_report if s.get('total_exp', 0) < target_exp]
            lines.append(f"Позиций без максимального опыта: {len(not_full)} шт.")
        
        lines.append(f"Время проверки: {current_time().strftime('%Y-%m-%d %H:%M:%S')}")
        
        return "\n".join(lines)

    def _collect_custom_positions_data(self, target_level, custom_positions):
        """
        收集指定舰位的舰船数据
        
        Args:
            target_level: 目标等级
            custom_positions: 自定义舰位列表，如 [1, 3, 6]
            
        Returns:
            dict: {'ships': list, 'error': str} 
                  ships为舰船数据列表，失败时为None
                  error为错误信息，成功时为None
        """
        from module.os_handler.assets import (
            OS_FLEET_SLOT_NAV_1_BUTTON,
            OS_FLEET_SLOT_NAV_2_BUTTON,
            OS_FLEET_SLOT_NAV_3_BUTTON,
            OS_FLEET_SLOT_NAV_4_BUTTON,
            OS_FLEET_SLOT_NAV_5_BUTTON,
            OS_FLEET_SLOT_NAV_6_BUTTON,
        )
        
        logger.info(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Сбор данных по заданным позициям кораблей: {custom_positions}")
        
        slot_buttons = {
            1: OS_FLEET_SLOT_NAV_1_BUTTON,
            2: OS_FLEET_SLOT_NAV_2_BUTTON,
            3: OS_FLEET_SLOT_NAV_3_BUTTON,
            4: OS_FLEET_SLOT_NAV_4_BUTTON,
            5: OS_FLEET_SLOT_NAV_5_BUTTON,
            6: OS_FLEET_SLOT_NAV_6_BUTTON,
        }
        
        ship_data_list = []
        
        self.fleet_set(self.config.OpsiFleet_Fleet)
        
        for position in sorted(custom_positions):
            button = slot_buttons.get(position)
            if not button:
                logger.warning(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Недопустимая позиция корабля: {position}")
                continue
            
            logger.info(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Проверка корабля на позиции {position}")
            
            self.equip_enter(button, check_button=EQUIPMENT_OPEN, long_click=True)
            
            self.device.screenshot()
            level, exp = ship_info_get_level_exp(main=self)
            
            if level < 1 or level > len(LIST_SHIP_EXP):
                logger.warning(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Некорректно распознан уровень корабля на позиции {position}: {level}")
                ship_data_list.append({
                    "position": position,
                    "level": level,
                    "current_exp": exp,
                    "total_exp": 0,
                })
            else:
                total_exp = LIST_SHIP_EXP[level - 1] + exp
                logger.info(
                    f"Позиция {position}: уровень {level}, опыт {exp}, общий опыт {total_exp}"
                )
                ship_data_list.append({
                    "position": position,
                    "level": level,
                    "current_exp": exp,
                    "total_exp": total_exp,
                })
            
            self.ui_back(check_button=self.is_in_map)
            self.device.sleep(0.5)
        
        if not ship_data_list:
            return {'ships': None, 'error': '未收集到任何舰船数据'}
        
        logger.info(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Сбор данных по заданным позициям завершён, кораблей: {len(ship_data_list)}.")
        return {'ships': ship_data_list, 'error': None}

    def _collect_ship_data_with_retry(self, target_level):
        """
        收集舰船数据，带重试机制
        
        Args:
            target_level: 目标等级
            
        Returns:
            dict: {'ships': list, 'error': str} 
                  ships为舰船数据列表，失败时为None
                  error为错误信息，成功时为None
        """
        max_retry = 3
        non_standard_retry_count = 0
        for attempt in range(max_retry):
            logger.info(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Сбор данных кораблей (попытка {attempt + 1}/{max_retry})")
            
            self.fleet_set(self.config.OpsiFleet_Fleet)
            self.equip_enter(FLEET_FLAGSHIP)
            
            ship_data_list = []
            position = 1
            
            while True:
                self.device.screenshot()
                level, exp = ship_info_get_level_exp(main=self)
                if level < 1 or level > len(LIST_SHIP_EXP):
                    logger.warning(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Некорректно распознан уровень корабля: {level}")
                    ship_data_list.append(
                        {
                            "position": position,
                            "level": level,
                            "current_exp": exp,
                            "total_exp": 0,
                        }
                    )
                    if not self.equip_view_next():
                        break
                    position += 1
                    continue
                total_exp = LIST_SHIP_EXP[level - 1] + exp
                logger.info(
                    f"Позиция: {position}, уровень: {level}, опыт: {exp}, общий опыт: {total_exp}, целевой опыт: {LIST_SHIP_EXP[target_level - 1]}"
                )

                ship_data_list.append(
                    {
                        "position": position,
                        "level": level,
                        "current_exp": exp,
                        "total_exp": total_exp,
                    }
                )

                if not self.equip_view_next():
                    break
                position += 1
            
            self.ui_back(appear_button=EQUIPMENT_OPEN, check_button=self.is_in_map)
            
            validation_result = self._validate_ship_data(ship_data_list)
            if validation_result['valid']:
                if validation_result.get('need_retry', False):
                    current_ship_count = len(ship_data_list)
                    non_standard_retry_count += 1
                    
                    if non_standard_retry_count >= 3:
                        logger.info(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Нестандартное количество кораблей ({current_ship_count}) подтверждено тремя попытками, используются текущие результаты")
                        return {'ships': ship_data_list, 'error': None}
                    
                    logger.warning(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Нестандартное количество кораблей ({current_ship_count}), повторная проверка ({non_standard_retry_count}/3)")
                    if attempt < max_retry - 1:
                        logger.info("[Операция «Сирена» — прокачка в зоне коррозии 1] Ожидание 1 секунду перед повторной попыткой...")
                        self.device.click_record_clear()
                        import time
                        time.sleep(1)
                    else:
                        logger.info(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Достигнуто максимальное число попыток, используются текущие результаты ({current_ship_count} кораблей)")
                        return {'ships': ship_data_list, 'error': None}
                else:
                    logger.info("[Операция «Сирена» — прокачка в зоне коррозии 1] Данные кораблей прошли проверку")
                    return {'ships': ship_data_list, 'error': None}
            else:
                logger.warning(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Данные кораблей не прошли проверку: {validation_result['reason']}")
                last_error = validation_result['reason']
                if attempt < max_retry - 1:
                    logger.info("[Операция «Сирена» — прокачка в зоне коррозии 1] Ожидание 1 секунду перед повторной попыткой...")
                    self.device.click_record_clear()
                    import time
                    time.sleep(1)
                else:
                    logger.error("[Операция «Сирена» — прокачка в зоне коррозии 1] Достигнуто максимальное число попыток, собрать данные кораблей не удалось")
                    return {'ships': None, 'error': f"验证失败: {last_error}"}
        
        return {'ships': None, 'error': "未知错误"}

    def _validate_ship_data(self, ship_data_list):
        """
        验证舰船数据有效性
        
        Args:
            ship_data_list: 舰船数据列表
            
        Returns:
            dict: {'valid': bool, 'reason': str}
        """
        if not ship_data_list:
            return {'valid': False, 'reason': '舰船数据为空'}
        
        ship_count = len(ship_data_list)
        if ship_count < 1 or ship_count > 6:
            return {
                'valid': False, 
                'reason': f'舰船数量异常: {ship_count}，应为1-6艘'
            }
        
        positions = [ship['position'] for ship in ship_data_list]
        if len(positions) != len(set(positions)):
            return {
                'valid': False, 
                'reason': f'存在重复的舰船位置: {positions}'
            }
        
        for ship in ship_data_list:
            if ship['level'] < 1 or ship['level'] > 125:
                return {
                    'valid': False, 
                    'reason': f"舰船等级异常: {ship['level']}"
                }
        
        if ship_count != 6:
            return {
                'valid': True, 
                'reason': f'舰船数量为{ship_count}，非标准6艘',
                'need_retry': True
            }
        
        return {'valid': True, 'reason': ''}

    def _check_custom_positions_full_exp(self, ship_data_list, target_level, custom_positions):
        """
        检查自定义舰位是否满经验
        
        Args:
            ship_data_list: 舰船数据列表
            target_level: 目标等级
            custom_positions: 自定义舰位列表，如 [4, 5]
        """
        target_exp = LIST_SHIP_EXP[target_level - 1]
        
        detected_positions = [ship['position'] for ship in ship_data_list]
        positions_full = []
        positions_not_full = []
        positions_not_exist = []
        
        for position in custom_positions:
            if position not in detected_positions:
                logger.warning(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Позиция {position} отсутствует в текущем флоте; обнаружены позиции: {detected_positions}")
                positions_not_exist.append(str(position))
                continue
            
            for ship in ship_data_list:
                if ship['position'] == position:
                    if ship['total_exp'] >= target_exp:
                        positions_full.append(str(position))
                        logger.info(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Корабль на позиции {position} достиг предела опыта")
                    else:
                        positions_not_full.append(str(position))
                        logger.info(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Корабль на позиции {position} ещё не достиг предела опыта")
                    break
        
        if positions_not_exist:
            logger.warning(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Следующие позиции отсутствуют: {', '.join(positions_not_exist)}")
        
        if positions_not_full:
            logger.info(
                f"Корабли на пользовательских позициях ещё не достигли предела опыта: {', '.join(positions_not_full)}"
            )
        elif positions_not_exist:
            logger.warning("[Операция «Сирена» — прокачка в зоне коррозии 1] Некоторые пользовательские позиции не обнаружены; достижение предела опыта не подтверждено")
        else:
            logger.info(
                f"Все корабли на пользовательских позициях достигли предела опыта: {', '.join(positions_full)}"
            )
            self.notify_push(
                title="自定义舰位练级检查通过",
                content=f"<{self.config.config_name}> Пользовательские позиции {', '.join(positions_full)} достигли ограничения уровня {target_level}.",
            )
            
            if self.config.OpsiFleetAutoChange_Enable:
                logger.info("[Операция «Сирена» — прокачка в зоне коррозии 1] Автоподбор флота включён, запуск")
                try:
                    from module.os.tasks.fleet_auto_change import OpsiFleetAutoChange
                    auto_change = OpsiFleetAutoChange(config=self.config, device=self.device)
                    auto_change.run()
                    logger.info("[Операция «Сирена» — прокачка в зоне коррозии 1] Автоподбор флота завершён")
                except Exception as e:
                    logger.error(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Не удалось выполнить автоподбор флота: {e}")
            
            if self.config.OpsiCheckLeveling_DelayAfterFull:
                logger.info("[Операция «Сирена» — прокачка в зоне коррозии 1] Корабли на пользовательских позициях достигли предела опыта, задача будет отложена")
                self.delay_opsi_active_task(server_update=True, task='OpsiHazard1Leveling')
                self.config.task_stop()

    def _record_ap_and_coins(self, sea_miles=None):
        """记录体力和货币到 Dashboard（始终执行）。

        Args:
            sea_miles: 海里数（可选），由 detect_and_record_sea_miles 传入
        """
        try:
            if self._action_point_current > 0:
                from module.statistics.opsi_runtime import record_ap_snapshot
                record_ap_snapshot(
                    config=self.config,
                    ap_current=self._action_point_current,
                    ap_total=self._action_point_total,
                    source='hazard1',
                    distance=sea_miles,
                )

            logger.info("[Операция «Сирена» — прокачка в зоне коррозии 1] Чтение текущей валюты")
            yellow_coins = self.get_yellow_coins()
            from module.statistics.cl1_database import db as cl1_db
            from module.statistics.opsi_month import get_coins_timeline
            instance_name = getattr(self.config, 'config_name', 'default')
            # 从 DB 查找上次已知紫币值（商店写入），保持图表连续
            purple_coins_val = None
            try:
                coin_timeline = get_coins_timeline(instance_name=instance_name)
                for pt in reversed(coin_timeline):
                    if "purple_coins" in pt and pt["purple_coins"] > 0:
                        purple_coins_val = int(pt["purple_coins"])
                        break
            except Exception:
                pass
            cl1_db.async_add_coins_snapshot(
                instance_name, yellow_coins, purple_coins=purple_coins_val, source='hazard1'
            )
            self.config.save()
        except Exception as e:
            logger.error(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Ошибка записи очков действия или валюты: {e}")

    def detect_and_record_sea_miles(self):
        """
        检测海里数
        
        Returns:
            int: 海里数，失败时返回None
        """
        logger.info("[Операция «Сирена» — прокачка в зоне коррозии 1] Начало определения морских миль")
        
        try:
            logger.info("[Операция «Сирена» — прокачка в зоне коррозии 1] Проверка наличия карты Операции «Сирена»")
            if not self.is_in_map():
                logger.info("[Операция «Сирена» — прокачка в зоне коррозии 1] Карта Операции «Сирена» не открыта, возврат на карту")
                self.ui_back(check_button=self.is_in_map)
            
            logger.info("[Операция «Сирена» — прокачка в зоне коррозии 1] Переход на страницу разведданных")
            skip_first_screenshot = True
            confirm_timer = Timer(3, count=6).start()
            while 1:
                if skip_first_screenshot:
                    skip_first_screenshot = False
                else:
                    self.device.screenshot()
                
                if self.appear(MISSION_CHECK, offset=(20, 20)):
                    break
                
                if confirm_timer.reached():
                    logger.warning("[Операция «Сирена» — прокачка в зоне коррозии 1] Истекло время перехода на страницу разведданных")
                    return None
                
                if self.appear_then_click(MISSION_ENTER, offset=(200, 5), interval=3):
                    continue
            
            logger.info("[Операция «Сирена» — прокачка в зоне коррозии 1] Распознавание морских миль")
            self.device.screenshot()
            sea_miles = OCR_SEA_MILES_DIGIT.ocr(self.device.image)
            
            if sea_miles <= 0:
                logger.warning(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Некорректно распознано количество морских миль: {sea_miles}")
                return None
            
            logger.info(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Морские мили успешно распознаны: {sea_miles}")

            logger.info("[Операция «Сирена» — прокачка в зоне коррозии 1] Выход со страницы разведданных")
            self.ui_click(
                MISSION_QUIT,
                check_button=self.is_in_map,
                offset=(20, 20),
                skip_first_screenshot=True
            )

            return sea_miles
            
        except Exception as e:
            logger.error(f"[Операция «Сирена» — прокачка в зоне коррозии 1] Не удалось определить морские мили: {e}")
            try:
                if self.appear(MISSION_CHECK, offset=(20, 20)):
                    self.ui_click(
                        MISSION_QUIT, 
                        check_button=self.is_in_map,
                        offset=(20, 20),
                        skip_first_screenshot=True
                    )
            except Exception:
                pass
            return None
