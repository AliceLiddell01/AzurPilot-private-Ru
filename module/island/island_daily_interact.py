"""岛屿每日交互模块。

管理岛屿每日的 NPC 交互任务，包括阿卡西、奥古斯特、斯蒂芬波特、
伊丽莎白女王等角色的好感度互动，以及不同地点的地图导航与交互流程。
"""
from module.island.island import *
from module.logger import logger
from module.island_daily_interact.assets import *
from datetime import timedelta
from module.config.time_source import now as current_time
from module.island.island_detection import red, yellow


class IslandDailyInteract(Island):
    def _asset_matches_server(self, button):
        """
        判断资产是否来自当前服务器。

        统一使用 Button 当前加载的 ``file`` 路径，不再维护业务侧资产路径映射。
        ``Button.file`` 在 :mod:`module.config.server` 中会按当前服务器自动选取，
        因此这里只需验证该路径的目录片段即可。
        """
        current_server = str(getattr(self.config, 'SERVER', '') or '').lower()
        file_path = str(getattr(button, 'file', '') or '').replace('\\', '/').lower()
        marker = '/assets/'
        if marker not in file_path:
            return True
        asset_root = file_path.split(marker, 1)[0].rstrip('/')
        asset_server = asset_root.rsplit('/', 1)[-1]
        if asset_server not in {'cn', 'en', 'jp', 'tw'}:
            return True
        return asset_server == current_server

    @staticmethod
    def _ensure_model_asset(button, name):
        file_path = str(getattr(button, 'file', '') or '')
        if not file_path:
            raise FileNotFoundError(f'У {name} отсутствует путь к ресурсу')
        if not os.path.exists(file_path):
            raise FileNotFoundError(f'Не найден ресурс {name}: {file_path}')

    def _ensure_interact_assets(self, button, name):
        if not self._asset_matches_server(button):
            raise FileNotFoundError(
                f'Ресурс {name} не соответствует текущему серверу {self.config.SERVER}: {button.file}'
            )
        self._ensure_model_asset(button, name)

    def _interact_model_available(self, button, name):
        try:
            self._ensure_interact_assets(button, name)
            return True
        except FileNotFoundError as exc:
            logger.warning(str(exc))
            return False

    def goto_npc(self, target):
        """导航到指定 NPC。"""
        self.ensure_map_assets(force=True)
        logger.info(f'[Остров — ежедневные взаимодействия] Переход к NPC: {target}')
        self.map_goto(target)
        self.device.sleep(0.5)

    def _detect_target(self, target, similarity=0.82):
        """检测交互目标，使用目标模板在当前截图中定位。"""
        if not self._interact_model_available(target, getattr(target, 'name', 'interact_target')):
            return None
        image = self.device.screenshot()
        buttons = target.match_multi(image, similarity=similarity, threshold=5)
        if buttons:
            buttons.sort(key=lambda btn: btn.area[1], reverse=True)
            return buttons[0]
        return None

    def _move_to_target(self, target, max_attempts=20):
        """尝试移动到目标交互范围。"""
        for attempt in range(max_attempts):
            target_button = self._detect_target(target)
            if target_button is None:
                logger.info(f'[Остров — ежедневные взаимодействия] Цель не обнаружена, попытка перемещения {attempt + 1}/{max_attempts}')
                self.island_up(1000)
                continue

            x, y = target_button.center
            logger.info(f'[Остров — ежедневные взаимодействия] Координаты цели: ({x}, {y})')
            if y > 500:
                self.island_down(400)
            elif y < 260:
                self.island_up(400)
            if x > 900:
                self.island_right(400)
            elif x < 380:
                self.island_left(400)

            if self.appear(INTERACT_BUTTON, offset=(40, 40)):
                return True
        return False

    def _interact_once(self, target, max_attempts=20):
        """执行一次通用 NPC 交互。"""
        for attempt in range(max_attempts):
            if self.appear(INTERACT_BUTTON, offset=(40, 40)):
                logger.info('[Остров — ежедневные взаимодействия] Нажатие кнопки взаимодействия')
                self.device.click(INTERACT_BUTTON)
                self.device.sleep(0.5)
                return True
            target_button = self._detect_target(target)
            if target_button:
                logger.info(f'[Остров — ежедневные взаимодействия] Цель обнаружена: {target_button}')
                self.device.click(target_button)
                self.device.sleep(0.5)
                continue
            logger.info(f'[Остров — ежедневные взаимодействия] Цель не найдена, попытка {attempt + 1}/{max_attempts}')
            self.island_up(500)
        return False

    def _handle_dialog(self, end_button=None, max_loops=30):
        """处理交互对话。"""
        for _ in range(max_loops):
            self.device.screenshot()
            if end_button and self.appear(end_button, offset=(20, 20)):
                logger.info('[Остров — ежедневные взаимодействия] Обнаружено завершение диалога')
                return True
            if self.appear_then_click(DIALOG_CONTINUE, offset=(40, 40), interval=1):
                continue
            if self.appear_then_click(DIALOG_SKIP, offset=(40, 40), interval=1):
                continue
            if self.appear_then_click(DIALOG_CONFIRM, offset=(40, 40), interval=1):
                continue
            if self.handle_popup_confirm('ISLAND_INTERACT'):
                continue
            if self.handle_get_items():
                continue
            if self.ui_additional(get_ship=False):
                continue
            self.device.click(DIALOG_SAFE_AREA)
            self.device.sleep(0.3)
        return False

    def interact_akashi(self):
        """与明石交互。"""
        logger.hr('Остров — ежедневное взаимодействие: Akashi', level=2)
        if not self._interact_model_available(INTERACT_AKASHI, 'INTERACT_AKASHI'):
            return False
        self.goto_npc('Akashi')
        if not self._move_to_target(INTERACT_AKASHI):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось приблизиться к Akashi')
            return False
        if not self._interact_once(INTERACT_AKASHI):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось начать взаимодействие с Akashi')
            return False
        self._handle_dialog()
        return True

    def interact_august(self):
        """与奥古斯特交互。"""
        logger.hr('Остров — ежедневное взаимодействие: August', level=2)
        if not self._interact_model_available(INTERACT_AUGUST, 'INTERACT_AUGUST'):
            return False
        self.goto_npc('August')
        if not self._move_to_target(INTERACT_AUGUST):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось приблизиться к August')
            return False
        if not self._interact_once(INTERACT_AUGUST):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось начать взаимодействие с August')
            return False
        self._handle_dialog()
        return True

    def interact_william(self):
        """与威廉·D·波特交互。"""
        logger.hr('Остров — ежедневное взаимодействие: WilliamDPorter', level=2)
        if not self._interact_model_available(INTERACT_WILLIAM, 'INTERACT_WILLIAM'):
            return False
        self.goto_npc('WilliamDPorter')
        if not self._move_to_target(INTERACT_WILLIAM):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось приблизиться к WilliamDPorter')
            return False
        if not self._interact_once(INTERACT_WILLIAM):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось начать взаимодействие с WilliamDPorter')
            return False
        self._handle_dialog()
        return True

    def interact_queen_elizabeth(self):
        """与伊丽莎白女王交互。"""
        logger.hr('Остров — ежедневное взаимодействие: QueenElizabeth', level=2)
        if not self._interact_model_available(INTERACT_QUEEN_ELIZABETH, 'INTERACT_QUEEN_ELIZABETH'):
            return False
        self.goto_npc('QueenElizabeth')
        if not self._move_to_target(INTERACT_QUEEN_ELIZABETH):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось приблизиться к QueenElizabeth')
            return False
        if not self._interact_once(INTERACT_QUEEN_ELIZABETH):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось начать взаимодействие с QueenElizabeth')
            return False
        self._handle_dialog()
        return True

    def interact_yixian(self):
        """与逸仙交互。"""
        logger.hr('Остров — ежедневное взаимодействие: Yixian', level=2)
        if not self._interact_model_available(INTERACT_YIXIAN, 'INTERACT_YIXIAN'):
            return False
        self.goto_npc('Yixian')
        if not self._move_to_target(INTERACT_YIXIAN):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось приблизиться к Yixian')
            return False
        if not self._interact_once(INTERACT_YIXIAN):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось начать взаимодействие с Yixian')
            return False
        self._handle_dialog()
        return True

    def interact_takao(self):
        """与高雄交互。"""
        logger.hr('Остров — ежедневное взаимодействие: Takao', level=2)
        if not self._interact_model_available(INTERACT_TAKAO, 'INTERACT_TAKAO'):
            return False
        self.goto_npc('Takao')
        if not self._move_to_target(INTERACT_TAKAO):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось приблизиться к Takao')
            return False
        if not self._interact_once(INTERACT_TAKAO):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось начать взаимодействие с Takao')
            return False
        self._handle_dialog()
        return True

    def interact_eugen(self):
        """与欧根亲王交互。"""
        logger.hr('Остров — ежедневное взаимодействие: Eugen', level=2)
        if not self._interact_model_available(INTERACT_EUGEN, 'INTERACT_EUGEN'):
            return False
        self.goto_npc('Eugen')
        if not self._move_to_target(INTERACT_EUGEN):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось приблизиться к Eugen')
            return False
        if not self._interact_once(INTERACT_EUGEN):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось начать взаимодействие с Eugen')
            return False
        self._handle_dialog()
        return True

    def interact_hood(self):
        """与胡德交互。"""
        logger.hr('Остров — ежедневное взаимодействие: Hood', level=2)
        if not self._interact_model_available(INTERACT_HOOD, 'INTERACT_HOOD'):
            return False
        self.goto_npc('Hood')
        if not self._move_to_target(INTERACT_HOOD):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось приблизиться к Hood')
            return False
        if not self._interact_once(INTERACT_HOOD):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось начать взаимодействие с Hood')
            return False
        self._handle_dialog()
        return True

    def interact_javelin(self):
        """与标枪交互。"""
        logger.hr('Остров — ежедневное взаимодействие: Javelin', level=2)
        if not self._interact_model_available(INTERACT_JAVELIN, 'INTERACT_JAVELIN'):
            return False
        self.goto_npc('Javelin')
        if not self._move_to_target(INTERACT_JAVELIN):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось приблизиться к Javelin')
            return False
        if not self._interact_once(INTERACT_JAVELIN):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось начать взаимодействие с Javelin')
            return False
        self._handle_dialog()
        return True

    def interact_laffey(self):
        """与拉菲交互。"""
        logger.hr('Остров — ежедневное взаимодействие: Laffey', level=2)
        if not self._interact_model_available(INTERACT_LAFFEY, 'INTERACT_LAFFEY'):
            return False
        self.goto_npc('Laffey')
        if not self._move_to_target(INTERACT_LAFFEY):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось приблизиться к Laffey')
            return False
        if not self._interact_once(INTERACT_LAFFEY):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось начать взаимодействие с Laffey')
            return False
        self._handle_dialog()
        return True

    def interact_feiyun(self):
        """与飞云交互。"""
        logger.hr('Остров — ежедневное взаимодействие: FeiYun', level=2)
        if not self._interact_model_available(INTERACT_FEIYUN, 'INTERACT_FEIYUN'):
            return False
        self.goto_npc('FeiYun')
        if not self._move_to_target(INTERACT_FEIYUN):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось приблизиться к FeiYun')
            return False
        if not self._interact_once(INTERACT_FEIYUN):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось начать взаимодействие с FeiYun')
            return False
        self._handle_dialog()
        return True

    def interact_explorer(self):
        """与探索者交互。"""
        logger.hr('Остров — ежедневное взаимодействие: Explorer', level=2)
        if not self._interact_model_available(INTERACT_EXPLORER, 'INTERACT_EXPLORER'):
            return False
        self.goto_npc('Explorer')
        if not self._move_to_target(INTERACT_EXPLORER):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось приблизиться к Explorer')
            return False
        if not self._interact_once(INTERACT_EXPLORER):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось начать взаимодействие с Explorer')
            return False
        self._handle_dialog()
        return True

    def interact_navigator(self):
        """与领航员交互。"""
        logger.hr('Остров — ежедневное взаимодействие: Navigator', level=2)
        if not self._interact_model_available(INTERACT_NAVIGATOR, 'INTERACT_NAVIGATOR'):
            return False
        self.goto_npc('Navigator')
        if not self._move_to_target(INTERACT_NAVIGATOR):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось приблизиться к Navigator')
            return False
        if not self._interact_once(INTERACT_NAVIGATOR):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось начать взаимодействие с Navigator')
            return False
        self._handle_dialog()
        return True

    def interact_ocean_crosser(self):
        """与远洋者交互。"""
        logger.hr('Остров — ежедневное взаимодействие: OceanCrosser', level=2)
        if not self._interact_model_available(INTERACT_OCEAN_CROSSER, 'INTERACT_OCEAN_CROSSER'):
            return False
        self.goto_npc('OceanCrosser')
        if not self._move_to_target(INTERACT_OCEAN_CROSSER):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось приблизиться к OceanCrosser')
            return False
        if not self._interact_once(INTERACT_OCEAN_CROSSER):
            logger.warning('[Остров — ежедневные взаимодействия] Не удалось начать взаимодействие с OceanCrosser')
            return False
        self._handle_dialog()
        return True

    def interact_location(self, location, target, label):
        """通用地点 NPC 交互。"""
        logger.hr(f'Остров — ежедневное взаимодействие: {label}', level=2)
        if not self._interact_model_available(target, label):
            return False
        self.goto_npc(location)
        if not self._move_to_target(target):
            logger.warning(f'[Остров — ежедневные взаимодействия] Не удалось приблизиться к {label}')
            return False
        if not self._interact_once(target):
            logger.warning(f'[Остров — ежедневные взаимодействия] Не удалось начать взаимодействие с {label}')
            return False
        self._handle_dialog()
        return True

    def run(self):
        """执行岛屿每日交互。"""
        self.island_error = False
        self.ui_ensure(page_island)

        interactions = []
        if self.config.IslandDailyInteract_Akashi:
            interactions.append(('Akashi', self.interact_akashi))
        if self.config.IslandDailyInteract_August:
            interactions.append(('August', self.interact_august))
        if self.config.IslandDailyInteract_WilliamDPorter:
            interactions.append(('WilliamDPorter', self.interact_william))
        if self.config.IslandDailyInteract_QueenElizabeth:
            interactions.append(('QueenElizabeth', self.interact_queen_elizabeth))
        if self.config.IslandDailyInteract_Yixian:
            interactions.append(('Yixian', self.interact_yixian))
        if self.config.IslandDailyInteract_Takao:
            interactions.append(('Takao', self.interact_takao))
        if self.config.IslandDailyInteract_Eugen:
            interactions.append(('Eugen', self.interact_eugen))
        if self.config.IslandDailyInteract_Hood:
            interactions.append(('Hood', self.interact_hood))
        if self.config.IslandDailyInteract_Javelin:
            interactions.append(('Javelin', self.interact_javelin))
        if self.config.IslandDailyInteract_Laffey:
            interactions.append(('Laffey', self.interact_laffey))
        if self.config.IslandDailyInteract_FeiYun:
            interactions.append(('FeiYun', self.interact_feiyun))
        if self.config.IslandDailyInteract_Explorer:
            interactions.append(('Explorer', self.interact_explorer))
        if self.config.IslandDailyInteract_Navigator:
            interactions.append(('Navigator', self.interact_navigator))
        if self.config.IslandDailyInteract_OceanCrosser:
            interactions.append(('OceanCrosser', self.interact_ocean_crosser))

        if self.config.IslandDailyInteract_Harbor:
            interactions.append((
                'Harbor',
                lambda: self.interact_location('Harbor', INTERACT_HARBOR, 'Harbor')
            ))
        if self.config.IslandDailyInteract_Mine:
            interactions.append((
                'Mine',
                lambda: self.interact_location('Mine', INTERACT_MINE, 'Mine')
            ))
        if self.config.IslandDailyInteract_LoggingCamp:
            interactions.append((
                'LoggingCamp',
                lambda: self.interact_location('LoggingCamp', INTERACT_LOGGING_CAMP, 'LoggingCamp')
            ))
        if self.config.IslandDailyInteract_SunnyRanch:
            interactions.append((
                'SunnyRanch',
                lambda: self.interact_location('SunnyRanch', INTERACT_SUNNY_RANCH, 'SunnyRanch')
            ))
        if self.config.IslandDailyInteract_Hometown:
            interactions.append((
                'Hometown',
                lambda: self.interact_location('Hometown', INTERACT_HOMETOWN, 'Hometown')
            ))
        if self.config.IslandDailyInteract_Farm:
            interactions.append((
                'Farm',
                lambda: self.interact_location('Farm', INTERACT_FARM, 'Farm')
            ))

        if not interactions:
            logger.info('[Остров — ежедневные взаимодействия] Взаимодействия не настроены; задача завершена')
            self.config.task_delay(server_update=True)
            return True

        completed = 0
        for name, func in interactions:
            logger.info(f'[Остров — ежедневные взаимодействия] Выполнение: {name}')
            try:
                if func():
                    completed += 1
            except Exception as exc:
                logger.warning(f'[Остров — ежедневные взаимодействия] Сбой {name}: {exc}')
                continue

        total = len(interactions)
        logger.info(f'[Остров — ежедневные взаимодействия] Выполнено: {completed}/{total}')
        self.config.task_delay(server_update=True)

        if self.island_error:
            from module.exception import GameBugError
            raise GameBugError('Обнаружен Island ERROR1; требуется перезапуск')

        return completed == total
