"""Запись и синхронизация изменений игровых ресурсов.

При изменении значений нефти, монет, алмазов, кубов и других ресурсов модуль
обновляет соответствующие поля ``Dashboard`` в конфигурации и время последней
записи. Для PT строка Dashboard хранит накопительный счётчик ивента; текущий
доступный баланс EventShop хранится отдельно в EventObservation.

Пример:
    >>> log_res = LogRes(config)
    >>> log_res.Oil = 12345
    >>> log_res.Coin = {'Value': 50000, 'Limit': 99999}

Структура данных панели:
    Dashboard.<имя ресурса> = {
        'Value': int,       # Текущее значение либо накопительный PT ивента.
        'Record': datetime, # Время последнего обновления.
        'Limit': int,       # Необязательный верхний предел.
    }
"""

from datetime import datetime

from cached_property import cached_property

from module.application.resource_fields import RESOURCE_NAME_MAP
from module.application.runtime_storage import get_runtime_storage
from module.config.deep import deep_get
from module.logger import logger


class LogRes:
    """Синхронизировать присваивания ресурсов с разделом ``Dashboard``.

    Значение ресурса задаётся как целое число либо как словарь с полями
    ``Value`` и, при необходимости, ``Limit``/``Total``.
    """

    YellowCoin: list

    def __init__(self, config):
        self.__dict__['config'] = config

    def _is_event_shop_pt(self, key):
        """Не позволять текущему балансу магазина перезаписывать накопительный PT."""
        if key != 'Pt':
            return False
        task_name = str(
            getattr(getattr(self.config, 'task', None), 'command', '') or ''
        )
        return task_name == 'EventShop'

    def _record_event_pt(self, value):
        """Сохранить накопительный PT и безопасно разбудить магазин ивента."""
        try:
            from module.webui.event_currency import persist_event_currency_update

            persist_event_currency_update(
                self.config,
                value,
                source='dashboard_ocr',
            )
        except Exception:
            # Запись ресурса является основной операцией. Планирование EventShop —
            # дополнительная операция и не должно ломать корректное обновление Dashboard.
            logger.exception(
                '[Ресурсы журнала] Не удалось сохранить PT ивента или разбудить EventShop'
            )

    def __setattr__(self, key, value):
        if self._is_event_shop_pt(key):
            # EventShop отдельно сохраняет точный доступный баланс как event_shop_ocr.
            # Dashboard.Pt остаётся накопительным счётчиком ивента и не смешивает семантики.
            return
        if key in self.groups:
            _key_group = f'Dashboard.{key}'
            _mod = False
            original = deep_get(self.config.data, keys=_key_group)
            if isinstance(value, int):
                if original['Value'] != value:
                    _key = _key_group + '.Value'
                    self.config.modified[_key] = value
                    _time = datetime.now().replace(microsecond=0)
                    _key_time = _key_group + f'.Record'
                    self.config.modified[_key_time] = _time
                    if key == 'YellowCoin':
                        instance_name = getattr(self.config, 'config_name', 'default')
                        get_runtime_storage().record_currency_snapshot(
                            instance_name,
                            'yellow_coin',
                            int(value),
                            source='dashboard',
                        )
                    if key == 'Pt':
                        self._record_event_pt(value)
                    # Сохраняем полный снимок ресурсов после изменения значения.
                    self._record_all_resource_snapshot({key: value})
            elif isinstance(value, dict):
                _mod = False
                value_changed = False
                for value_name, _value in value.items():
                    if _value == original[value_name]:
                        continue
                    _key = _key_group + f'.{value_name}'
                    self.config.modified[_key] = _value
                    _key_time = _key_group + f'.Record'
                    _time = datetime.now().replace(microsecond=0)
                    self.config.modified[_key_time] = _time
                    _mod = True
                    if value_name == 'Value':
                        value_changed = True
                if _mod:
                    if key == 'ActionPoint' and value.get('Value') is not None:
                        from module.statistics.opsi_runtime import record_ap_snapshot

                        source = 'dashboard'
                        task = getattr(getattr(self.config, 'task', None), 'command', None)
                        if task:
                            source = task
                        record_ap_snapshot(
                            self.config,
                            ap_current=value.get('Value'),
                            ap_total=value.get('Total'),
                            source=source,
                        )
                    if key == 'Pt' and value_changed:
                        self._record_event_pt(value.get('Value'))
                    # Сохраняем полный снимок ресурсов после изменения словаря.
                    value_to_record = value.get('Value') if isinstance(value, dict) else None
                    if value_to_record is not None:
                        self._record_all_resource_snapshot({key: value_to_record})
                    else:
                        self._record_all_resource_snapshot()
        else:
            logger.info('[Ресурсы журнала] Такого ресурса нет на панели мониторинга')
            super().__setattr__(name=key, value=value)

    def _record_all_resource_snapshot(self, overrides=None):
        """Собрать текущие значения ``Dashboard`` и записать снимок ресурсов."""
        instance_name = getattr(self.config, 'config_name', 'default')
        overrides = overrides or {}
        resources = {}
        for group_name in self.groups:
            if group_name not in RESOURCE_NAME_MAP:
                logger.warning(
                    f'[Ресурсы журнала] Для группы Dashboard.{group_name} '
                    'нет поля в реестре снимка ресурсов'
                )
                continue
            if group_name in overrides:
                value = overrides[group_name]
            elif f'Dashboard.{group_name}.Value' in self.config.modified:
                value = self.config.modified[f'Dashboard.{group_name}.Value']
            else:
                group_data = deep_get(self.config.data, f'Dashboard.{group_name}')
                if not isinstance(group_data, dict):
                    continue
                value = group_data.get('Value')
            if value is not None:
                try:
                    resources[RESOURCE_NAME_MAP[group_name]] = int(value)
                except (TypeError, ValueError):
                    continue
        get_runtime_storage().record_resource_snapshot(instance_name, resources)

    def group(self, name):
        return deep_get(self.config.data, f'Dashboard.{name}')

    @cached_property
    def groups(self) -> dict:
        from module.config.utils import read_file, filepath_argument
        return deep_get(d=read_file(filepath_argument("dashboard")), keys='Dashboard')


if __name__ == '__main__':
    from module.config.config import AzurLaneConfig

    config = AzurLaneConfig('alas2')
    LogRes(config=config).ActionPoint = {'Total': 99999, 'Value': 99999}
    config.update()
    exit(0)
