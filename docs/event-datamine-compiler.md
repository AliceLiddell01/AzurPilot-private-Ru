# Event Datamine, supplemental-данные и наблюдения UI

Подсистема разделяет четыре разных класса истины: структурные факты клиента из закреплённого ShareCfg snapshot, проверяемые статические дополнения из закреплённых внешних evidence, ограниченную runtime-policy generated-карт и динамические runtime-наблюдения конкретного профиля. Ни один слой не подменяет другой.

## Граница доверия ShareCfg

- `SourceSnapshot` требует server, repository и полный 40-символьный Git SHA.
- `ShareCfgLoader` разбирает только поддерживаемые Lua-таблицы и не исполняет Lua.
- Streamed ShareCfg читается через одноимённый `sharecfgdata` companion; отсутствие companion даёт структурированную ошибку.
- Parser, normalized model, validator/generator и atomic writer разделены.
- Неизвестный grid, effect или land-based mechanic блокирует production generation. Известное структурное исключение допускается только через проверяемые данные `compatibility_data/<event-slug>.json` с причиной, source evidence и ожидаемым эффектом.

ShareCfg остаётся первичным источником identity и связей: activity/task/map/shop/reward IDs, lifecycle, topology карты, spawn data и других фактов, которые клиент предоставляет структурно. Supplemental-слой не имеет права перепривязать такую identity по одному тексту или изображению.

Для siren ShareCfg доказывает только исходный `expedition.icon`. В `MapSpec` это хранится как `siren_source_icons`. Такое значение **не является** именем CV-шаблона AzurPilot и никогда автоматически не превращается в `MAP_SIREN_TEMPLATE`.

## Структурная совместимость без event-specific Python

Редкие доказанные расхождения между формой ShareCfg и поддерживаемой моделью компилятора хранятся как данные в `module/event_datamine/compatibility_data/`. Generic Python знает только схему и не содержит ветвлений по activity ID, map ID или имени события.

Compatibility snapshot содержит:

- версию схемы и self-digest;
- точную `event_id`;
- закреплённый repository и полный Git SHA evidence;
- список map-scoped исключений с причиной и source path;
- только заранее поддерживаемые типы структурных исключений.

Повреждённый digest, неизвестное поле, небезопасный путь либо неподдерживаемая форма данных отклоняются. Отсутствие файла для нового события означает отсутствие исключений, а не применение неявного fallback.

## Runtime-policy generated-карт

Факты исполнения карты, которых ShareCfg сам по себе не доказывает, хранятся рядом с generated package в `campaign/generated_event/<package>/runtime.json`. Это отдельный доверительный слой, а не продолжение ShareCfg-модели.

Runtime-policy schema v4 содержит:

- `generated_package` и `event_id`;
- доказанную UI-policy;
- evidence исходного runtime-наблюдения;
- отдельный `map_evidence` с repository и полным Git SHA;
- типизированные `runtime_maps`.

Для map policy разрешены только семантические поля, которые generic backend умеет проверять и безопасно проецировать. В текущей схеме это:

- `siren_recognition.templates`;
- `siren_recognition.boss_icon_small`;
- `stage_entry.one_time`;
- `stage_entry.has_mode_switch`.
- `boss_clear.strategy` со значениями из закрытого semantic allowlist.
- `camera_calibration` с доказанными camera nodes и spawn camera nodes.
- `detector_calibration` с типизированными line/swipe calibration и ограниченными map flags.
- `battle_plan` с декларативным enemy filter и siren/filter steps; произвольный Python-код не принимается.

Произвольные `MAP_*` ключи через JSON не принимаются. Generic resolver сам преобразует разрешённые семантические поля в ограниченный набор runtime-настроек.

Перед генерацией проверяются identity карты, `chapter_name`, evidence path, digest policy и существование каждого локального `TEMPLATE_SIREN_*` в canonical asset root. Если ShareCfg доказывает наличие siren, но отдельная runtime-policy не доказывает способ распознавания, карта получает `runtime_status = unsupported`, её module path остаётся пустым и Python-модуль не генерируется. Это fail-closed поведение не заменяется исходным ShareCfg icon и не использует угаданный шаблон. Для любой исполнимой карты отдельно обязателен `boss_clear.strategy`: `boss_refresh` из ShareCfg определяет только номер `battle_N` и не используется для выбора объекта флота. Отсутствие доказанной boss strategy даёт `boss_clear_missing` и также блокирует generation.

`source_status` и `runtime_status` имеют разный смысл: первый относится к структурной поддержке ShareCfg, второй — к безопасной исполнимости generated-карты. Runtime selector принимает только карты, для которых оба статуса `verified`.

Event-specific CV-шаблоны не регистрируются вручную в `module/map_detection/utils_assets.py`. Canonical registry создаётся генератором `module/template/assets.py` из фактически присутствующих файлов `assets/<server>/template/`.

## Raw artifact и runtime composite

Production registry читает generated [`index.json`](../module/event_datamine/data/index.json). Runtime identity для configured Event берётся из declarative binding `(server, selector) -> event_id`, а lifecycle используется отдельно для current-event задач и decommission. Несколько active/redemption production events без явного selector-контракта дают fail-closed ambiguity. Raw EN artifact хранится в [`data/production/`](../module/event_datamine/data/production/) и не выбирается по имени, activity ID или view class.

Raw artifact остаётся неизменяемым результатом компиляции ShareCfg: его digest, `provenance.revision` и `source_status` описывают только этот source snapshot. В частности, supplemental-данные не переписывают committed `production/*.json` и не маскируют ограничения самого datamine.

