"""Общая политика покупки очков действия в Операции «Сирена»."""

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class ActionPointPurchasePolicy:
    """Стоимость и прирост AP для следующей недельной покупки."""

    oil_cost: int
    ap_gain: int


ACTION_POINT_PURCHASE_POLICY_BY_REMAINING = MappingProxyType(
    {
        1: ActionPointPurchasePolicy(oil_cost=4000, ap_gain=400),
        2: ActionPointPurchasePolicy(oil_cost=2000, ap_gain=200),
        3: ActionPointPurchasePolicy(oil_cost=2000, ap_gain=200),
        4: ActionPointPurchasePolicy(oil_cost=1000, ap_gain=100),
        5: ActionPointPurchasePolicy(oil_cost=1000, ap_gain=100),
    }
)


def get_action_point_purchase_policy(
    remaining: int | None,
) -> ActionPointPurchasePolicy | None:
    """Вернуть условия покупки по числу оставшихся недельных покупок."""

    if isinstance(remaining, bool) or not isinstance(remaining, int):
        return None
    return ACTION_POINT_PURCHASE_POLICY_BY_REMAINING.get(remaining)


__all__ = (
    "ACTION_POINT_PURCHASE_POLICY_BY_REMAINING",
    "ActionPointPurchasePolicy",
    "get_action_point_purchase_policy",
)
