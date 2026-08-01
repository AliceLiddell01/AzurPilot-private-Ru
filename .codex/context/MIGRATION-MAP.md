# Карта переработки старого AI-контекста

## Сохранено в переработанном виде

| Старый источник | Новый документ |
|---|---|
| `.agent/ARCHITECTURE.md`, `MODULE-MAP.md`, `README.md`, `GAME-FUNCTIONS.md` | `01-PROJECT-MAP.md`, `02-RUNTIME-ARCHITECTURE.md` |
| `.agent/CONFIG.md`, `.cursor/rules/config-system.mdc` | `03-CONFIG-I18N.md` |
| `.agent/BASE.md`, `DEVICE.md`, `UI.md`, `OCR.md`, `OCR-USAGE.md`, `HANDLER.md` | `04-DEVICE-UI-OCR.md` |
| `.agent/COMBAT.md`, `COMBAT-UI.md`, `MAP.md`, `MAP-DETECTION.md`, `CAMPAIGN.md` | `05-COMBAT-MAP-CAMPAIGN.md` |
| `.agent/OS-SYSTEM.md` | `06-OPERATION-SIREN.md` |
| `.agent/ENTRY-ALAS.md`, `ENTRY-GUI.md`, `ENTRY-MCP-SERVER.md`, `INFRASTRUCTURE.md` | `02-RUNTIME-ARCHITECTURE.md`, `07-WEBUI-INFRASTRUCTURE.md` |
| `.agent/CONVENTIONS.md`, `.cursor/rules/develop-rules.mdc` | корневой `AGENTS.md`, `08-VERIFICATION.md` |
| проектные русские Git/PowerShell-регламенты | `GIT-WORKFLOW.md`, `POWERSHELL-GIT-RULES.md` |

## Не перенесено намеренно

### `.claude/settings*.json`

Причины:

- локальные разрешения другого инструмента;
- wildcard-доступ;
- абсолютный путь конкретного компьютера;
- одноразовые команды;
- не используются Codex.

### `.agent/ISSUES.md`

Причины:

- субъективные и неподтверждённые предложения;
- отсутствует связь с актуальными issue/PR;
- быстро устаревает.

Реальные проблемы следует заводить как GitHub Issues с воспроизведением и доказательствами.

### Построчные и количественные снимки

Удалены:

- номера строк;
- точное число методов/tools/files;
- длина классов на дату генерации;
- старые default ports и branch snapshots без проверки;
- детальное перечисление каждой функции.

Такие сведения Codex должен получать из текущего кода.

## Язык

Новый контекст полностью русскоязычный. Китайские обязательные инструкции удалены. Имена кода и внешних контрактов оставлены на английском.
