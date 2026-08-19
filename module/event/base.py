"""活动战役基础模块。

提供活动关卡的基类和通用工具，供 CampaignABCD、CampaignSP 等子类继承。

主要功能：
- EventStage: 从活动目录中的 .py 文件名提取关卡名称
- EventBase: 活动战役基类，提供关卡名称转换和过滤功能
- STAGE_FILTER: 基于正则的关卡过滤器，用于用户自定义关卡选择

活动地图文件存放在 campaign/{event_name}/ 目录下，
每个 .py 文件对应一个关卡（如 a1.py, b1.py, sp.py）。
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
    """活动关卡文件的封装，从文件名提取关卡名称。"""

    def __init__(self, filename):
        self.filename = filename
        # 从文件名中去掉 .py 后缀作为关卡名
        self.stage = 'unknown'
        if filename[-3:] == '.py':
            self.stage = filename[:-3]

    def __str__(self):
        return self.stage

    def __eq__(self, other):
        return str(self) == str(other)


class EventBase(CampaignRun):
    """活动战役基类，继承自 CampaignRun。

    提供活动关卡加载、关卡名称转换和关卡过滤等基础功能。
    """

    def load_campaign(self, *args, **kwargs):
        """加载战役地图，并强制关闭一次性关卡标记。"""
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
