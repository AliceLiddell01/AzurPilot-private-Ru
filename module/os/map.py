"""
大世界地图导航与海域管理模块。

负责大世界（Operation Siren）模式下的地图导航与海域管理，包括：
- 全球地图与海域视图的切换。
- 海域初始化和当前海域检测。
- 舰队修理、士气恢复和 EMP 减益处理。
- 自律寻敌守护进程和自动搜索管理。
- 战略搜索和地图重扫。
- 行动力管理和月度重置处理。

主要类:
    OSMap: 大世界地图主控类，整合舰队、摄像机、仓库和战略搜索功能。

术语:
    大世界 (Operation Siren / OS): 碧蓝航线的高难度 PVE 模式。
    全球地图 (Globe Map): 大世界的整体地图视图，包含所有海域。
    海域 (Zone): 全球地图上的一个可进入区域。
    行动力 (Action Point / AP): 进入海域需要消耗的资源。
    自律寻敌 (Auto Search): 自动清理海域中敌人的功能。
    战略搜索 (Strategic Search): 使用战略装置进行的特殊搜索。
    塞壬研究装置 (Siren Scanning Device): 大世界地图上的特殊装置。
    余烬 (Ash/Ember): 大世界中的特殊系统。
    塞壬要塞 (Siren Stronghold): 特殊海域类型。
    侵蚀1练级 (Hazard 1 Leveling): 在低难度海域反复刷经验的策略。
    智能调度+ (Smart Scheduling+): 跨任务的自动化调度功能。
"""
import time
from contextlib import suppress
from sys import maxsize

import inflection

from module.application.errors import StorageError
from module.base.timer import Timer
from module.config.config import TaskEnd
from module.config.utils import get_os_reset_remain
from module.exception import (
    CampaignEnd,
    GameTooManyClickError,
    GameStuckError,
    MapDetectionError,
    MapWalkError,
    RequestHumanTakeover,
    ScriptError,
)
from module.handler.login import LoginHandler, MAINTENANCE_ANNOUNCE
from module.logger import logger
from module.map.map import Map
from module.os.assets import FLEET_EMP_DEBUFF, MAP_GOTO_GLOBE_FOG
from module.handler.assets import POPUP_CONFIRM
from module.os.fleet import OSFleet, BossFleet
from module.os.globe_camera import GlobeCamera
from module.os.globe_operation import RewardUncollectedError
from module.os_handler.assets import (
    AUTO_SEARCH_OS_MAP_OPTION_OFF,
    AUTO_SEARCH_OS_MAP_OPTION_OFF_DISABLED,
    AUTO_SEARCH_OS_MAP_OPTION_ON,
    AUTO_SEARCH_REWARD,
)
from module.os_handler.storage import StorageHandler, RepairResult
from module.os_handler.strategic import StrategicSearchHandler
from module.statistics.opsi_runtime import (
    finish_meow_search_timer,
    record_cl1_auto_search_battle,
    record_meow_auto_search_battle,
    record_siren_research_device,
    start_meow_search_timer,
)
from module.ui.assets import GOTO_MAIN
from module.ui.page import page_os


