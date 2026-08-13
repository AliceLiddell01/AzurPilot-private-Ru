# Event Datamine Compiler

Stage 3 переносит объективные факты события из ручного EventPlan в закреплённый локальный artifact, собранный из расшифрованного `AzurLaneTools/AzurLaneLuaScripts` snapshot.

## Граница доверия

- `SourceSnapshot` требует server, repository и полный 40-символьный Git SHA.
- `ShareCfgLoader` разбирает только поддерживаемые Lua-таблицы и не исполняет Lua.
- Streamed ShareCfg читается через одноимённый `sharecfgdata` companion; отсутствие companion даёт структурированную ошибку.
- Parser, normalized model, validator/generator и atomic writer разделены.
- Неизвестный grid, effect или land-based mechanic блокирует production generation. Известное исключение допускается только через `CompatibilityPatch` с причиной, source evidence и ожидаемым эффектом.

## Локальный artifact и состояние пользователя

Встроенный [`rose_tower.json`](../module/event_datamine/data/rose_tower.json) — детерминированный historical golden, а не live-сетевой cache. Его provenance закрепляет repository/revision/server/activity ID. Digest проверяется до чтения, а writer заменяет файл атомарно только после validation.

`EventSpec` содержит факты игры: identity, даты, карты, shop rows, валюты, milestones, PT sources, asset references, provenance и findings. `config/state/event_user_state/` отдельно хранит только пользовательскую политику: количества покупок, статусы recurring sources и runtime progress. Старый Stage 2 plan мигрируется fail-closed: stable shop IDs сохраняются, ручные source facts остаются как `legacy_unverified` и не становятся активным provider.

Обычный Event UI не позволяет добавлять или редактировать source facts и не использует Wiki/BWiki. Текущий PT берётся только из свежей Dashboard/OCR записи. `PtLimit`, Scheduler и EventShop меняются лишь существующими явными действиями пользователя; небезопасный shop selector сохраняет fail-closed поведение.

## Developer CLI

```powershell
uv run python -m dev_tools.map_extractor `
  --source-root C:\path\to\AzurLaneLuaScripts `
  --server EN `
  --revision <full-sha> `
  --activity-id 5941 `
  --artifact .\event.json
```

`--maps-output` включается отдельно. Map modules не генерируются, если artifact содержит blocking findings. Generated config включает только факты карты; произвольная runtime policy не добавляется.

## Известные границы Stage 3

- Точное PT за повторный clear карты отсутствует в исследованной ShareCfg family и помечено `map_pt_amount_unavailable`.
- Для ship/furniture и части resource rewards сохранены game IDs, но финальный asset resolver/card renderer относится к Stage 4.
- Свежий CN snapshot проверен как непроизводственный probe; production CN→EN reconciliation и переключение live event не выполнялись.
