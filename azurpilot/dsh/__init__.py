"""Клиентская граница DeepSeek Harness, принадлежащая репозиторию AzurPilot.

Пакет владеет только статическими ассетами клиента и именами переменных
окружения генерации: ни учётные данные моста, ни изменяемая идентичность
источника здесь не хранятся. Значения передаёт запускатель
`azurpilot.tooling.dsh` на время одной сессии.
"""

from __future__ import annotations

from pathlib import Path

__all__ = (
    "ASSET_ROOT",
    "AZUR_EXECUTABLE_ENV_VAR",
    "BRIDGE_GUARD_PATH",
    "BRIDGE_OVERLAY_PATH",
    "CHECKOUT_ROOT_ENV_VAR",
    "DSH_PACKAGE_ENV_VAR",
    "DSH_PROFILE_ENV_VAR",
    "IDENTITY_ENV_VARS",
    "SOURCE_REVISION_ENV_VAR",
)

ASSET_ROOT: Path = Path(__file__).resolve().parent

BRIDGE_OVERLAY_PATH: Path = ASSET_ROOT / "azurpilot-bridge.patch.yml"
BRIDGE_GUARD_PATH: Path = ASSET_ROOT / "azurpilot-bridge-guard.mjs"

CHECKOUT_ROOT_ENV_VAR = "AZURPILOT_DSH_CHECKOUT_ROOT"
SOURCE_REVISION_ENV_VAR = "AZURPILOT_DSH_SOURCE_REVISION"
AZUR_EXECUTABLE_ENV_VAR = "AZURPILOT_DSH_AZUR"
DSH_PROFILE_ENV_VAR = "AZURPILOT_DSH_PROFILE"
DSH_PACKAGE_ENV_VAR = "AZURPILOT_DSH_PACKAGE"

IDENTITY_ENV_VARS: dict[str, str] = {
    "azurpilot-dev": "AZURPILOT_DSH_DEV_SOURCE_IDENTITY",
    "azurpilot-game": "AZURPILOT_DSH_GAME_SOURCE_IDENTITY",
}
