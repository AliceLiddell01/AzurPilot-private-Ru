# Event Datamine, supplemental-данные и наблюдения UI

Подсистема разделяет три разных класса истины: структурные факты клиента из закреплённого ShareCfg snapshot, проверяемые статические дополнения из закреплённых внешних evidence и динамические runtime-наблюдения конкретного профиля. Ни один слой не подменяет другой.

## Граница доверия ShareCfg

- `SourceSnapshot` требует server, repository и полный 40-символьный Git SHA.
- `ShareCfgLoader` разбирает только поддерживаемые Lua-таблицы и не исполняет Lua.
- Streamed ShareCfg читается через одноимённый `sharecfgdata` companion; отсутствие companion даёт структурированную ошибку.
- Parser, normalized model, validator/generator и atomic writer разделены.
- Неизвестный grid, effect или land-based mechanic блокирует production generation. Известное исключение допускается только через `CompatibilityPatch` с причиной, source evidence и ожидаемым эффектом.

ShareCfg остаётся первичным источником identity и связей: activity/task/map/shop/reward IDs, lifecycle, topology карты, spawn data и других фактов, которые клиент предоставляет структурно. Supplemental-слой не имеет права перепривязать такую identity по одному тексту или изображению.

## Raw artifact и runtime composite

Production registry читает generated [`index.json`](../module/event_datamine/data/index.json), выбирает единственный active или redemption artifact по server-local lifecycle и исключает роль `demo`. Несколько подходящих событий дают fail-closed ambiguity. Raw EN artifact хранится в [`data/production/`](../module/event_datamine/data/production/) и не выбирается по имени, activity ID или view class.

Raw artifact остаётся неизменяемым результатом компиляции ShareCfg: его digest, `provenance.revision` и `source_status` описывают только этот source snapshot. В частности, supplemental-данные не переписывают committed `production/*.json` и не маскируют ограничения самого datamine.

Для runtime `EventArtifactRegistry.resolve_current()` поверх raw artifact может применить проверенный supplemental snapshot. Полученный composite artifact строится заново через стандартный artifact envelope и имеет собственный digest и `composite_revision = sha256(base_revision + supplemental_digest)`. Исходный SHA сохраняется отдельно как `base_revision`/`source_revision`.

Это разделение важно для наблюдений: WebUI, EventShop scanner и OCR используют одну composite revision. Наблюдение, полученное для старого raw/supplemental набора, не может незаметно попасть в новый набор данных как будто источник не изменился. `EventArtifactRegistry.get()` и `resolve_current(..., supplemental=False)` по-прежнему дают raw artifact для сборки, аудита и regression tests.

Встроенный [`rose_tower.json`](../module/event_datamine/data/rose_tower.json) остаётся детерминированным historical golden/demo, а не production default и не live-сетевым cache.

## Проверяемый supplemental-слой

Supplemental snapshot лежит отдельно от каталога raw artifacts: `module/event_datamine/supplemental_data/<event-slug>/`. Он является данными, а не behavioral-кодом: production-модули не содержат ветвлений по текущему activity/map/task ID.

Manifest содержит:

- schema version и self-digest;
- точную `event_id`;
- `base_contract`: server, activity ID, имя, полный ShareCfg revision и ожидаемые количества maps/shop rows/milestones;
- SHA-256 исходных пользовательских evidence archives и метаданные закреплённой страницы;
- task classification, shop name overrides и generic resource display identities;
- cross-source verification для shop/milestones;
- farm mechanics/rules, дополнительные миссии и явно сохранённые source conflicts.

Большие map records вынесены в отдельные JSON parts. Имена parts проходят allowlist-проверку, пути не могут выйти из каталога event snapshot, а digest считается по уже собранному документу, поэтому изменение любого part без пересчёта manifest отвергается.

Перед применением supplemental resolver проверяет его против raw EventSpec. Проверяются как минимум identity события и source revision, map inventory/chapter names, task names/PT rewards, shop row identity/price/stock, полный shop buyout oracle и точная последовательность milestone thresholds. Если supplemental устарел, повреждён или относится к другому source snapshot, он **не** применяется: runtime сохраняет доказанный raw artifact и добавляет `supplemental_rejected`, вместо тихого смешивания несовместимых данных.

Текущий pinned snapshot дополнительно сохраняет известный конфликт источника: ShareCfg arithmetic и текстовая note дают полный buyout `138550`, а отдельный banner страницы показывает `138750`. Конфликт остаётся `info` evidence; resolver не выбирает противоречащее значение молча.

## PT, миссии и farm metadata

`PtSourceSpec` из raw ShareCfg по возможности классифицируется структурно. Когда ShareCfg доказывает task identity и размер PT, но не даёт достаточно данных для семантической категории, supplemental может присвоить `daily`/`one_time` и scope только после проверки точного task ID, исходного текста и PT reward.

