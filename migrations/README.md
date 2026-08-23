# Alembic-миграции

Alembic является единственным механизмом изменения PostgreSQL schema.
Revision-файлы не должны читать production config или выполнять runtime wiring.

`AZURPILOT_POSTGRES_DISPOSABLE=1` является только явным подтверждением
оператора, а не доказательством изоляции. Destructive downgrade выполняется
только в отдельной test-only БД с credentials, недоступными production.
