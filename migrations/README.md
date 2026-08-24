# Alembic-миграции

Alembic является единственным механизмом изменения PostgreSQL schema.
Revision-файлы не должны читать production config или выполнять runtime wiring.

`AZURPILOT_POSTGRES_DISPOSABLE=1` является только явным подтверждением
оператора, а не доказательством изоляции. Destructive downgrade выполняется
только в отдельной test-only БД с credentials, недоступными production.
Alembic entry point также требует отдельного точного подтверждения target через
`AZURPILOT_POSTGRES_DISPOSABLE_HOST`, `_PORT`, `_DATABASE` и `_USER`.

Текущий единственный head — `0004_fleet_manual_scan_command`. Он расширяет
schema v1 нормализованным durable lifecycle ручных команд WebUI → worker для
Surface Fleet 1..6.

Downgrade `0002` допускается только в пустой disposable БД. На импортированных
Stage 3 данных он намеренно не является lossless: `akashi_ap` отсутствует в
старом CHECK, а Numeric `asset` нельзя безопасно вернуть в bigint.