Map PT также приходит из event-specific data, а не из глобальных констант. Runtime composite поддерживает одновременно:

- базовую награду повторного clear;
- отдельную daily-first-clear награду с явными `base_points`, `bonus_points`, `multiplier` и `daily_limit`;
- daily-only SP reward;
- карты, явно не выдающие event PT.

Множитель первого прохождения не является универсальным правилом движка. Он хранится в supplemental snapshot конкретного события и может отличаться у других событий/перезапусков.

Farm metadata хранит только доказанные статические факты: mode/title/description, PT, unlock relations, clear/three-star rewards, enemy/boss levels, battles, airspace, fleet/stat restrictions, Specialized Core drops, boss-only drops, drop families, D-map oil caps/ranges и другие явно зафиксированные значения. Диапазон монет не превращается в выдуманное scalar observation.

Runtime-проекция заполняет статический `points`/`oil per run` только когда соответствующего runtime-наблюдения нет. Подробные oil caps остаются отдельной структурой. Наличие static metadata не меняет `observation_status`, не создаёт фиктивные звёзды, completion state или число прохождений.

Дополнительные event missions, которые не дают PT, могут храниться отдельно от `pt_sources`; их наличие не означает, что бот умеет определять выполнение миссии.

## Состояние пользователя и EventShop

`config/state/event_user_state/` хранит только пользовательскую политику: желаемые количества покупок и связанные настройки. Старые ручные current PT и статусы recurring sources мигрируются в `legacy_debug_evidence`; они не становятся production truth.

`config/state/event_observation/` хранит типизированные runtime-наблюдения отдельно для профиля, события, сервера и composite source revision. Запись атомарна, повреждённый JSON переносится в `*.corrupt-*`, старые наблюдения не превращаются в нули. Fixture/replay evidence разрешено только явным тестовым флагом и по умолчанию отвергается production reader.

После полного `EventShopClerk.scan_all()` runtime rows сопоставляются с catalog rows только по уникальному exact key. Для `unmatched`, `ambiguous` и некорректных счётчиков purchased остаётся неизвестным. Любая покупка инвалидирует snapshot до следующего полного сканирования. Желаемое количество остаётся независимой пользовательской политикой.

Runtime не обращается к Wiki или другим внешним сайтам. Закреплённый supplemental snapshot является локальным build/runtime input с проверяемой provenance; сеть не участвует в разрешении текущего события.

## Developer CLI

Обновление raw current EN artifact не требует знать новый activity ID. Команда проверяет HEAD source checkout, структурно обнаруживает current major event, компилирует artifact и карты, затем пересобирает registry и local-only asset catalog:

```powershell
uv run python -m dev_tools.event_datamine_build `
  --source-root C:\path\to\AzurLaneLuaScripts `
  --server EN `
  --revision <full-sha> `
  --current `
  --now <server-local-iso-datetime> `
  --output-root .\module\event_datamine\data `
  --maps-output .\campaign\generated_event `
  --overwrite
```

Для воспроизводимых integration tests минимальная source-derived fixture извлекается отдельно; manifest сохраняет source identity, record counts и SHA-256 всех реально записанных таблиц:

```powershell
uv run python -m dev_tools.event_datamine_fixture `
  --source-root C:\path\to\AzurLaneLuaScripts `
  --server EN `
  --repository AzurLaneTools/AzurLaneLuaScripts `
  --revision <full-sha> `
  --now <server-local-iso-datetime> `
  --output .\tests\fixtures\event_datamine\current_en
```

Исторический extractor с явным activity ID остаётся инструментом golden/regression. `--maps-output` включается отдельно. Map modules не генерируются, если artifact содержит blocking findings. Generated config включает только факты карты; произвольная runtime policy не добавляется.

После обновления raw source revision старый supplemental обязан пройти `base_contract` заново. Если source revision изменился, его нельзя механически «переподписать»: сначала нужно сверить supplemental facts с новым snapshot/evidence, после чего обновить data и digest.

## Известные границы

- Supplemental snapshot не является автоматическим web scraper output: факты, извлечённые из визуальной страницы, должны быть закреплены, проверены и явно занесены в типизированные data records.
- В репозитории нет надёжного scanner-а отдельных mission rows с task identity. Поэтому mission completion не автоматизируется даже если статическая mission taxonomy известна.
- Звёзды, фактическое число прохождений, Clearing Mode и прочее пользовательское progression-state не выводятся из wiki/static farm metadata.
- Неименованные в evidence icon-only drop identities не угадываются. Они остаются вне machine-readable supplemental до появления надёжного источника.
- Generated/local display asset не подменяет structural reward/shop identity: сначала должна быть доказана соответствующая game identity.
- Legacy EventShop automation поддерживает только строки с доказанным runtime filter identity. Неизвестные товары остаются видимыми, но не включаются в автоматизацию.