Для runtime `EventArtifactRegistry.resolve_current()` поверх raw artifact может применить проверенный supplemental snapshot. Полученный composite artifact строится заново через стандартный artifact envelope и имеет собственный digest и `composite_revision = sha256(base_revision + supplemental_digest)`. Исходный SHA сохраняется отдельно как `base_revision`/`source_revision`.

Это разделение важно для наблюдений: WebUI, EventShop scanner и OCR используют одну composite revision. Наблюдение, полученное для старого raw/supplemental набора, не может незаметно попасть в новый набор данных как будто источник не изменился. `EventArtifactRegistry.get()` и `resolve_current(..., supplemental=False)` по-прежнему дают raw artifact для сборки, аудита и regression tests.

Встроенный [`rose_tower.json`](../module/event_datamine/data/rose_tower.json) остаётся детерминированным historical golden/demo, а не production default и не live-сетевым cache.

## Event platform, overlay и controlled retirement

Event-система разделена по ownership:

- **platform** — generic registry/resolver/compiler/runtime routing, `EventBase`, generic campaign orchestration и validators; этот слой не знает identity текущего event;
- **overlay** — production artifact, selector binding, supplemental/compatibility data и generated campaign package конкретного события;
- **assets** — reusable static files в `assets/...`; они не принадлежат lifecycle события только потому, что впервые понадобились в нём.

Нормальная смена события не требует правки `CampaignRun`, resolver или generic Event classes: новый artifact и selector binding подключаются через тот же registry contract. Builder сохраняет `generated_package` в metadata artifact, чтобы overlay можно было вывести из эксплуатации без угадывания package по event identity.

Lifecycle retirement выполняется только после `shop_end`, когда `artifact_lifecycle(...) == "expired"`. `farm_end` завершает farming, но не redemption/shop период. Обычный runtime никогда не удаляет repository data и не меняет Git checkout по времени.

Явная repository/build операция `module.event_datamine.retirement.retire_event_overlay(...)`:

1. требует однозначный production `event_id` и явно переданный `now`;
2. fail-closed отклоняет `upcoming`, `active` и `redemption`;
3. удаляет только artifact, его selector bindings, supplemental/compatibility data и доказанно принадлежащий ему generated package;
4. пересобирает `index.json` и `assets.json`;
5. проверяет, что другой event не использует тот же generated package;
6. **не удаляет static assets** и не обходит неизвестные source files рекурсивной очисткой.

Повторный retirement уже отсутствующего event намеренно fail-closed. Это не runtime recovery и не compatibility shim.

`index.json` — generated metadata. Legacy schema v1 не мигрируется постоянным converter-слоем: при несовместимой версии нужно удалить только устаревший generated `index.json` и повторить штатную Event-сборку. Production artifacts и static assets для этого удалять не требуется.

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
  --source-root C:\\path\\to\\AzurLaneLuaScripts `
  --server EN `
  --campaign-selector <event-selector> `
  --revision <full-sha> `
  --current `
  --now <server-local-iso-datetime> `
  --output-root .\\module\\event_datamine\\data `
  --maps-output .\\campaign\\generated_event `
  --overwrite
```

Для воспроизводимых integration tests минимальная source-derived fixture извлекается отдельно; manifest сохраняет source identity, record counts и SHA-256 всех реально записанных таблиц:

```powershell
uv run python -m dev_tools.event_datamine_fixture `
  --source-root C:\\path\\to\\AzurLaneLuaScripts `
  --server EN `
  --repository AzurLaneTools/AzurLaneLuaScripts `
  --revision <full-sha> `
  --now <server-local-iso-datetime> `
  --output .\\tests\\fixtures\\event_datamine\\current_en
```

Исторический extractor с явным activity ID остаётся инструментом golden/regression. `--maps-output` включается отдельно. Map modules не генерируются, если structural artifact содержит blocking findings или если обязательная runtime-policy карты не подтверждена.

Перед сборкой нового события runtime-факты, отсутствующие в ShareCfg, сначала заносятся в `campaign/generated_event/<package>/runtime.json` с evidence и новым digest. Если новый Event не требует runtime-фактов сверх ShareCfg, пустые event-specific ветви в Python не создаются.

После обновления raw source revision старый supplemental обязан пройти `base_contract` заново. Если source revision изменился, его нельзя механически «переподписать»: сначала нужно сверить supplemental facts с новым snapshot/evidence, после чего обновить data и digest.

## Известные границы

- Supplemental snapshot не является автоматическим web scraper output: факты, извлечённые из визуальной страницы, должны быть закреплены, проверены и явно занесены в типизированные data records.
- Runtime-policy карты также не является доверенным произвольным конфигом: новые виды поведения требуют сначала добавить generic тип политики и проверки, а затем данные конкретного события.
- В репозитории нет надёжного scanner-а отдельных mission rows с task identity. Поэтому mission completion не автоматизируется даже если статическая mission taxonomy известна.
- Звёзды, фактическое число прохождений, Clearing Mode и прочее пользовательское progression-state не выводятся из wiki/static farm metadata.
- Неименованные в evidence icon-only drop identities не угадываются. Они остаются вне machine-readable supplemental до появления надёжного источника.
- Generated/local display asset не подменяет structural reward/shop identity: сначала должна быть доказана соответствующая game identity.
- Legacy EventShop automation поддерживает только строки с доказанным runtime filter identity. Неизвестные товары остаются видимыми, но не включаются в автоматизацию.
