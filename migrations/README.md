# Alembic-миграции

Alembic является единственным механизмом изменения PostgreSQL schema.
Revision-файлы не должны читать production config или выполнять runtime wiring.

`AZURPILOT_POSTGRES_DISPOSABLE=1` является только явным подтверждением
оператора, а не доказательством изоляции. Destructive downgrade выполняется
только в отдельной test-only БД с credentials, недоступными production.
Alembic entry point также требует отдельного точного подтверждения target через
`AZURPILOT_POSTGRES_DISPOSABLE_HOST`, `_PORT`, `_DATABASE` и `_USER`.

Текущий единственный head — `0008_dorm_morale_idempotency`. Он переводит
уникальность Dorm scan idempotency key на scope `app_instance + caller key`,
чтобы новые scan rows сохраняли semantic caller key без namespaced rehash.
Существующие Stage 2 rows сохраняют необратимые legacy SHA-256 keys как opaque
значения: исходный caller key из них достоверно восстановить невозможно.
Следствие этой необратимости: повтор операции, впервые записанной до `0008`,
нельзя достоверно сопоставить со старой SHA-256 строкой по исходному caller key;
первый такой post-upgrade retry создаст новую semantic-key запись. Старую строку
миграция не переписывает и не пытается восстанавливать догадкой.

Downgrade `0002` допускается только в пустой disposable БД. На импортированных
Stage 3 данных он намеренно не является lossless: `akashi_ap` отсутствует в
старом CHECK, а Numeric `asset` нельзя безопасно вернуть в bigint.

Downgrade `0008` fail-closed запрещён, если после upgrade одинаковый caller key
уже используется несколькими app instances: старый глобальный UNIQUE нельзя
восстановить без потери данных.
