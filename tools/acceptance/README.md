# Инструменты приёмки

Эти команды работают с реальной внешней средой и намеренно не входят в обязательный CI.

- `uv run python -m tools.acceptance.device --help` — проверки устройства и управления с явно выбранной целью;
- `uv run python -m tools.acceptance.ocr --help` — локальная проверка OCR provider и безопасного debug output;
- `uv run python -m tools.acceptance.ocr_opsi_zone --help` — ограниченная read-only проверка OCR зон Operation Siren;
- `uv run python -m tools.acceptance.ocr_commission --help` — ограниченная read-only проверка Commission OCR;
- `uv run python -m tools.acceptance.webui_smoke --help` — локальный smoke запуска WebUI.

Результаты приёмки являются локальной диагностикой. Не коммитьте generated reports, screenshots, device identifiers, локальные пути и внешний вывод.

Команды не должны запускаться при импорте модуля. Для реального прогона требуется явно подготовленная контролируемая среда; обязательные jobs `Python`, `Windows` и `Security` её не используют.