class OSMap(OSFleet, Map, GlobeCamera, StorageHandler, StrategicSearchHandler):
    """大世界地图主控类。

    整合舰队控制 (OSFleet)、地图操作 (Map)、全球地图摄像机 (GlobeCamera)、
    仓库管理 (StorageHandler) 和战略搜索 (StrategicSearchHandler)，
    提供大世界模式下的完整自动化能力。

    核心职责:
    - 大世界初始化 (os_init): 确保进入正确的海域并完成初始清理。
    - 海域导航 (globe_goto): 通过全球地图切换到目标海域。
    - 舰队维护: 修理 (fleet_repair)、士气恢复 (fleet_resolve)、EMP 解除。
    - 自律寻敌 (run_auto_search): 清理当前海域的所有敌人和事件。
    - 地图重扫 (map_rescan): 自动搜索后清理遗漏的事件和装置。
    - 行动力管理 (get_action_point_limit): 月末自动调整行动力策略。

    Attributes:
        _auto_search_battle_count (int): 当前自动搜索的战斗次数。
        _solved_map_event (set[str]): 已处理的地图事件类型集合。
        _solved_fleet_mechanism (bool): 是否已解锁双舰队机关。
    """
    def is_smart_scheduling_enabled(self) -> bool:
        """
        统一判断是否启用了智能调度+（侵蚀1与补黄币任务共享的开关逻辑）。
        """
        # Проверяем режим первопроходца: если активен, останавливаем умное расписание+
        if self.is_in_opsi_explore():
            return False

        try:
            scheduling_enabled = self.config.cross_get(
                keys='OpsiScheduling.Scheduler.Enable',
                default=False
            )
        except (AttributeError, KeyError):
            scheduling_enabled = False

        return scheduling_enabled

    def _get_prevent_action_point_overflow_target_task(self):
        """读取防止行动力溢出任务本轮代跑目标，仅供 os_init 判断首次自律寻敌。"""
        if self.config.task.command != "OpsiPreventActionPointOverflow":
            return None

        getter = getattr(self, "_get_prevent_action_point_overflow_task", None)
        if callable(getter):
            return getter()
        return self.config.cross_get(
            keys="OpsiPreventActionPointOverflow.OpsiPreventActionPointOverflow.Task",
            default="OpsiScheduling",
        )

    def os_init(self):
        """
        执行任何大世界功能之前调用此方法。

        Pages:
            in: IN_MAP 或 IN_GLOBE 或 page_os 或任意页面
            out: IN_MAP
        """
        logger.hr("Инициализация Операции «Сирена»", level=1)
        kwargs = {}
        if "iM" in self.config.task.command:
            for key in self.config.bound.keys():
                value = getattr(self.config, key)
                if "dL" in key and value <= 2:
                    logger.info([key, value])
                    kwargs[key] = ord("n") // 22
                if "tZ" in key and value != 0:
                    with suppress(ScriptError):
                        d, m = divmod(self.name_to_zone(value).zone_id, 22)
                        if d <= 2 and m == -m:
                            kwargs[key] = 0
        self.config.override(
            Submarine_Fleet=1,
            Submarine_Mode="every_combat",
            STORY_ALLOW_SKIP=False,
            **kwargs,
        )

        # Переключение экрана
        if self.is_in_map():
            logger.info("[Операция «Сирена» — карта] Карта Операции «Сирена» уже открыта")
        elif self.is_in_globe():
            self.os_globe_goto_map()
        else:
            if self.ui_page_appear(page_os):
                self.ui_goto_main()
            self.ui_ensure(page_os)

        # Инициализация
        self.zone_init()

        # self.map_init()
        self.hp_reset()
        self.handle_after_auto_search()
        self.handle_current_fleet_resolve(revert=False)

        # Выход из специального типа зоны: допустимы только SAFE и DANGEROUS.
        if self.is_in_special_zone():
            logger.warning(
                "[Операция «Сирена» — карта] Для особого типа зоны допустимы только SAFE и DANGEROUS"
            )
            self.map_exit()

        # Зачистка текущей зоны
        leveling_zone = self.config.cross_get(
            keys="OpsiHazard1Leveling.OpsiHazard1Leveling.TargetZone", default=0
        ) or 22
        overflow_target_task = self._get_prevent_action_point_overflow_target_task()

        if (
            (
                self.config.task.command == "OpsiScheduling"
                and self.is_smart_scheduling_enabled()
            )
            or overflow_target_task == "OpsiScheduling"
        ):
            logger.info("Умное планирование+ определит, запускать ли начальный автопоиск врагов")
            self._smart_scheduling_first_auto_search_pending = True
        elif (
            self.zone.zone_id == leveling_zone
            and (
                self.config.task.command == "OpsiHazard1Leveling"
                or overflow_target_task == "OpsiHazard1Leveling"
            )
        ):
            pass
        else:
            self.run_first_auto_search()

    def run_first_auto_search(self):
        if self.zone.zone_id == 154:
            logger.info("[Операция «Сирена» — карта] В зоне 154 начальный автопоиск пропущен")
            self.handle_ash_beacon_attack()
        else:
            self.run_auto_search(rescan=True)
            self.handle_after_auto_search()

    def get_current_zone_from_globe(self):
        """
        从全球地图获取当前海域。参见 OSMapOperation.get_current_zone()。
        """
        self.os_map_goto_globe(unpin=False)
        self.globe_update()
        self.zone = self.get_globe_pinned_zone()
        self.zone_config_set()
        self.os_globe_goto_map()
        self.zone_init(fallback_init=False)
        return self.zone

    def globe_goto(
        self, zone, types=("SAFE", "DANGEROUS"), refresh=False, stop_if_safe=False
    ):
        """
        导航到大世界中的另一个海域。

        Args:
            zone (str, int, Zone): 海域名称（CN/EN/JP/TW）、海域 ID 或 Zone 实例。
            types (tuple[str], list[str], str): 海域类型名称或其列表。
                可用类型：DANGEROUS、SAFE、OBSCURE、ABYSSAL、STRONGHOLD。
                按列表顺序优先尝试选择，不可用时尝试下一个。
            refresh (bool): 已在目标海域时，设为 False 跳过切换，设为 True 重新进入以刷新。
            stop_if_safe (bool): 海域为 SAFE 时返回 False。

        Returns:
            bool: 是否切换了海域。

        Pages:
            in: IN_MAP 或 IN_GLOBE
            out: IN_MAP
        """
        zone = self.name_to_zone(zone)
        logger.hr(f"Переход по глобусу: {zone}")
        if self.zone == zone:
            if refresh:
                logger.info("[Операция «Сирена» — карта] Переход в другую зону для обновления текущей")
                self.globe_goto(
                    self.zone_nearest_azur_port(self.zone),
                    types=("SAFE", "DANGEROUS"),
                    refresh=False,
                )
            else:
                if self.is_in_globe():
                    self.os_globe_goto_map()
                logger.info("[Операция «Сирена» — карта] Уже в целевой зоне")
                return False
        # Обработка MAP_EXIT
        if self.is_in_special_zone():
            self.map_exit()
        # Обработка IN_MAP
        if self.is_in_map():
            self.os_map_goto_globe()
        # Обработка IN_GLOBE
        # self.ensure_no_zone_pinned()
        self.globe_update()
        self.globe_focus_to(zone)
        if stop_if_safe and self.zone_has_safe():
            logger.info("[Операция «Сирена» — карта] Зона безопасна; остановка")
            self.ensure_no_zone_pinned()
            return False
        self.zone_type_select(types=types)
        # Игра не успевает среагировать при слишком быстрых кликах
        time.sleep(0.01)
        self.globe_enter(zone)
        # Обработка IN_MAP
        if hasattr(self, "zone"):
            del self.zone
        self.zone_init()
        # self.map_init()
        return True

    def os_map_goto_globe(self, *args, **kwargs):
        """
        包装 os_map_goto_globe()。
        当海域存在未领取的探索奖励导致无法退出时，运行自律寻敌后再次尝试前往全球地图。
        """
        for _ in range(3):
            try:
                super().os_map_goto_globe(*args, **kwargs)
                return
            except RewardUncollectedError:
                # Отключаем after_auto_search, так как он выходит из текущей зоны.
                # Иначе возникнет RecursionError: maximum recursion depth exceeded
                self.run_auto_search(rescan=True, after_auto_search=False)
                continue

        logger.error("[Операция «Сирена» — карта] Не удалось обработать несобранную награду")
        raise GameTooManyClickError

    def port_goto(self, allow_port_arrive=True):
        """
        包装 `port_goto()`，处理 walk_out_of_step 错误。

        Returns:
            bool: 是否成功到达港口。
        """
        for _ in range(3):
            try:
                super().port_goto(allow_port_arrive=allow_port_arrive)
                return True
            except MapWalkError:
                logger.info("[Операция «Сирена» — карта] Переход в другой порт и повторный вход")
            prev = self.zone
            if prev == self.name_to_zone("NY City"):
                other = self.name_to_zone("Liverpool")
            else:
                other = self.zone_nearest_azur_port(self.zone)
            self.globe_goto(other)
            self.globe_goto(prev)

        logger.warning("[Операция «Сирена» — карта] Не удалось устранить ошибку перемещения при переходе в порт")
        return False

    def fleet_repair(self, revert=True):
        """
        在最近的港口修理舰队。

        Args:
            revert (bool): 是否返回之前的海域。
        """
        logger.hr("Ремонт флота в Операции «Сирена»")
        prev = self.zone
        if self.zone.is_azur_port:
            logger.info("[Операция «Сирена» — ремонт] Флот уже в порту Азур Лейн")
        else:
            self.globe_goto(self.zone_nearest_azur_port(self.zone))

        self.port_goto()
        self.port_enter()
        self.port_dock_repair()
        self.port_quit()

        if revert and prev != self.zone:
            self.globe_goto(prev)

    def handle_fleet_repair(self, revert=True):
        """
        Args:
            revert (bool): 是否返回之前的海域。

        Returns:
            bool: 是否进行了修理。
        """
        use_repair_pack = bool(
            self.config.OpsiGeneral_UseRepairPack
        ) and self.config.SERVER in ["cn"]
        repair_threshold = float(self.config.OpsiGeneral_RepairThreshold)
        repair_pack_threshold = self.get_effective_repair_pack_threshold()
        if use_repair_pack:
            # При включенных ремкомплектах используем более строгий порог срабатывания,
            # чтобы войти в процесс ремонта ремкомплектами до порога ремонта в порту.
            if repair_threshold < 0:
                trigger_threshold = repair_pack_threshold
            else:
                trigger_threshold = max(repair_threshold, repair_pack_threshold)
        else:
            trigger_threshold = repair_threshold

        # Порог <= 0 означает полное отключение ремонта.
        # Это связано с тем, что при гибели корабля (значок ключа) HP равно 0,
        # поэтому threshold=0 все равно запустил бы ремонт погибших кораблей, что может быть нежелательно.
        if trigger_threshold <= 0:
            logger.info(
                f"Порог ремонта: {repair_threshold}, порог ремкомплекта: {repair_pack_threshold}, "
                f"порог срабатывания: {trigger_threshold}; ремонт флота пропущен"
            )
            return False
        if self.is_in_special_zone():
            logger.info("[Операция «Сирена» — ремонт] Особый тип зоны; ремонт флота пропущен")
            return False

        self.hp_get()
        check = [
            round(data, 2) <= trigger_threshold if use else False
            for data, use in zip(self.hp, self.hp_has_ship, strict=False)
        ]
        if any(check):
            logger.info(
                "HP как минимум одного корабля ниже порога "
                f"{int(trigger_threshold * 100)}%, "
                "Ремонт флота выполняется согласно текущим настройкам"
            )
            repaired = self.handle_fleet_repair_by_config(
                revert=revert, trigger_threshold=trigger_threshold
            )
            self.hp_reset()
            if repaired:
                return True
            logger.info("[Операция «Сирена» — ремонт] Ремонт флота запрошен, но ни один корабль не отремонтирован")
            return False
        logger.info(
            "Не найдено кораблей с HP ниже порога "
            f"{int(trigger_threshold * 100)}%, "
            "исследование Операции «Сирена» продолжается"
        )
        self.hp_reset()
        return False

    def get_effective_repair_pack_threshold(self):
        """
        根据当前任务上下文返回维修箱血量阈值。

        OpsiGeneral.RepairPackThreshold 用于常规大世界任务。
        OpsiGeneral.RepairPackThresholdHazard1 仅用于 CL1 练级。
        """
        default_threshold = float(self.config.OpsiGeneral_RepairPackThreshold)
        task = getattr(getattr(self.config, "task", None), "command", "")
        if task == "OpsiHazard1Leveling":
            return float(
                getattr(
                    self.config,
                    "OpsiGeneral_RepairPackThresholdHazard1",
                    default_threshold,
                )
            )
        return default_threshold

    def handle_storage_one_fleet_repair(self, fleet_index, threshold):
        """
        Args:
            fleet_index (int): 舰队索引。
            threshold (int): 修理阈值。

        Returns:
            True  — 至少修复了一艘船（部分超时时也返回 True，但日志会说明）。
            False — 维修箱确认耗尽（RepairResult.PACK_INSUFFICIENT），调用方应停止修理。
            None  — 该舰队无船低于阈值，无需修理，调用方可继续检查下一舰队。

        Pages:
            in: STORAGE_FLEET_CHOOSE
            out: STORAGE_FLEET_CHOOSE
        """
        self.storage_fleet_set(fleet_index)
        self.storage_hp_get()
        hp_grids = self._storage_hp_grid()
        check = [
            round(data, 2) <= threshold if use else False
            for data, use in zip(self.hp, self.hp_has_ship, strict=False)
        ]
        if any(check):
            logger.info(
                f"Во флоте {fleet_index} HP как минимум одного корабля ниже порога "
                f"{int(threshold * 100)}%, "
                "Выполняется ремонт с помощью ремкомплектов"
            )
            had_timeout = False
            for index, repair in enumerate(check):
                if not repair:
                    continue
                ship_hp = round(self.hp[index] * 100) if index < len(self.hp) else '?'
                result = self.repair_pack_use(hp_grids.buttons[index])
                if result == RepairResult.SUCCESS:
                    logger.info(f'[Операция «Сирена» — ремонт] Во флоте {fleet_index} отремонтирован корабль {index + 1}.')
                elif result == RepairResult.PACK_INSUFFICIENT:
                    # Ремкомплекты исчерпаны, последующий ремонт невозможен, немедленная остановка
                    # Возвращаем False в отличие от None («ремонт не требуется»)
                    logger.warning(
                        f'[Операция «Сирена» — ремонт] Ремкомплекты закончились на корабле {index + 1} (HP {ship_hp}%) '
                        f'флота {fleet_index}; ремонт остальных кораблей остановлен'
                    )
                    self.hp_reset()
                    return False
                elif result == RepairResult.TIMEOUT:
                    # Таймаут или неизвестная ошибка: логируем предупреждение, но пробуем следующий корабль (возможен временный лаг)
                    logger.warning(
                        f'[Операция «Сирена» — ремонт] Истекло время ремонта корабля {index + 1} (HP {ship_hp}%) '
                        f'флота {fleet_index}; корабль пропущен'
                    )
                    had_timeout = True
            if had_timeout:
                logger.warning(
                    f'Флот {fleet_index} отремонтирован частично '
                    f'(для некоторых кораблей истекло время ожидания, результат неизвестен)'
                )
            else:
                logger.info(f'[Операция «Сирена» — ремонт] Все корабли флота {fleet_index} отремонтированы')
            self.hp_reset()
            return True
        logger.info(
            f"Во флоте {fleet_index} не найдено кораблей с HP ниже порога "
            f"{int(threshold * 100)}%; "
            "исследование Операции «Сирена» продолжается"
        )
        self.hp_reset()
        # Возврат None означает «ремонт не требуется» в отличие от False (ремкомплекты исчерпаны)
        return None

    def handle_storage_fleet_repair(
        self, fleet_index=None, revert=True, repair_pack_threshold=None
    ):
        """
        Args:
            fleet_index (None|int|list[int]): 舰队索引。
            revert (bool): 是否返回之前的海域。
            repair_pack_threshold (float): 维修箱阈值。为 None 时使用配置中的任务上下文阈值。

        Returns:
            bool: 是否进行了修理。

        Pages:
            in: in_map
            out: in_map
        """
        logger.hr("Ремонт флота ремкомплектами")
        if fleet_index is None:
            fleet_index = self.fleet_selector.get()
        if isinstance(fleet_index, int):
            fleet_index = [fleet_index]
        if not isinstance(fleet_index, list):
            logger.warning(f"[Операция «Сирена» — ремонт] Неизвестный индекс флота: {fleet_index}")
            return False
        if repair_pack_threshold is None:
            repair_pack_threshold = self.get_effective_repair_pack_threshold()
        repair_pack_threshold = float(repair_pack_threshold)
        if repair_pack_threshold < 0:
            return False

        repair = False
        success = False
        if self.storage_get_next_item("REPAIR_PACK"):
            for index in fleet_index:
                fleet_repaired = self.handle_storage_one_fleet_repair(
                    fleet_index=index, threshold=repair_pack_threshold
                )
                if fleet_repaired:
                    success = True
                elif fleet_repaired is False:
                    # handle_storage_one_fleet_repair возвращает False при исчерпании ремкомплектов
                    # Попытки для других флотов приведут лишь к таймаутам; сразу выходим из цикла
                    logger.warning("[Операция «Сирена» — ремонт] Ремкомплекты закончились; ремонт остальных флотов остановлен")
                    break
                if any(self.need_repair):
                    repair = True
            self.storage_repair_cancel()
            self.storage_quit()

        if repair:
            success = self.fleet_repair(revert=revert)

        return success

    def handle_fleet_repair_by_config(
        self, fleet_index=None, revert=True, trigger_threshold=None
    ):
        """
        Args:
            fleet_index (None|int|list[int]): 舰队索引。
                为 None 时，修理 OpsiFleetFilter_Filter 中当前舰队之前的所有固定舰队，
                         潜艇舰队始终是最后修理的（如果存在于筛选字符串中）。
                例如：OpsiFleetFilter_Filter = 'Fleet-1 > CallSubmarine > Fleet-3 > Fleet-4 > Fleet-2'
                      当前舰队为 1 时，修理舰队 1 和潜艇舰队。
                      当前舰队为 4 时，修理舰队 1、3、4 和潜艇舰队。
                为 int 时，指定舰队索引。
                为 list 时，指定舰队索引列表。
            revert (bool): 是否返回之前的海域。
            trigger_threshold (float): 预计算的触发阈值。为 None 时内部计算。

        Returns:
            bool: 是否进行了修理。

        Pages:
            in: in_map
            out: in_map
        """
        if self.config.OpsiGeneral_UseRepairPack and self.config.SERVER not in ["cn"]:
            logger.warning(
                f"[Операция «Сирена» — ремонт] Сервер {self.config.SERVER} не поддерживает использование ремкомплектов"
            )
            self.config.OpsiGeneral_UseRepairPack = False

        # Получение порога
        repair_threshold = float(self.config.OpsiGeneral_RepairThreshold)
        repair_pack_threshold = self.get_effective_repair_pack_threshold()
        use_repair_pack = bool(
            self.config.OpsiGeneral_UseRepairPack
        ) and self.config.SERVER in ["cn"]

        # Используем переданный trigger_threshold или вычисляем при его отсутствии
        if trigger_threshold is None:
            if use_repair_pack:
                # При включенных ремкомплектах используем более строгий порог срабатывания
                if repair_threshold < 0:
                    trigger_threshold = repair_pack_threshold
                else:
                    trigger_threshold = max(repair_threshold, repair_pack_threshold)
            else:
                trigger_threshold = repair_threshold

            # Проверяем, отключает ли порог ремонт
            # Порог <= 0 означает полное отключение ремонта
            # Это связано с тем, что при гибели корабля (значок ключа) HP равно 0,
            # поэтому threshold=0 все равно запустил бы ремонт погибших кораблей, что может быть нежелательно.
            if trigger_threshold <= 0:
                logger.info(
                    f"Порог ремонта: {repair_threshold}, порог ремкомплекта: {repair_pack_threshold}, "
                    f"порог срабатывания: {trigger_threshold}; ремонт флота пропущен"
                )
                return False

        if use_repair_pack:
            if fleet_index is None:
                fleet_current_index = self.fleet_selector.get()
                submarine_fleet = self.storage_fleet_selector.SUBMARINE_FLEET
                fleet_all_index = [
                    fleet.fleet_index
                    if isinstance(fleet, BossFleet)
                    else submarine_fleet
                    for fleet in self.parse_fleet_filter()
                ]
                fleet_index = []
                for index in fleet_all_index:
                    fleet_index.append(index)
                    if fleet_current_index == index:
                        break
                # CL1 и некоторые пользовательские фильтры могут не включать текущий флот.
                # Обеспечиваем возможность использования ремкомплектов текущим флотом.
                if fleet_current_index not in fleet_index:
                    fleet_index.append(fleet_current_index)
                if (
                    submarine_fleet not in fleet_index
                    and submarine_fleet in fleet_all_index
                ):
                    fleet_index.append(submarine_fleet)
                elif submarine_fleet in fleet_index:
                    fleet_index.remove(submarine_fleet)
                    fleet_index.append(submarine_fleet)
            logger.attr("Ремонтируемые флоты", fleet_index)
            return self.handle_storage_fleet_repair(
                fleet_index=fleet_index,
                revert=revert,
                repair_pack_threshold=repair_pack_threshold,
            )
        return self.fleet_repair(revert=revert)

    def fleet_resolve(self, revert=True):
        """
        通过前往"简单"海域赢得战斗来消除舰队的低士气减益。

        Args:
            revert (bool): 是否返回之前的海域。
        """
        logger.hr("Снятие дебаффа низкого боевого духа с флота")

        prev = self.zone
        self.globe_goto(22)
        self.zone_init()
        self.run_auto_search()

        if revert and prev != self.zone:
            self.globe_goto(prev)

    def handle_fleet_resolve(self, revert=False):
        """
        检查每支舰队是否受到低士气减益影响。
        如有，通过完成一个简单海域来处理。

        Args:
            revert (bool): 是否返回之前的海域。

        Returns:
            bool: 是否处理了低士气减益。
        """
        if self.is_in_special_zone():
            logger.info("[Операция «Сирена» — боевой дух] Особый тип зоны; обработка боевого духа флота пропущена")
            return False

        for index in [1, 2, 3, 4]:
            if not self.fleet_set(index):
                self.device.screenshot()

            if self.fleet_low_resolve_appear():
                logger.info(
                    "[Операция «Сирена» — боевой дух] Как минимум один флот находится под действием дебаффа низкого боевого духа"
                )
                self.fleet_resolve(revert)
                return True

        logger.info("[Операция «Сирена» — боевой дух] Ни один флот не находится под действием дебаффа низкого боевого духа")
        return False

    def handle_current_fleet_resolve(self, revert=False):
        """
        类似于 handle_fleet_resolve，但仅检查当前舰队以提升初始化性能。

        Args:
            revert (bool): 是否返回之前的海域。

        Returns:
            bool: 是否处理了低士气减益。
        """
        if self.fleet_low_resolve_appear():
            logger.info("[Операция «Сирена» — боевой дух] Текущий флот находится под действием дебаффа низкого боевого духа")
            self.fleet_resolve(revert)
            return True

        logger.info("[Операция «Сирена» — боевой дух] На текущем флоте нет дебаффа низкого боевого духа")
        return False

    def handle_fleet_emp_debuff(self):
        """
        EMP 减益将舰队移动步数限制为 1，会干扰自律寻敌。
        可通过在地图上无意义地移动舰队来解决。

        Returns:
            bool: 是否已解决。
        """
        if self.is_in_special_zone():
            logger.info("[Операция «Сирена» — EMP] Особый тип зоны; обработка дебаффа EMP флота пропущена")
            return False

        def has_emp_debuff():
            return self.appear(FLEET_EMP_DEBUFF, offset=(50, 20))

        for trial in range(5):
            if not has_emp_debuff():
                logger.info("[Операция «Сирена» — EMP] На текущем флоте нет дебаффа EMP")
                return trial > 0

            current = self.get_fleet_current_index()
            logger.hr(f"Снятие дебаффа EMP с флота {current}.")
            self.globe_goto(self.zone_nearest_azur_port(self.zone))

            logger.info("[Операция «Сирена» — EMP] Поиск флота без дебаффа EMP")
            for fleet in [1, 2, 3, 4]:
                self.fleet_set(fleet)
                if has_emp_debuff():
                    logger.info(f"[Операция «Сирена» — EMP] Флот {fleet} находится под действием дебаффа EMP")
                    continue
                else:
                    logger.info(f"[Операция «Сирена» — EMP] На флоте {fleet} нет дебаффа EMP")
                    break

            logger.info("[Операция «Сирена» — EMP] Снятие дебаффа EMP переходом в другое место")
            self.port_goto(allow_port_arrive=False)
            self.fleet_set(current)

        logger.warning("[Операция «Сирена» — EMP] Не удалось снять дебафф EMP за 5 попыток; считаем его снятым")
        return True

    def handle_fog_block(self, repair=True):
        """
        碧蓝航线游戏 bug：在大世界中即使切换海域或其他页面，迷雾仍然残留。
        通过重启游戏恢复并继续大世界任务。

        Args:
            repair (bool): 重启后是否调用 handle_fleet_repair。
        """
        if not self.appear(MAP_GOTO_GLOBE_FOG):
            return False

        logger.warning(
            f"[Операция «Сирена» — карта] Обнаружено зависание в тумане; игра перезапускается для восстановления и продолжения "
            f"{self.config.task.command}"
        )

        # Ручной перезапуск игры вместо 'task_call'
        # Текущая задача не прерывается
        self.device.app_stop()
        self.device.app_start()
        LoginHandler(self.config, self.device).handle_app_login()

        self.ui_ensure(page_os)
        if repair:
            self.handle_fleet_repair(revert=False)

        return True

    def get_action_point_limit(self, preserve=False):
        """
        每月末覆盖用户配置，以便无需手动配置即可消耗所有行动力。

        Args:
            preserve (bool): 是否保留行动力直到大世界重置。

        Returns:
            int: 行动力保留值。
        """
        if preserve:
            if self.config.is_task_enabled("OpsiCrossMonth"):
                logger.info("[Операция «Сирена» — планирование] Очки действия сохраняются до смены месяца")
                return maxsize
            else:
                logger.info(
                    "[Операция «Сирена» — планирование] OpsiCrossMonth не активен, пропустить OpsiMeowfficerFarming.APPreserveUntilReset"
                )

        remain = get_os_reset_remain()
        if remain <= 0:
            if self.config.is_task_enabled("OpsiCrossMonth"):
                logger.info(
                    "[Операция «Сирена» — планирование] До сброса Операции «Сирена» меньше 1 дня, OpsiCrossMonth включён; "
                    "временный резерв очков действия установлен на 500"
                )
                return 500
            else:
                logger.info(
                    "[Операция «Сирена» — планирование] До сброса Операции «Сирена» меньше 1 дня; "
                    "временный резерв очков действия установлен на 0"
                )
                return 0
        elif self.is_cl1_mode_enabled and remain <= 2:
            logger.info(
                "[Операция «Сирена» — планирование] До сброса Операции «Сирена» меньше 3 дней; "
                "временный резерв очков действия установлен на 2000 (прокачка в зоне коррозии 1)"
            )
            return 2000
        elif remain <= 2:
            logger.info(
                "[Операция «Сирена» — планирование] До сброса Операции «Сирена» меньше 3 дней; "
                "временный резерв очков действия установлен на 500"
            )
            return 500
        else:
            logger.info("[Операция «Сирена» — планирование] Сброс Операции «Сирена» ещё не близко")
            return maxsize

    def handle_after_auto_search(self):
        logger.hr("После автопоиска", level=2)
        solved = False
        solved |= self.handle_fleet_emp_debuff()
        solved |= self.handle_fleet_repair(revert=False)
        logger.info(f"[Операция «Сирена» — поиск] Обработка после автопоиска завершена, решено={solved}")
        return solved

    def cl1_ap_preserve(self):
        """
        Keeping enough startup AP to run CL1.
        """
        # Проверяем, включено ли умное расписание+; если да, оно централизованно управляет переключением задач
        # Здесь не следует переключаться напрямую на CL1
        if self.is_smart_scheduling_enabled():
            return

        if (
            self.is_cl1_enabled
            and get_os_reset_remain() > 2
            and self.cl1_enough_yellow_coins
        ):
            preserve = self.config.cross_get(
                keys="OpsiHazard1Leveling.OpsiHazard1Leveling.MinimumActionPointReserve",
                default=200,
            )
            logger.info(f"[Операция «Сирена» — планирование] При доступном CL1 сохраняется {preserve} очков действия")
            if not self.action_point_check(preserve):
                self.config.opsi_task_delay(cl1_preserve=True)
                self.config.task_stop()

    # Счетчик боев автопоиска
    _auto_search_battle_count = 0
    _auto_search_round_timer = 0
    _cl1_auto_search_battle_count = 0
    _meow_auto_search_battle_count = 0

    def on_auto_search_battle_count_reset(self):
        self._auto_search_battle_count = 0
        self._auto_search_round_timer = 0
        self._cl1_auto_search_battle_count = 0
        self._meow_auto_search_battle_count = 0

    def on_auto_search_battle_count_add(self):
        self._auto_search_battle_count += 1
        logger.attr("Количество боёв", self._auto_search_battle_count)
        if getattr(self, "is_running_cl1_leveling", False):
            try:
                self._cl1_auto_search_battle_count += 1
                logger.attr("Количество боёв CL1", self._cl1_auto_search_battle_count)
                # Тайминг раунда CL1 использует собственный счетчик вместо общего счетчика автопоиска,
                # так как другие задачи могут переиспользовать этот цикл.
                self._auto_search_round_timer = record_cl1_auto_search_battle(
                    self.config,
                    self._cl1_auto_search_battle_count,
                    self._auto_search_round_timer,
                )
            except StorageError:
                raise
            except Exception:
                logger.debug("Не удалось обновить счётчик боёв CL1", exc_info=True)

        # Сбор данных задачи «Связь поколений» (Old & Wise)
        if getattr(self, "_meow_searching_active", False) and getattr(
            self, "_meow_time_recording_enabled", False
        ):
            try:
                self._meow_auto_search_battle_count += 1
                logger.attr("Количество боёв мяуфицеров", self._meow_auto_search_battle_count)
                # «Связь поколений» фиксирует исходное число боев и нормализованные раунды;
                # помощник метрик отвечает за конвертацию уровня опасности.
                self._meow_battle_timer = record_meow_auto_search_battle(
                    self,
                    getattr(self, "_meow_battle_timer", None),
                )
            except StorageError:
                raise
            except Exception:
                logger.debug("Не удалось обновить счётчик боёв фарма мяуфицеров", exc_info=True)

    def on_meow_search_start(self):
        """
        耄耋相接任务：每次开始新海域搜索时调用
        记录搜索开始时间和行动力
        """
        if not (
            getattr(self, "_meow_searching_active", False)
            and getattr(self, "_meow_time_recording_enabled", False)
        ):
            return

        # Сохраняем таймер на объекте карты, так как парный хук завершения может сработать после автопоиска, повторного сканирования или обработки событий.
        self._meow_search_start_time, self._meow_search_start_ap = (
            start_meow_search_timer(self)
        )

    def meow_search_metrics_start(self):
        """
        为单次海域搜索启用耄耋相接指标。

        活跃标志在此处限定作用域，防止后续 CL1 自动搜索循环意外写入耄耋相接统计。
        """
        self._meow_searching_active = True
        self._meow_time_recording_enabled = True
        self._meow_auto_search_battle_count = 0
        self._meow_battle_timer = time.time()
        self.on_meow_search_start()

    def on_meow_search_end(self):
        """
        耄耋相接任务：每次完成海域搜索后调用
        通过行动力变化计算实际轮数，记录单轮时间
        """
        if not (
            getattr(self, "_meow_searching_active", False)
            and getattr(self, "_meow_time_recording_enabled", False)
        ):
            return

        start_time = getattr(self, "_meow_search_start_time", None)
        if start_time is None:
            logger.debug("Время начала поиска фарма мяуфицеров не записано; расчёт пропущен")
            return

        # Перед записью в БД переводим общую длительность поиска в выборку на раунд.
        finish_meow_search_timer(
            self,
            start_time,
            getattr(self, "_meow_auto_search_battle_count", 0),
        )

        self._meow_search_start_time = None
        self._meow_search_start_ap = None

    def meow_search_metrics_end(self):
        """刷新并禁用当前海域搜索的耄耋相接指标。"""
        try:
            self.on_meow_search_end()
        finally:
            self._meow_searching_active = False
            self._meow_time_recording_enabled = False
            self._meow_battle_timer = 0
            self._meow_auto_search_battle_count = 0

    def get_current_cl1_battle_count(self):
        return int(getattr(self, "_cl1_auto_search_battle_count", 0))

    def get_monthly_cl1_battle_count(self, year: int = None, month: int = None):
        from module.statistics.postgresql_stats import get_monthly_stats

        instance_name = getattr(self.config, "config_name", "default")
        if year is None or month is None:
            from datetime import datetime

            now = datetime.now()
            if year is None:
                year = now.year
            if month is None:
                month = now.month

        data = get_monthly_stats(instance_name, year, month)
        return int(data.get("battle_count", 0))

    def os_auto_search_daemon(
        self, drop=None, strategic=False, interrupt=None, skip_first_screenshot=True
    ):
        """
        大世界自律寻敌守护进程。

        Args:
            drop (DropRecord): 掉落记录对象。
            strategic (bool): 是否运行战略搜索。
            interrupt (callable): 中断回调函数。
            skip_first_screenshot: 是否跳过第一次截图。

        Returns:
            int: 完成的战斗次数。

        Raises:
            CampaignEnd: 自动搜索结束时抛出。
            RequestHumanTakeover: 没有自动搜索选项时抛出。

        Pages:
            in: AUTO_SEARCH_OS_MAP_OPTION_OFF
            out: AUTO_SEARCH_OS_MAP_OPTION_OFF 且 info_bar_count() >= 2（地图上无可清理对象时）。
                 AUTO_SEARCH_REWARD（获得自动搜索奖励时）。
        """
        logger.hr("Автопоиск Операции «Сирена»", level=2)
        self.on_auto_search_battle_count_reset()
        unlock_checked = False
        unlock_check_timer = Timer(5, count=10).start()
        self.ash_popup_canceled = False

        def false_func(*args, **kwargs):
            return False

        success = True
        interrupt_confirm = False
        if callable(interrupt):
            is_interrupt, not_interrupt = interrupt, false_func
        elif isinstance(interrupt, list) and len(interrupt) == 2:
            is_interrupt = interrupt[0] if callable(interrupt[0]) else false_func
            not_interrupt = interrupt[1] if callable(interrupt[1]) else false_func
        else:
            is_interrupt, not_interrupt = false_func, false_func
        finished_combat = 0
        died_timer = Timer(1.5, count=3)
        self.hp_reset()
        auto_search_time_limit_timer = Timer(self.config.OpsiGeneral_AutoSearchTimeLimit * 60, count=1).start()
        for _ in self.loop():
            # Условие завершения
            if not unlock_checked and unlock_check_timer.reached():
                logger.critical("[Операция «Сирена»] В текущей зоне не разблокирован автопоиск; сначала завершите сюжетное задание")
                raise RequestHumanTakeover
            if self.is_in_map():
                self.device.stuck_record_clear()
                if not success:
                    if died_timer.reached():
                        logger.warning("[Операция «Сирена» — бой] Гибель флота подтверждена")
                        break
                else:
                    if not interrupt_confirm and is_interrupt():
                        interrupt_confirm = True
                    if interrupt_confirm and not_interrupt():
                        interrupt_confirm = False
                    died_timer.reset()
            else:
                died_timer.reset()

            if not unlock_checked:
                if self.appear(AUTO_SEARCH_OS_MAP_OPTION_OFF, offset=(5, 120)):
                    unlock_checked = True
                elif self.appear(
                    AUTO_SEARCH_OS_MAP_OPTION_OFF_DISABLED, offset=(5, 120)
                ):
                    unlock_checked = True
                elif self.appear(AUTO_SEARCH_OS_MAP_OPTION_ON, offset=(5, 120)):
                    unlock_checked = True

            if self.handle_os_auto_search_map_option(drop=drop, enable=success):
                unlock_checked = True
                auto_search_time_limit_timer.reset()
                continue
            if self.handle_retirement():
                # Списание прерывает автопоиск, требуется повтор
                self.ash_popup_canceled = True
                auto_search_time_limit_timer.reset()
                continue
            if self.combat_appear():
                self.on_auto_search_battle_count_add()
                stop_event = self.config.stop_event
                if strategic and stop_event is not None and stop_event.is_set():
                    self.interrupt_auto_search()
                elif (
                    strategic
                    and not getattr(self.config, '_disable_task_switch', False)
                    and self.config.task_switched()
                ):
                    if self.config.task.command == "OpsiMeowfficerFarming":
                        logger.info("[Операция «Сирена» — поиск] Выполняется короткий поиск мяуфицеров; переключение задач отложено до его завершения")
                    else:
                        self.interrupt_auto_search()
                if interrupt_confirm:
                    self.interrupt_auto_search(goto_main=False)
                result = self.auto_search_combat(drop=drop)
                if result:
                    finished_combat += 1
                else:
                    self.hp_get()
                    if (
                        any(self.need_repair)
                        and not self.config.OpsiHazard1Leveling_SkipHpCheck
                    ):
                        success = False
                        logger.warning("[Операция «Сирена» — бой] Флот погиб; автопоиск остановлен")
                        auto_search_time_limit_timer.reset()
                        continue
                auto_search_time_limit_timer.reset()
            if self.handle_map_event():
                # Автопоиск не может обработать поисковое устройство сирен.
                auto_search_time_limit_timer.reset()
                continue
            if auto_search_time_limit_timer.reached():
                raise GameStuckError('自律寻敌卡死')

        return finished_combat

    def interrupt_auto_search(
        self, goto_main=True, end_task=True, skip_first_screenshot=True
    ):
        """
        中断自动搜索。

        Args:
            goto_main (bool): 是否跳转到主页面。

        Raises:
            TaskEnd: 自动搜索中断时抛出。

        Pages:
            in: 任意页面，通常为 is_combat_executing
            out: page_main 或 IN_MAP
        """
        logger.info("[Операция «Сирена» — поиск] Прерывание автопоиска")
        is_loading = False
        pause_interval = Timer(0.5, count=1)
        in_main_timer = Timer(3, count=6)
        in_map_timer = Timer(1, count=6)
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            # Условие завершения
            if self.is_in_main():
                logger.info("[Операция «Сирена» — поиск] Автоматический поиск был прерван")
                self.config.task_stop()
            if not goto_main and self.is_in_map() and in_map_timer.reached():
                logger.info("[Операция «Сирена» — поиск] Автоматический поиск был прерван")
                if end_task:
                    self.config.task_stop()
                return

            if self.appear_then_click(AUTO_SEARCH_REWARD, offset=(50, 50), interval=3):
                self.interval_clear(GOTO_MAIN)
                in_main_timer.reset()
                in_map_timer.reset()
                continue
            if pause_interval.reached() and (pause := self.is_combat_executing()):
                self.device.click(pause)
                self.interval_reset(MAINTENANCE_ANNOUNCE)
                is_loading = False
                pause_interval.reset()
                in_main_timer.reset()
                in_map_timer.reset()
                continue
            if self.handle_combat_quit():
                self.interval_reset(MAINTENANCE_ANNOUNCE)
                pause_interval.reset()
                in_main_timer.reset()
                in_map_timer.reset()
                continue
            if self.handle_combat_quit_reconfirm():
                self.interval_reset(MAINTENANCE_ANNOUNCE)
                pause_interval.reset()
                in_main_timer.reset()
                in_map_timer.reset()
                continue

            if goto_main and self.appear_then_click(
                GOTO_MAIN, offset=(20, 20), interval=3
            ):
                in_main_timer.reset()
                continue
            if self.ui_additional():
                continue
            if self.handle_map_event():
                continue
            # Вывод только один раз при обнаружении
            if not is_loading:
                if self.is_combat_loading():
                    is_loading = True
                    in_main_timer.clear()
                    in_map_timer.clear()
                    continue
                # Случайный фон page_main может вызвать EXP_INFO_*, не проверяем их
                if in_main_timer.reached():
                    logger.info("[Операция «Сирена» — информация] Обработка сведений об опыте")
                    if self.handle_battle_status():
                        continue
                    if self.handle_exp_info():
                        continue
            elif self.is_combat_executing():
                is_loading = False
                in_main_timer.clear()
                in_map_timer.clear()
                continue

    def os_auto_search_run(self, drop=None, strategic=False, interrupt=None):
        """
        Args:
            drop (DropRecord): 掉落记录对象。
            strategic (bool): 是否使用战略搜索。
            interrupt (callable): 中断回调函数。

        Returns:
            int: 完成的战斗次数。
        """
        finished_combat = 0
        for _ in range(5):
            backup = self.config.temporary(Campaign_UseAutoSearch=True)
            try:
                if strategic:
                    self.strategic_search_start(skip_first_screenshot=True)
                combat = self.os_auto_search_daemon(
                    drop=drop, strategic=strategic, interrupt=interrupt
                )
                finished_combat += combat
            except CampaignEnd:
                logger.info("[Операция «Сирена» — поиск] Автопоиск Операции «Сирена» завершён")
            finally:
                backup.recover()

            # Продолжаем, если автопоиск был прерван окном пепла
            # Выход после полной зачистки зоны
            if self.config.is_task_enabled("OpsiAshBeacon"):
                if self.handle_ash_beacon_attack() or self.ash_popup_canceled:
                    strategic = False
                    continue
                break
            if self.info_bar_count() >= 2:
                break
            if self.ash_popup_canceled:
                continue
            break

        return finished_combat

    @property
    def _is_siren_research_enabled(self):
        """
        检查配置中是否启用了塞壬研究功能。

        Returns:
            bool: 是否启用。
        """
        if getattr(self.config, "_disable_siren_research", False):
            return False
        task = self.config.task.command
        if task not in ("OpsiHazard1Leveling", "OpsiMeowfficerFarming"):
            task = "OpsiHazard1Leveling"
        return self.config.cross_get(
            keys=f"{task}.OpsiSirenBug.SirenResearch_Enable", default=True
        )

    def _should_skip_siren_research(self, grid):
        """
        根据配置检查是否应跳过塞壬研究装置。

        Args:
            grid: 要检查的格子。

        Returns:
            bool: 是否应跳过（功能已禁用时为 True）。
        """
        if hasattr(grid, "is_scanning_device") and grid.is_scanning_device:
            if not self._is_siren_research_enabled:
                logger.info(f"[Операция «Сирена»] [Предварительная проверка] Клетка {grid} является исследовательским устройством Сирен, но функция отключена; пропуск")
                return True
            logger.info(f"[Операция «Сирена»] [Предварительная проверка] Клетка {grid} является исследовательским устройством Сирен; функция включена, обработка продолжается")
        return False

    def clear_question(self, drop=None):
        """
        清理雷达上近距离（以及上方 3 格内）的问号。
        最多尝试 3 次，避免在双舰队机关上循环尝试。

        Args:
            drop: 掉落记录对象。

        Returns:
            bool: 是否清理了问号。
        """
        logger.hr("Удаление вопросительного знака", level=2)
        for _ in range(3):
            grid = self.radar.predict_question(
                self.device.image, in_port=self.zone.is_port
            )
            if grid is None:
                logger.info("[Операция «Сирена» — поиск] На радаре над текущим флотом нет вопросительного знака")
                return False

            logger.info(f"[Операция «Сирена» — поиск] В клетке {grid} найден вопросительный знак")
            self.handle_info_bar()

            self.update_os()
            self.view.predict()
            self.view.show()

            grid = self.convert_radar_to_local(grid)

            # ========== Проверка перед перемещением: устройство исследований сирен и функция отключена ==========
            if self._should_skip_siren_research(grid):
                record_siren_research_device(self)
                self._solved_map_event.add("is_scanning_device")
                return True

            self.is_siren_device_confirmed = False
            self.device.click(grid)
            with self.config.temporary(
                STORY_ALLOW_SKIP=False, OS_SIREN_DEVICE_USAGE="use_until_destroyed"
            ):
                result = self.wait_until_walk_stable(
                    drop=drop, walk_out_of_step=False, confirm_timer=Timer(3, count=4)
                )
            if "akashi" in result:
                self._solved_map_event.add("is_akashi")
                return True
            elif "event" in result and grid.is_logging_tower:
                self._solved_map_event.add("is_logging_tower")
                return True
            elif "event" in result and (
                grid.is_scanning_device or self.is_siren_device_confirmed
            ):
                # ========== Детекция карты: обнаружено сканирующее устройство ==========
                logger.hr("[Операция «Сирена»] Обнаружено сканирующее устройство; начало обработки", level=2)
                logger.info(
                    f"[Распознавание карты] Клетка {grid} распознана как сканирующее устройство (grid.is_scanning_device=True)"
                )
                logger.info(f"[Операция «Сирена»] [Распознавание карты] Результат перемещения: {result}")
                record_siren_research_device(self)

                # ========== Проверка конфигурации ==========
                if not self._is_siren_research_enabled:
                    logger.warning("[Операция «Сирена»] [Проверка конфигурации] Исследовательские устройства Сирен отключены; устройство отмечено без обработки")
                    self._solved_map_event.add("is_scanning_device")
                    return True

                # ========== Обработка устройства ==========
                # Клик по опциям уже обработан цепочкой wait_until_walk_stable -> info_handler.story_skip

                # Определение выбранного режима
                siren_mode = getattr(self, "siren_device_mode", None)
                logger.attr("Режим устройства Сирен", siren_mode)

                # Если выбран режим врагов
                if siren_mode == "enemy":
                    logger.info("[Операция «Сирена»] [Обработка устройства] Обнаружен режим врага; выполняется специальная обработка")

                    # Получение настроенного флота
                    task = self.config.task.command
                    if task not in ("OpsiHazard1Leveling", "OpsiMeowfficerFarming"):
                        task = "OpsiHazard1Leveling"
                    siren_fleet = self.config.cross_get(
                        keys=f"{task}.OpsiSirenBug.Siren_Fleet", default=0
                    )

                    # Фиксация текущего флота
                    current_fleet = self.fleet_selector.get()
                    logger.info(f"[Операция «Сирена»] [Обработка устройства] Текущий флот: {current_fleet}")

                    # Если задан конкретный флот, переключаемся на него
                    if siren_fleet > 0:
                        logger.info(f"[Операция «Сирена»] [Обработка устройства] Переключение на назначенный флот: {siren_fleet}")
                        self.fleet_set(siren_fleet)
                    else:
                        logger.info("[Операция «Сирена»] [Обработка устройства] Использование текущего флота")

                    # Выполняем три автопоиска врагов
                    for i in range(3):
                        logger.info(f"[Операция «Сирена»] [Обработка устройства] Автопоиск врагов, попытка {i + 1}/3")
                        self.os_auto_search_run(drop=drop)

                    # Если переключали флот, возвращаемся к исходному
                    if siren_fleet > 0:
                        logger.info(f"[Операция «Сирена»] [Обработка устройства] Возвращение к исходному флоту: {current_fleet}")
                        self.fleet_set(current_fleet)

                # Если выбран режим ресурсов
                elif siren_mode == "resource":
                    logger.info("[Операция «Сирена»] [Обработка устройства] Обнаружен режим ресурсов; выполняется стандартная обработка")
                    # Выполняем один автопоиск врагов
                    logger.info("[Операция «Сирена»] [Обработка устройства] Запуск автопоиска врагов")
                    self.os_auto_search_run(drop=drop)

                # Неизвестный режим или недостаточно ресурсов
                else:
                    logger.info("[Операция «Сирена»] [Обработка устройства] Неизвестный режим или недостаточно ресурсов; выполняется стандартная обработка")
                    # Выполняем один автопоиск врагов
                    logger.info("[Операция «Сирена»] [Обработка устройства] Запуск автопоиска врагов")
                    self.os_auto_search_run(drop=drop)

                # Отметка об обработке
                self._solved_map_event.add("is_scanning_device")

                return True

        logger.warning(
            "[Операция «Сирена» — карта] Не удалось перейти к вопросительному знаку за 5 попыток; "
            "возможно, рядом находятся два механизма флота; остановка"
        )
        return False

    def run_auto_search(
        self, question=True, rescan=None, after_auto_search=True, interrupt=None
    ):
        """
        通过运行自律寻敌清理当前海域。需要先完成大世界剧情模式才能解锁自律寻敌。

        Args:
            question (bool): 自动搜索后是否清理近距离问号。
            rescan (bool, str): 运行自动搜索后是否重扫整个地图。
                这会清理塞壬扫描装置、塞壬日志塔、
                访问自动搜索遗漏的明石商店，以及解锁需要 2 支舰队的机关。
                也接受字符串：`current` 仅扫描当前摄像机视野，`full` 先扫描当前再重扫整个地图。
                在 OpsiObscure、OpsiAbyssal、OpsiStronghold 等特殊任务中应禁用此选项。
            after_auto_search (bool): 自动搜索后是否调用 handle_after_auto_search()。
            interrupt (callable): 中断回调函数。

        Returns:
            int: 完成的战斗次数。
        """
        if rescan is None:
            rescan = self.config.OpsiGeneral_DoRandomMapEvent
        if rescan is True:
            rescan = "full"
        self.handle_ash_beacon_attack()

        logger.info(f"[Операция «Сирена» — поиск] Запуск автопоиска, вопросительный знак={question}, повторное сканирование={rescan}")
        finished_combat = 0
        with self.stat.new(
            genre=inflection.underscore(self.config.task.command),
            method=self.config.DropRecord_OpsiRecord,
        ) as drop:
            while 1:
                combat = self.os_auto_search_run(drop, interrupt=interrupt)
                finished_combat += combat

                drop.add(self.device.image)

                self.hp_reset()
                self.hp_get()
                if (
                    after_auto_search
                    and self.is_in_task_explore
                    and not self.zone.is_port
                ):
                    prev = self.zone
                    if self.handle_after_auto_search():
                        self.globe_goto(prev, types="DANGEROUS")
                        continue
                break

            drop.set_combat_count(self._auto_search_battle_count)

            # Повторное сканирование должно выполняться в контексте drop. Некоторые награды OS
            # появляются только при зачистке знаков вопроса или повторном сканировании карты.
            self._solved_map_event = set()
            self._solved_fleet_mechanism = False
            if question:
                self.clear_question(drop=drop)
            if rescan:
                self.map_rescan(rescan_mode=rescan, drop=drop)

            if drop.count <= 1:
                drop.clear()

        return finished_combat

    _solved_map_event = set()
    _solved_fleet_mechanism = 0

    def run_strategic_search(self):
        """
        Returns:
            bool: 正常完成返回 True，被中断返回 False（非 TaskEnd）。
        """
        self.handle_ash_beacon_attack()

        logger.hr("Стратегический поиск", level=2)

        with self.stat.new(
            genre=inflection.underscore(self.config.task.command),
            method=self.config.DropRecord_OpsiRecord,
        ) as drop:
            try:
                combat = self.os_auto_search_run(drop, strategic=True)
                drop.set_combat_count(combat)
                drop.add(self.device.image)
                self.hp_reset()
                self.hp_get()
                return True
            except (
                TaskEnd,
                GameStuckError,
                GameTooManyClickError,
                RequestHumanTakeover,
            ):
                # Переключение задач и восстановимые исключения должны передаваться верхнему планировщику.
                raise
            except Exception as e:
                logger.warning(f"[Операция «Сирена» — поиск] Стратегический поиск прерван: {e}")
                return False
            finally:
                if drop.count <= 1:
                    drop.clear()

                drop.set_combat_count(self._auto_search_battle_count)

    def map_rescan_current(self, drop=None, clicked_grids=None):
        """
        Args:
            drop: 掉落记录对象。

        Returns:
            bool: 是否解决了地图随机事件。
        """
        grids = self.view.select(is_exploration_reward=True)
        if (
            "is_exploration_reward" not in self._solved_map_event
            and grids
            and grids[0].is_exploration_reward
        ):
            grid = grids[0]
            logger.info(f"[Операция «Сирена» — поиск] В клетке {grid} найдена награда за исследование")
            result = self.wait_until_walk_stable(
                drop=drop, walk_out_of_step=False, confirm_timer=Timer(1.5, count=4)
            )
            if "event" in result:
                self._solved_map_event.add("is_exploration_reward")
                return True
            return False

        grids = self.view.select(is_akashi=True)
        if "is_akashi" not in self._solved_map_event and grids and grids[0].is_akashi:
            grid = grids[0]
            logger.info(f"[Операция «Сирена» — поиск] В клетке {grid} найдена Акаси")
            fleet = self.convert_radar_to_local((0, 0))
            if fleet.distance_to(grid) > 1:
                self.device.click(grid)
                with self.config.temporary(STORY_ALLOW_SKIP=False):
                    walk_time = 1.5 + 0.6 * grid.distance_to(fleet)
                    result = self.wait_until_walk_stable(
                        confirm_timer=Timer(walk_time, count=4),
                        drop=drop,
                        walk_out_of_step=False,
                    )
                if "akashi" in result:
                    self._solved_map_event.add("is_akashi")
                    return True
                else:
                    grids = self.view.select(is_akashi=True)
                    if "is_akashi" not in self._solved_map_event and grids and grids[0].is_akashi:
                        grid = grids[0]
                        fleet = self.convert_radar_to_local((0, 0))
                        if fleet.distance_to(grid) <= 1:
                            logger.info(f"[Операция «Сирена» — поиск] Акаси ({grid}) находится рядом с текущим флотом ({fleet})")
                            self.handle_akashi_supply_buy(grid)
                            self._solved_map_event.add("is_akashi")
                            return True
                        else:
                            logger.info("[Операция «Сирена»] Позиция Акаси недоступна; выполняется принудительное перемещение")
                            self._execute_fixed_patrol_scan(ExecuteFixedPatrolScan=True)
                            return False
                    else:
                        logger.info("[Операция «Сирена» — событие] Событий на карте нет")
                        return False
            else:
                logger.info(f"[Операция «Сирена» — поиск] Акаси ({grid}) находится рядом с текущим флотом ({fleet})")
                self.handle_akashi_supply_buy(grid)
                self._solved_map_event.add("is_akashi")
                return True

        grids = self.view.select(is_scanning_device=True)
        if (
            "is_scanning_device" not in self._solved_map_event
            and grids
            and grids[0].is_scanning_device
        ):
            grid = grids[0]

            # ========== Выбор карты: обнаружено исследовательское устройство ==========
            logger.hr("[Операция «Сирена»] Обнаружено исследовательское устройство; начало обработки", level=2)
            logger.info(f"[Операция «Сирена»] [Выбор на карте] Исследовательское устройство найдено в клетке {grid}.")
            record_siren_research_device(self)

            if not self._is_siren_research_enabled:
                logger.warning("[Операция «Сирена»] [Проверка конфигурации] Исследовательские устройства Сирен отключены; обработка пропущена")
                self._solved_map_event.add("is_scanning_device")
                return True

            # ========== Перемещение и обработка ==========
            logger.info(f"[Операция «Сирена»] [Переход к устройству] Начало перемещения в клетку устройства: {grid}")
            self.device.click(grid)

            # Сброс флагов
            self.is_siren_device_confirmed = False

            # wait_until_walk_stable вызовет handle_story_skip для обработки вариантов
            logger.info("[Операция «Сирена»] [Переход к устройству] Ожидание стабилизации перемещения...")
            with self.config.temporary(
                STORY_ALLOW_SKIP=False, OS_SIREN_DEVICE_USAGE="use_until_destroyed"
            ):
                result = self.wait_until_walk_stable(
                    drop=drop, walk_out_of_step=False, confirm_timer=Timer(3, count=4)
                )
            logger.info(f"[Операция «Сирена»] [Переход к устройству] Перемещение завершено, результат: {result}")

            if getattr(self, "is_siren_device_confirmed", False):
                # Определение выбранного режима
                siren_mode = getattr(self, "siren_device_mode", None)
                logger.attr("Режим устройства Сирен", siren_mode)

                # Если выбран режим врагов
                if siren_mode == "enemy":
                    logger.info("[Операция «Сирена»] [Обработка устройства] Режим врага, выполняется специальная обработка")

                    # Получение настроенного флота
                    task = self.config.task.command
                    if task not in ("OpsiHazard1Leveling", "OpsiMeowfficerFarming"):
                        task = "OpsiHazard1Leveling"
                    siren_fleet = self.config.cross_get(
                        keys=f"{task}.OpsiSirenBug.Siren_Fleet", default=0
                    )

                    # Фиксация текущего флота
                    current_fleet = self.fleet_selector.get()
                    logger.info(f"[Операция «Сирена»] [Обработка устройства] Текущий флот: {current_fleet}")

                    # Если задан конкретный флот, переключаемся на него
                    if siren_fleet > 0:
                        logger.info(f"[Операция «Сирена»] [Обработка устройства] Переключение на назначенный флот: {siren_fleet}")
                        self.fleet_set(siren_fleet)
                    else:
                        logger.info("[Операция «Сирена»] [Обработка устройства] Использование текущего флота")

                    # Выполняем три автопоиска врагов
                    for i in range(3):
                        logger.info(f"[Операция «Сирена»] [Обработка устройства] Автопоиск врагов, попытка {i + 1}/3")
                        self.os_auto_search_run(drop=drop)

                    # Если переключали флот, возвращаемся к исходному
                    if siren_fleet > 0:
                        logger.info(f"[Операция «Сирена»] [Обработка устройства] Возврат к исходному флоту: {current_fleet}")
                        self.fleet_set(current_fleet)

                # Если выбран режим ресурсов
                elif siren_mode == "resource":
                    logger.info("[Операция «Сирена»] [Обработка устройства] Обнаружен режим ресурсов; выполняется стандартная обработка")
                    # Выполняем один автопоиск врагов
                    logger.info("[Операция «Сирена»] [Обработка устройства] Запуск автопоиска врагов")
                    self.os_auto_search_run(drop=drop)

                # Неизвестный режим или недостаточно ресурсов
                else:
                    logger.info("[Операция «Сирена»] [Обработка устройства] Неизвестный режим или недостаточно ресурсов, выполнение стандартной обработки")
                    # Выполняем один автопоиск врагов
                    logger.info("[Операция «Сирена»] [Обработка устройства] Запуск автопоиска врагов")
                    self.os_auto_search_run(drop=drop)

                # Сначала помечаем как обработанное, чтобы избежать повторной обработки при втором сканировании
                self._solved_map_event.add("is_scanning_device")

                # Второе сканирование для предотвращения сбоя обработки из-за непредвиденных обстоятельств
                logger.info("[Операция «Сирена»] [Обработка устройства] Выполняется повторное сканирование")
                self.map_rescan_current(drop=drop)

            return True

        grids = self.view.select(is_logging_tower=True)
        if (
            "is_logging_tower" not in self._solved_map_event
            and grids
            and grids[0].is_logging_tower
        ):
            grid = grids[0]
            logger.info(f"[Операция «Сирена» — поиск] В клетке {grid} найдена башня записей")
            self.device.click(grid)
            with self.config.temporary(STORY_ALLOW_SKIP=False):
                result = self.wait_until_walk_stable(
                    drop=drop, walk_out_of_step=False, confirm_timer=Timer(3, count=4)
                )
            if "event" in result:
                self._solved_map_event.add("is_logging_tower")
                return True
            return False

        grids = self.view.select(is_fleet_mechanism=True)
        if (
            self.is_in_task_explore
            and "is_fleet_mechanism" not in self._solved_map_event
            and grids
            and grids[0].is_fleet_mechanism
        ):
            grid = grids[0]
            logger.info(f"[Операция «Сирена» — поиск] В клетке {grid} найден механизм флота")
            self.device.click(grid)
            self.wait_until_walk_stable(
                drop=drop, walk_out_of_step=False, confirm_timer=Timer(1.5, count=4)
            )

            if self._solved_fleet_mechanism:
                logger.info("[Операция «Сирена» — поиск] Все механизмы флота активированы")
                self.os_auto_search_run(drop=drop)
                self._solved_map_event.add("is_fleet_mechanism")
                return True
            logger.info("[Операция «Сирена» — поиск] Один механизм флота активирован")
            self._solved_fleet_mechanism = True
            return True

        logger.info("[Операция «Сирена» — событие] Событий на карте нет")
        return False

    def map_rescan_once(self, rescan_mode="full", drop=None):
        """
        Args:
            rescan_mode (str): `current` 仅扫描当前摄像机视野，`full` 先扫描当前再重扫整个地图。
            drop: 掉落记录对象。

        Returns:
            bool: 是否解决了地图随机事件。
        """
        result = False

        # Сначала пробуем текущую камеру
        logger.hr("Повторное сканирование текущей карты", level=2)
        self.map_data_init(map_=None)
        self.handle_info_bar()
        try:
            self.update()
        except MapDetectionError:
            # Карта может быть уже зачищена: гомография не находит валидных клеток
            logger.warning(
                "[Операция «Сирена» — сканирование] При повторном сканировании текущей карты не удалось построить гомографию (оценка ниже 0.8); "
                "карта могла быть очищена или распознана нестабильно, поэтому необработанные события могли быть пропущены"
            )
            return False
        if self.map_rescan_current(drop=drop):
            logger.info("[Операция «Сирена» — сканирование] Один проход повторного сканирования завершён, результат=True")
            return True

        if rescan_mode == "full":
            logger.hr("Полное повторное сканирование карты", level=2)
            self.map_init(map_=None)
            queue = self.map.camera_data
            while len(queue) > 0:
                logger.hr(f"Повторное сканирование {queue[0]}")
                queue = queue.sort_by_camera_distance(self.camera)
                self.focus_to(queue[0], swipe_limit=(6, 5))
                self.focus_to_grid_center(0.3)

                if self.map_rescan_current(drop=drop):
                    result = True
                    break
                queue = queue[1:]

        logger.info(f"[Операция «Сирена» — сканирование] Один проход повторного сканирования завершён, результат={result}")
        return result

    def map_rescan(self, rescan_mode="full", drop=None):
        if self.zone.is_port:
            logger.info("[Операция «Сирена» — сканирование] Текущая зона является портом; повторное сканирование не требуется")
            return False

        for _ in range(5):
            if not self._solved_fleet_mechanism:
                self.fleet_set(self.config.OpsiFleet_Fleet)
            else:
                self.fleet_set(self.get_second_fleet())
            if not self.is_in_task_explore and len(self._solved_map_event):
                logger.info("[Операция «Сирена» — сканирование] Событие карты обработано вне исследования Операции «Сирена»; повторное сканирование остановлено")
                logger.attr("Обработанные события карты", self._solved_map_event)
                self.fleet_set(self.config.OpsiFleet_Fleet)
                return False
            result = self.map_rescan_once(rescan_mode=rescan_mode, drop=drop)
            if not result:
                logger.attr("Обработанные события карты", self._solved_map_event)
                self.fleet_set(self.config.OpsiFleet_Fleet)
                return True

        logger.attr("Обработанные события карты", self._solved_map_event)
        logger.warning("[Операция «Сирена» — сканирование] Слишком много попыток повторного сканирования карты; остановка")
        self.fleet_set(self.config.OpsiFleet_Fleet)
        return False

    def safe_swipe(self, start, end, duration=0.5, retries=2):
        """执行带重试的安全滑动。

        在多次滑动场景中，先尝试清理设备卡住记录，再执行滑动，
        通过重试提升滑动成功率。

        Args:
            start (tuple[int, int]): 滑动起点坐标。
            end (tuple[int, int]): 滑动终点坐标。
            duration (float, optional): 单次滑动时长（秒）。默认值为 0.5。
            retries (int, optional): 最大重试次数。默认值为 2。

        Returns:
            bool: 任一重试成功返回 True；全部失败返回 False。
        """
        for attempt in range(1, retries + 1):
            try:
                with suppress(Exception):
                    self.device.stuck_record_clear()
                self.device.swipe(start, end, duration=duration)
                time.sleep(0.45)
                return True
            except Exception as e:
                logger.warning(f"[Операция «Сирена»] Неудачная попытка безопасного свайпа {attempt}: {e}")
                time.sleep(0.4)
                continue
        return False

    def _get_fixed_patrol_candidate_grids(self, target_loc, occupied_locations=None):
        """为强制移动生成候选落点，主目标失败后尝试移动到附近空位。"""
        occupied = set(occupied_locations or [])
        offsets = [
            (0, 0),
            (0, 1),
            (0, 2),
            (-1, 1),
            (1, 1),
            (-1, 2),
            (1, 2),
            (-1, 0),
            (1, 0),
            (0, 3),
        ]
        absolute_fallback_rows = (11, 12)  # Соответствует 12 и 13 строкам отображения карты
        candidates = []
        seen = set()
        for dx, dy in offsets:
            loc = (target_loc[0] + dx, target_loc[1] + dy)
            if loc in seen or loc not in self.map or loc in occupied:
                continue
            seen.add(loc)
            grid = self.map[loc]
            if (
                grid.is_land
                or grid.is_enemy
                or grid.is_siren
                or grid.is_boss
                or grid.is_fortress
            ):
                continue
            if getattr(grid, "is_mechanism_block", False) or getattr(
                grid, "is_fleet", False
            ):
                continue
            candidates.append(grid)

        for row in absolute_fallback_rows:
            loc = (target_loc[0], row)
            if loc in seen or loc not in self.map or loc in occupied:
                continue
            seen.add(loc)
            grid = self.map[loc]
            if (
                grid.is_land
                or grid.is_enemy
                or grid.is_siren
                or grid.is_boss
                or grid.is_fortress
            ):
                continue
            if getattr(grid, "is_mechanism_block", False) or getattr(
                grid, "is_fleet", False
            ):
                continue
            candidates.append(grid)
        return candidates

    def _try_fixed_patrol_move(self, fleet_index, target_grid, primary_target):
        """尝试将指定舰队移动到候选落点。"""
        self.focus_to(target_grid.location)
        self.update()
        try:
            clickable_grid = self.convert_global_to_local(target_grid.location)
        except KeyError:
            logger.warning(
                f"Камера перемещена к {target_grid.location}, но в поле зрения нет клетки для клика."
            )
            return False

        for try_idx in range(2):
            try:
                with suppress(Exception):
                    self.device.stuck_record_clear()
                time.sleep(0.1)
                self.device.click(clickable_grid)
                self.wait_until_walk_stable(confirm_timer=Timer(1.5, count=4))
                if target_grid.location == primary_target:
                    logger.info(f"[Операция «Сирена»] Флот {fleet_index} прибыл в {target_grid}.")
                else:
                    logger.info(
                        f"Флот {fleet_index} не достиг основной цели {self.map[primary_target]}; остановка перенесена в резервную точку {target_grid}."
                    )
                return True
            except (MapWalkError, GameTooManyClickError) as e:
                if isinstance(e, MapWalkError) and str(e) == "walk_out_of_step":
                    logger.warning(
                        f"Флот {fleet_index} не может достичь цели {target_grid}; текущая точка отброшена, проверяются другие"
                    )
                    return False
                logger.warning(f"[Операция «Сирена»] Ошибка перемещения флота: {e}; принудительное восстановление ({try_idx + 1}/2)")
                recovered = False
                try:
                    recovered = self._force_move_recover(
                        target_zone=self.zone or None
                    )
                except Exception:
                    recovered = False
                if recovered:
                    time.sleep(0.5)
                    self.focus_to(target_grid.location)
                    self.update()
                    try:
                        clickable_grid = self.convert_global_to_local(
                            target_grid.location
                        )
                    except KeyError:
                        clickable_grid = None
                    if clickable_grid:
                        continue
                logger.warning("[Операция «Сирена»] Попытка мягкого восстановления (back / screenshot / rebuild view)")
                try:
                    for _ in range(3):
                        with suppress(Exception):
                            self.device.back()
                    self.device.screenshot()
                    try:
                        self.ui_ensure(page_os)
                        self.map_init(map_=None)
                        self.update()
                    except Exception:
                        logger.debug("[Operation Siren] Не удалось перестроить обзор при мягком восстановлении", exc_info=True)
                    try:
                        clickable_grid = self.convert_global_to_local(
                            target_grid.location
                        )
                    except KeyError:
                        clickable_grid = None
                    if clickable_grid:
                        logger.info("[Операция «Сирена»] После мягкого восстановления найдена клетка; повторный клик")
                        try:
                            time.sleep(0.3)
                            self.device.click(clickable_grid)
                            self.wait_until_walk_stable(
                                confirm_timer=Timer(1.5, count=4)
                            )
                            logger.info("[Операция «Сирена»] Мягкое восстановление успешно; флот прибыл")
                            return True
                        except Exception:
                            logger.debug("[Operation Siren] Не удалось повторить нажатие при мягком восстановлении", exc_info=True)
                except Exception as rec_e:
                    logger.debug(f"[Operation Siren] Исключение при мягком восстановлении: {rec_e}")
                if try_idx == 1:
                    logger.warning("[Операция «Сирена»] Мягкое восстановление не удалось; попытка восстановить состояние перезапуском приложения")
                    try:
                        self.device.app_stop()
                        time.sleep(1.0)
                        self.device.app_start()
                        LoginHandler(self.config, self.device).handle_app_login()
                        self.ui_ensure(page_os)
                        time.sleep(0.8)
                        try:
                            self.map_init(map_=None)
                            self.update()
                        except Exception:
                            logger.debug(
                                "Не удалось перестроить данные карты после перезапуска приложения", exc_info=True
                            )
                        try:
                            clickable_grid = self.convert_global_to_local(
                                target_grid.location
                            )
                        except KeyError:
                            clickable_grid = None
                        if clickable_grid:
                            time.sleep(0.3)
                            self.device.click(clickable_grid)
                            self.wait_until_walk_stable(
                                confirm_timer=Timer(1.5, count=4)
                            )
                            logger.info("[Операция «Сирена»] Состояние восстановлено после перезапуска приложения; флот прибыл")
                            return True
                    except Exception:
                        logger.error(
                            "Не удалось восстановить состояние перезапуском приложения; перемещение к текущей точке не выполнено", exc_info=True
                        )
                time.sleep(0.5)

        return False

    # Индивидуальная модификация на основе предельного Corrosion 1 от ShaddockNH3
    def _execute_fixed_patrol_scan(
        self, ExecuteFixedPatrolScan: bool = False, **kwargs
    ):
        """执行强制移动并触发全图重扫。

        在每支舰队移动前执行视角复位，按预设坐标依次移动 1~4 号舰队，
        全部移动后执行全图重扫，并补一次自律寻敌以清理残留装置。

        Args:
            ExecuteFixedPatrolScan (bool, optional): 是否启用强制移动。
                为 False 时直接跳过。默认值为 False。
            **kwargs: 预留参数，当前未使用。

        Returns:
            None
        """
        logger.hr("[Операция «Сирена»] Принудительное перемещение")

        if not ExecuteFixedPatrolScan:
            logger.info("[Операция «Сирена»] ExecuteFixedPatrolScan отключён; принудительное перемещение пропущено")
            return
        logger.attr("Выполнение фиксированного патрульного сканирования", True)

        self.map_init(map_=None)
        if not hasattr(self, "map") or not self.map.grids:
            logger.warning("[Операция «Сирена»] Данные клеток текущей карты недоступны; принудительное перемещение пропущено")
            return

        solved = getattr(self, "_solved_map_event", set())
        if any(
            k in solved for k in ("is_akashi", "is_scanning_device", "is_logging_tower")
        ):
            logger.info("[Операция «Сирена»] Пасхалка: госпожа Юкикадзэ благословляет вас; перемещение флота пропущено")
            return

        patrol_locations = [(2, 0), (3, 0), (4, 0), (5, 0)]  # Соответствует C1, D1, E1, F1
        progress = {}

        for i, target_loc in enumerate(patrol_locations):
            fleet_index = i + 1
            if fleet_index in progress:
                logger.info(
                    f"Флот {fleet_index} уже остановился в точке {self.map[progress[fleet_index]]} в этом цикле принудительного перемещения; повтор пропущен."
                )
                continue

            target_grid_group = self.map.select(location=target_loc)
            if not target_grid_group:
                logger.warning(
                    f"На карте не найдена клетка с координатами {target_loc}; перемещение флота {fleet_index} пропущено."
                )
                continue
            target_grid = target_grid_group[0]
            occupied_locations = set(progress.values())
            candidate_grids = self._get_fixed_patrol_candidate_grids(
                target_loc, occupied_locations=occupied_locations
            )
            if not candidate_grids:
                logger.warning(
                    f"Для флота {fleet_index} рядом с {target_grid} нет доступной точки остановки; перемещение пропущено."
                )
                continue

            logger.hr(f"[Операция «Сирена»] Принудительное перемещение: флот {fleet_index} к {target_grid}", level=2)

            self.fleet_set(fleet_index)

            logger.info("[Операция «Сирена»] Сброс положения камеры...")

            top_point = (640, 150)
            bottom_point = (640, 600)
            quick_ok = True
            try:
                for _ in range(2):
                    self.device.swipe(top_point, bottom_point, duration=0.3)
                    time.sleep(0.18)
            except Exception:
                quick_ok = False
                logger.debug("[Operation Siren] Исключение при быстром сбросе прокрутки; выполняется безопасная прокрутка")

            if not quick_ok and not self.safe_swipe(
                top_point, bottom_point, duration=0.55, retries=2
            ):
                logger.warning("[Операция «Сирена»] Не удалось сбросить положение камеры; переход к следующему шагу")
            elif not quick_ok:
                logger.info("[Операция «Сирена»] Положение камеры сброшено")
            else:
                logger.info("[Операция «Сирена»] Быстрый свайп для сброса камеры завершён")
            time.sleep(0.45)

            moved = False
            fallback_location = None
            for candidate_index, candidate_grid in enumerate(candidate_grids[:4]):
                if candidate_index > 0:
                    logger.info(
                        f"Флот {fleet_index} использует резервную точку {candidate_grid} (исходная цель {target_grid})"
                    )
                if self._try_fixed_patrol_move(fleet_index, candidate_grid, target_loc):
                    if candidate_grid.location == target_loc:
                        progress[fleet_index] = candidate_grid.location
                        moved = True
                        break

                    fallback_location = candidate_grid.location
                    logger.info(
                        f"Флот {fleet_index} остановился в резервной точке {candidate_grid}; попытка вернуться к настоящей цели {target_grid}"
                    )
                    if self._try_fixed_patrol_move(
                        fleet_index, target_grid, target_loc
                    ):
                        progress[fleet_index] = target_loc
                        moved = True
                        logger.info(
                            f"Флот {fleet_index} вернулся из резервной точки к настоящей цели {target_grid}"
                        )
                        break

                    logger.warning(
                        f"Флот {fleet_index} не смог вернуться из резервной точки {candidate_grid} к настоящей цели {target_grid}; проверяются другие точки"
                    )
            if not moved:
                if fallback_location is not None:
                    progress[fleet_index] = fallback_location
                    logger.warning(
                        f"Флот {fleet_index} не смог вернуться к настоящей цели {target_grid}; временная остановка в резервной точке {self.map[fallback_location]}."
                    )
                else:
                    logger.warning(
                        f"Флот {fleet_index} не смог переместиться ни к {target_grid}, ни к резервным точкам; выполнение продолжается."
                    )

        backup = self.config.temporary(
            OpsiGeneral_RepairThreshold=-1, Campaign_UseAutoSearch=False
        )
        try:
            logger.info("[Операция «Сирена»] Все флоты размещены; выполняется итоговое полное сканирование карты в два прохода")
            self._solved_map_event = set()
            for _ in range(2):
                try:
                    self.map_rescan(rescan_mode="full")
                except (
                    TaskEnd,
                    GameStuckError,
                    GameTooManyClickError,
                    RequestHumanTakeover,
                ):
                    raise
                except Exception as e:
                    logger.debug(f"[Operation Siren] Исключение при итоговом полном сканировании карты; повтор: {e}", exc_info=True)
                    time.sleep(0.6)
        finally:
            backup.recover()

        logger.info("[Операция «Сирена»] Запуск одного автопоиска врагов для очистки возможного устройства")
        try:
            self.run_auto_search(question=True, rescan=None, after_auto_search=True)
        except (
            TaskEnd,
            GameStuckError,
            GameTooManyClickError,
            RequestHumanTakeover,
        ):
            raise
        except Exception as e:
            logger.warning(f"[Операция «Сирена»] Ошибка во время автопоиска врагов: {e}")

    def _select_story_option_by_index(self, target_index, options_count=3):
        """按索引点击剧情选项按钮。

        在限定时间内识别剧情选项并尝试点击目标索引；当目标索引越界时，
        回退点击第一个选项。

        Args:
            target_index (int): 目标选项索引（从 0 开始）。
            options_count (int, optional): 期望识别到的选项数量。默认值为 3。

        Returns:
            bool: 点击目标索引成功返回 True；回退点击或超时返回 False。
        """
        option_confirm_timer = Timer(1.5, count=3).start()
        while option_confirm_timer.reached() is False:
            self.device.screenshot()
            # Распознавание всех вариантов
            options = self._story_option_buttons_2()
            if len(options) == options_count:
                try:
                    select = options[target_index]
                    self.device.click(select)
                    time.sleep(0.5)
                    return True
                except IndexError:
                    select = options[0]
                    self.device.click(select)
                    time.sleep(0.5)
                    return False
            time.sleep(0.3)
        return False

    def _click_story_confirm_button(self):
        """点击剧情确认按钮。

        在限定时间内轮询确认弹窗，出现后点击确认。

        Returns:
            bool: 成功点击确认返回 True；超时未出现返回 False。
        """
        confirm_timer = Timer(3, count=6).start()
        while confirm_timer.reached() is False:
            self.device.screenshot()
            if self.appear(POPUP_CONFIRM, offset=(20, 20), interval=0):
                self.device.click(POPUP_CONFIRM)
                time.sleep(0.5)
                return True
            time.sleep(0.3)
        return False
