"""平台模拟器管理包。"""

from module.device.env import IS_WINDOWS, IS_MACINTOSH

if IS_WINDOWS:
    from module.device.platform.platform_windows import PlatformWindows as Platform
elif IS_MACINTOSH:
    from module.device.platform.platform_mac import PlatformMac as Platform
else:
    from module.device.platform.platform_base import PlatformBase as Platform


def get_recovery_platform(config):
    """Создать платформу для изолированной Stage 2 recovery-chain."""
    if IS_WINDOWS:
        from module.device.platform.platform_windows_recovery import RecoveryPlatformWindows
        return RecoveryPlatformWindows(config, connect=False)
    return Platform(config, connect=False)
