# Инструменты приёмки

Эти команды работают с реальной внешней средой и намеренно не входят в обязательный CI.

- `uv run python -m tools.acceptance.device --help` — проверки устройства и управления с явно выбранной целью;
- `uv run python -m tools.acceptance.formation --help` — реальная проверка Formation Fleet Scanner для выбранного флота;
- `uv run python -m tools.acceptance.ocr --help` — локальная проверка OCR provider и безопасного debug output;
- `uv run python -m tools.acceptance.ocr_opsi_zone --help` — ограниченная read-only проверка OCR зон Operation Siren;
- `uv run python -m tools.acceptance.ocr_commission --help` — ограниченная read-only проверка Commission OCR;
- `uv run python -m tools.acceptance.webui_smoke --help` — локальный smoke запуска WebUI;
- `uv run python -m tools.acceptance.emulator_recovery_smoke --help` — bounded smoke recovery-контрактов;
- `uv run python -m tools.acceptance.game_recovery_smoke --help` — bounded smoke game-recovery контрактов;
- `uv run python -m tools.acceptance.emulator_recovery --help` — live recovery acceptance для явно выбранного эмулятора;
- `uv run python -m tools.acceptance.webui_traceback_browser` — browser acceptance traceback fixtures;
- `pwsh -File tools/acceptance/powershell/Test-AzurPilotLifecycle.ps1` и
  `pwsh -File tools/acceptance/powershell/Test-Update-AzurPilot.ps1` — Windows lifecycle checks.

Результаты приёмки являются локальной диагностикой. Не коммитьте generated reports, screenshots, device identifiers, локальные пути и внешний вывод.

Команды не должны запускаться при импорте модуля. Для реального прогона требуется явно подготовленная контролируемая среда; обязательные jobs `Python`, `Windows` и `Security` её не используют.
