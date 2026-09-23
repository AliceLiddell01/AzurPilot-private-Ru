"""Read-only queries for typed application-owned state stores."""

from __future__ import annotations

from azurpilot.tooling.contracts import (
    ApplicationStateDetails,
    CommissionRecoveryProjection,
    OperationState,
    ResultCode,
    ToolingResult,
)
from azurpilot.tooling.errors import ToolingError


class ApplicationStateService:
    """Project-owned query boundary; it never starts WebUI as a transport."""

    def read(
        self,
        state_id: str,
        profile: str,
    ) -> ToolingResult[ApplicationStateDetails, ApplicationStateDetails]:
        if state_id != "commission/recovery":
            raise ToolingError(
                ResultCode.TOOLING_INVALID_INVOCATION,
                "Неизвестный application state id.",
            )

        from module.application.commission_recovery import CommissionRecoveryStore

        store = CommissionRecoveryStore.from_environment()
        try:
            state = store.read(profile)
        except ValueError as exc:
            raise ToolingError(
                ResultCode.TOOLING_INVALID_INVOCATION,
                "Профиль не соответствует canonical application state contract.",
            ) from exc
        finally:
            store.close()

        value = CommissionRecoveryProjection(
            profile=state.profile,
            status=state.status,
            remaining=state.remaining,
            used=state.used,
            next_oil_cost=state.next_oil_cost,
            next_ap_gain=state.next_ap_gain,
            confirmed_at=state.confirmed_at,
            reset_at=state.reset_at,
            source=state.source,
            last_result=state.last_result,
            cache_status=state.cache_status,
            error=state.error,
        )
        details = ApplicationStateDetails(value=value)
        return ToolingResult(
            ok=True,
            code=ResultCode.OK,
            state=OperationState.READY,
            message=(
                "Application state прочитан."
                if state.status == "confirmed"
                else "Application state прочитан; значение не подтверждено."
            ),
            details=details,
        )


__all__ = ["ApplicationStateService"]
