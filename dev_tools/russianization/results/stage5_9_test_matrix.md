# Матрица тестов Stage 5–9

| Stage | Подсистема | Автоматические проверки | Ручная приёмка |
|---|---|---|---|
| 5 | locale loader | ru-RU only, no foreign fallback, browser fallback, key completeness | WebUI startup |
| 5 | deploy migration | patch-only Language, unknown keys preserved, idempotence | existing user config copy |
| 5 | server separation | EN server unchanged across locale migration | EN/Global profile open |
| 5 | generator | config/i18n regeneration leaves tree clean | none |
| 6 | WebUI | inventory reaches zero untranslated first-party UI items; render smoke fixtures | dark/light, long Russian labels |
| 6 | CLI/OOBE | hardcoded UI gate with technical allowlist | OOBE and error pages |
| 7 | deploy/process logs | first-party Russian context; raw stderr preserved | Start/Update/Repair/Build logs |
| 7 | config/lifecycle | message sequence preserved; only text changes | startup/shutdown |
| 8A | device/ADB/control | reconnect/timeouts/backend messages; raw ADB preserved | emulator/device smoke |
| 8B | OCR | model selection/fallback/template errors | EN OCR smoke |
| 8C | scheduler/tasks | queue/start/retry/stop sequence unchanged | safe task smoke |
| 8D | campaign/combat | event sequence and formatted values unchanged | safe campaign scenario |
| 8E | Operation Siren | AP/navigation/repair/shop sequence unchanged | isolated OS smoke |
| 9 | locale cleanup | no references to removed locale; generator-clean | WebUI startup |
| 9 | asset cleanup | missing-reference scan, glob/import/registry tests, button_extract | EN/Global/OCR smoke |
| 9 | package/server options | unsupported legacy profile yields explicit migration error | old profile fixture |

Permanent gates retained: Ruff syntax/static, Stage 3 regression suite, button/config regeneration, PowerShell Parser and PSScriptAnalyzer.
