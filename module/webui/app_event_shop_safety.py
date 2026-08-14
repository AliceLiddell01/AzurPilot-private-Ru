"""Compatibility MRO bridge for the priority-driven EventShop WebUI."""

from module.webui.app_event_shop_v2 import EventShopV2Mixin


class EventShopSafetyMixin(EventShopV2Mixin):
    """Keep the historical mixin slot while EventShop uses the new priority UI."""

    pass
