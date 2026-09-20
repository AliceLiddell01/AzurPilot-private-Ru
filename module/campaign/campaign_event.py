"""Модуль управления событиями кампании.

Управляет конфигурацией и состоянием событийных кампаний, включая:
- Автоматическое отключение и сброс конфигурации по завершении события
- Сброс этапа для задач GemsFarming (откат на 2-4 по окончании события)
- Отправка push-уведомлений о событиях
- Проверка доступности страниц событий при навигации

Поддерживаемые типы событий:
- Обычные события (Event)
- Рейдовые события (Raid)
- Коллаборации (Coalition)
- Военный архив (War Archives)
- Госпиталь (Hospital)
- Морской эскорт (MaritimeEscort)

Наследуется от CampaignStatus, обеспечивая возможность проверки статуса событий.
"""

import re

from module.campaign.campaign_status import CampaignStatus
from module.config.config_updater import COALITIONS, EVENTS, GEMS_FARMINGS, HOSPITAL, MARITIME_ESCORTS, RAIDS
from module.config.time_sentinel import is_default_time
from module.config.time_source import now as current_time
from module.config.utils import DEFAULT_TIME
from module.logger import logger
from module.notify import handle_notify
from module.ui.assets import CAMPAIGN_MENU_NO_EVENT
from module.ui.page import page_campaign_menu, page_coalition, page_event, page_sp
from module.war_archives.assets import WAR_ARCHIVES_CAMPAIGN_CHECK


