"""Runtime UI locale and Global event metadata contract."""

UI_LOCALE = "ru-RU"

# en-US is retained only as a build-time key/parity source for ru-RU generation.
BUILD_TIME_LOCALES = ("en-US",)

# Rejection-only compatibility list; none of these locales is runtime-selectable.
LEGACY_UI_LOCALES = ("en-US", "ja-JP", "zh-CN", "zh-MIAO", "zh-TW")

EVENT_NAME_SOURCE = "en"
EVENT_NAME_FALLBACK_ORDER = ()
