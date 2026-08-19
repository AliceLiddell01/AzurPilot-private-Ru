"""Базовые инструменты для этапов событий.

Модуль предоставляет общий слой для CampaignABCD и CampaignSP:
- EventStage представляет имя этапа;
- EventBase загружает карты и приводит имена этапов к текущему источнику;
- STAGE_FILTER применяет пользовательский фильтр этапов.

Для current generated-события список этапов берётся из verified-каталога
Event artifact. Физический каталог campaign/{event_name}/ остаётся fallback
только для исторических legacy-событий.
"""

import os
import re

from module.base.filter import Filter
from module.campaign.run import CampaignRun
from module.config.time_source import now as current_time
from module.event_datamine.campaign_selector import (
    generated_stage_target,
    resolve_generated_campaign_modules,
)
from module.handler.fast_forward import to_map_file_name

STAGE_FILTER = Filter(regex=re.compile('^(.*?)$'), attr=('stage',))


class EventStage:
    """Представление этапа события, полученного из имени campaign-модуля."""

    def __init__(self, filename):
        self.filename = filename
        self.stage = 'unknown'
        if filename[-3:] == '.py':
            self.stage = filename[:-3]

    def __str__(self):
        return self.stage

    def __eq__(self, other):
        return str(self) == str(other)


class EventBase(CampaignRun):
    """Базовый исполнитель событий с единым выбором источника этапов."""

    def load_campaign(self, *args, **kwargs):
        """Загрузить карту и отключить ограничение одноразового этапа для daily-задач."""
        super().load_campaign(*args, **kwargs)
        self.campaign.config.temporary(
            MAP_IS_ONE_TIME_STAGE=False
        )

    def available_stages(self):
        """Вернуть доступные этапы текущего события из безопасного источника.

        Для current generated-события источником является verified-каталог
        artifact. Физический legacy-каталог используется только как fallback,
        когда selector не относится к current generated-событию.
        """

        selector = self.config.Campaign_Event
        modules = resolve_generated_campaign_modules(
            selector,
            now=current_time(),
        )
        if modules is not None:
            return [EventStage(f'{stage}.py') for stage in modules]
        return [
            EventStage(file)
            for file in os.listdir(f'./campaign/{selector}')
        ]

    def convert_stages(self, stages):
        """Привести этапы к именам, соответствующим текущему источнику карт.

        Current generated-событие сохраняет канонические имена из verified
        artifact и не пропускает фильтры через legacy T/HT aliases. Для
        исторических событий сохраняется прежняя нормализация handle_stage_name().
        """

        def convert(n):
            selector = self.config.Campaign_Event
            modules = resolve_generated_campaign_modules(
                selector,
                now=current_time(),
            )
            if modules is not None:
                target = generated_stage_target(modules, n)
                if target is not None:
                    return target.rsplit('.', 1)[-1]
                return to_map_file_name(n)
            return self.handle_stage_name(n, folder=selector)[0]

        if isinstance(stages, str):
            return convert(stages)
        if isinstance(stages, list):
            out = []
            for name in stages:
                if isinstance(name, EventStage):
                    name.stage = convert(name.stage)
                    out.append(name)
                elif isinstance(name, str):
                    out.append(convert(name))
                else:
                    out.append(name)
            return out
        if isinstance(stages, Filter):
            stages.filter = [[convert(selection[0])] for selection in stages.filter]
            return stages
        return stages