class CampaignEvent(CampaignStatus):
    """Менеджер событий кампании.

    Обрабатывает жизненный цикл событий: обнаружение, отключение, сброс настроек и уведомления.
    """
    def _reset_gems_farming(self, tasks):
        """
        Сбрасывает этап GemsFarming на 2-4 по завершении события.

        Args:
            tasks (list[str]): Список названий задач.
        """
        for task in tasks:
            if task not in GEMS_FARMINGS:
                continue
            name = self.config.cross_get(keys=f'{task}.Campaign.Name', default='2-4')
            if not self.stage_is_main(name):
                logger.info(f'[Кампания события] Фарм самоцветов сброшен на 2-4')
                self.config.cross_set(keys=f'{task}.Campaign.Name', value='2-4')
                self.config.cross_set(keys=f'{task}.Campaign.Event', value='campaign_main')

    def _disable_tasks(self, tasks):
        """
        Отключает задачи из указанного списка задач.

        Args:
            tasks (list[str]): Список названий задач.
        """
        with self.config.multi_set():
            # Отключаем обычные задачи события
            for task in tasks:
                if task in GEMS_FARMINGS:
                    continue
                keys = f'{task}.Scheduler.Enable'
                logger.info(f'[Кампания события] Задача `{task}` отключена')
                self.config.cross_set(keys=keys, value=False)
                keys = f'{task}.Emotion.Fleet1Onsen'
                self.config.cross_set(keys=keys, value=False)
                keys = f'{task}.Emotion.Fleet2Onsen'
                self.config.cross_set(keys=keys, value=False)

            # Сбрасываем GemsFarming
            self._reset_gems_farming(tasks)

            logger.info(f'[Кампания события] Ограничение времени события сброшено')
            self.config.cross_set(keys='EventGeneral.EventGeneral.TimeLimit', value=DEFAULT_TIME)

    def event_pt_limit_triggered(self):
        """
        Проверяет, достигнут ли лимит очков события (PT).

        Returns:
            bool: Сработал ли лимит очков события.

        Pages:
            in: page_event or page_sp
        """
        # Некоторые конфигурации могут использовать формат с разделителем тысяч, например "100,000"
        limit = int(
            re.sub(r'[,.\'"，。]', '', str(self.config.EventGeneral_PtLimit))
        )
        tasks = EVENTS + RAIDS + COALITIONS + GEMS_FARMINGS + HOSPITAL
        command = self.config.Scheduler_Command
        if limit <= 0 or command not in tasks:
            self.get_event_pt()
            return False
        if command in GEMS_FARMINGS and self.stage_is_main(self.config.Campaign_Name):
            self.get_event_pt()
            return False

        pt = self.get_event_pt()
        if pt >= limit and limit > 0:
            logger.attr('Лимит очков события', f'{pt}/{limit}')
            logger.hr(f'Достигнут лимит очков события: {limit}')
            self._disable_tasks(tasks)
            return True
        else:
            return False

    def coin_limit_triggered(self):
        """
        Проверяет, достигло ли количество монет лимита StopCondition.CoinLimit.

        Returns:
            bool: Сработал ли лимит монет.
        """
        limit = int(
            re.sub(r'[,.\'"，。]', '', str(self.config.StopCondition_CoinLimit))
        )
        if limit <= 0:
            return False

        coin = self.get_coin()
        if coin == 0:
            # Защита от ошибки OCR / нулевого результата
            logger.warning('[Кампания события] Монеты не найдены')
            return False

        logger.attr('Лимит монет', f'{coin}/{limit}')
        if coin >= limit:
            logger.hr(f'Достигнут лимит монет: {limit}')
            self.config.task_delay(minute=(120, 240))
            handle_notify(
                self.config.Error_OnePushConfig,
                title=f"AzurPilot <{self.config.config_name}>: кампания отложена",
                content=f"<{self.config.config_name}> {self.config.Campaign_Name}: достигнут лимит монет"
            )
            return True
        else:
            return False

    def event_time_limit_triggered(self):
        """
        Проверяет, достигнуто ли ограничение по времени события.

        Returns:
            bool: Сработало ли ограничение времени.

        Pages:
            in: page_event or page_sp
        """
        limit = self.config.EventGeneral_TimeLimit
        tasks = EVENTS + RAIDS + COALITIONS + GEMS_FARMINGS + MARITIME_ESCORTS + HOSPITAL
        command = self.config.Scheduler_Command
        if command not in tasks or is_default_time(limit):
            return False
        if command in GEMS_FARMINGS and self.stage_is_main(self.config.Campaign_Name):
            return False

        now = current_time().replace(microsecond=0)
        logger.attr('Ограничение времени события', f'{now} -> {limit}')
        if now > limit:
            logger.hr(f'Достигнуто ограничение времени события: {limit}')
            self._disable_tasks(tasks)
            return True
        else:
            return False

    def triggered_task_balancer(self):
        """
        Проверяет, сработал ли балансировщик задач.

        Returns:
            bool: Требуется ли переключение задачи.

        Pages:
            in: page_event or page_sp
        """
        from module.config.deep import deep_get
        limit = self.config.TaskBalancer_CoinLimit
        coin = deep_get(self.config.data, 'Dashboard.Coin.Value')
        logger.attr('Количество монет', coin)

        # Проверяем монеты
        if coin == 0:
            # Защита от ошибки OCR / нулевого результата
            logger.warning('[Кампания события] Монеты не найдены')
            return False
        else:
            if self.is_balancer_task():
                if coin < limit:
                    logger.hr('Достигнут лимит монет')
                    return True
                else:
                    return False
            else:
                return False

    def handle_task_balancer(self):
        if self.config.TaskBalancer_Enable and self.triggered_task_balancer():
            self.config.task_delay(minute=5)
            next_task = self.config.TaskBalancer_TaskCall
            logger.hr(f'Сработал балансировщик задач; переключаюсь на {next_task}')
            self.config.task_call(next_task)
            self.config.task_stop()

    def is_event_entrance_available(self):
        """
        Проверяет доступность входа в событие.

        Returns:
            bool: True, если вход доступен.

        Raises:
            TaskEnd: Выбрасывается, если событие недоступно.
        """
        if self.appear(CAMPAIGN_MENU_NO_EVENT, offset=(20, 20)):
            logger.info('[Кампания события] Событие недоступно; задача отключена')
            tasks = EVENTS + RAIDS + COALITIONS + GEMS_FARMINGS + HOSPITAL
            self._disable_tasks(tasks)
            self.config.task_stop()
        else:
            logger.info('[Кампания события] Событие доступно')
            return True

    def ui_goto_event(self):
        # Уже на page_event, поэтому проверку события пропускаем.
        if self.ui_get_current_page() == page_event:
            if self.appear(WAR_ARCHIVES_CAMPAIGN_CHECK, offset=(20, 20)):
                logger.info('[Кампания события] Открыты Архивы')
                self.ui_goto_main()
            else:
                logger.info('[Кампания события] Уже на странице события')
                return True
        self.ui_goto(page_campaign_menu)
        # Проверяем доступность события
        if self.is_event_entrance_available():
            self.ui_goto(page_event)
            return True

    def ui_goto_sp(self):
        # Уже на page_sp, поэтому проверку события пропускаем.
        if self.ui_get_current_page() == page_sp:
            if self.appear(WAR_ARCHIVES_CAMPAIGN_CHECK, offset=(20, 20)):
                logger.info('[Кампания события] Открыты Архивы')
                self.ui_goto_main()
            else:
                logger.info('[Кампания события] Уже на странице SP')
                return True
        self.ui_goto(page_campaign_menu)
        # Проверяем доступность события
        if self.is_event_entrance_available():
            self.ui_goto(page_sp)
            return True

    def ui_goto_coalition(self):
        # Уже на page_coalition, поэтому проверку события пропускаем.
        if self.ui_get_current_page() == page_coalition:
            logger.info('[Кампания события] Уже на странице коллаборации')
            return True
        else:
            self.ui_goto(page_campaign_menu)
            # Проверяем доступность события
            if self.is_event_entrance_available():
                self.ui_goto(page_coalition)
                return True

    def disable_raid_on_event(self):
        """
        Отключает задачи рейдов (или коллабораций) при входе в событие,
        чтобы предотвратить работу устаревших рейдов, если пользователь забыл отключить их вручную.
        """
        command = self.config.Scheduler_Command
        if command not in EVENTS + GEMS_FARMINGS:
            return False
        if command in GEMS_FARMINGS and self.stage_is_main(self.config.Campaign_Name):
            return False

        tasks = RAIDS + COALITIONS + MARITIME_ESCORTS
        tasks = [t for t in tasks if self.config.is_task_enabled(t)]
        if tasks:
            logger.info('[Кампания события] Идёт новое событие; задача старого рейда отключена')
            self._disable_tasks(tasks)
            return True
        else:
            return False

    def disable_event_on_raid(self):
        """
        Отключает задачи обычных событий при входе в рейд или коллаборацию,
        чтобы предотвратить работу устаревших событий, если пользователь забыл отключить их вручную.
        """
        command = self.config.Scheduler_Command
        if command not in RAIDS + COALITIONS + MARITIME_ESCORTS:
            return False

        events = [t for t in EVENTS if self.config.is_task_enabled(t)]
        gems = [t for t in GEMS_FARMINGS if self.config.is_task_enabled(t)]
        with self.config.multi_set():
            if events:
                logger.info('[Кампания события] Идёт новый рейд; задача старого события отключена')
                self._disable_tasks(events)
            if gems:
                self._reset_gems_farming(gems)
        return events or gems

    @staticmethod
    def stage_is_main(name) -> bool:
        """
        Определяет, является ли указанное название этапа этапом основной кампании.

        Args:
            name (str): Название этапа, например `7-2`, `D3`.
        """
        regex_main = re.compile(r'\d{1,2}[-_]\d')
        return bool(regex_main.search(name))
