# Global/EN runtime contract

AzurPilot-private-Ru supports the Global release only.

- Game server: `en`.
- Android package: `com.YoStarEN.AzurLane`.
- Canonical game asset root: `assets/en`.
- Runtime UI locale: `ru-RU`.
- `module/config/i18n/en-US.json` is retained only as a build-time key and placeholder parity source.
- Event display metadata uses English names and falls back to the stable technical event identifier.
- OCR exposes the Global/English `azur_lane` namespace while retaining shared detection and generic English model files.

Unsupported server or package values are rejected before device or resource side effects. The removed `assets/cn`, `assets/jp`, and `assets/tw` roots are not runtime fallbacks.
