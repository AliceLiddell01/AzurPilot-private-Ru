# Stage 4 — аудит русификации и карта зависимостей

## Границы

Этот отчёт создан read-only аудитором после перехода на единый runtime locale `ru-RU`. Legacy locale-файлы и assets не удалялись; game server, OCR и package options сохранены.

## Воспроизводимость

```text
uv run python -m dev_tools.russianization_audit --write
uv run python -m dev_tools.russianization_audit --check
```

`--check` генерирует результаты во временном каталоге и побайтово сравнивает их с committed baseline, не изменяя tracked tree.

## Итоговые counts

| Метрика | Значение |
|---|---:|
| Tracked files scanned | 10962 |
| Text files scanned | 2114 |
| Locale files | 6 |
| UI string entries | 23334 |
| UI translation required | 21348 |
| First-party/direct log entries | 5753 |
| Log translation required | 5088 |
| Asset entries | 10463 |
| Asset bytes represented | 585450747 |
| EN/Global required candidates | 3670 |
| Manual review assets | 1943 |
| Probable delete candidates | 1231 |
| Confirmed delete candidates | 0 |

Source fingerprint: `6887c76429d13ed8350cfb182e24210c0f2c771beef9216ee92283bd2c8589cd`

## Runtime locale architecture

- Active runtime locales: `['ru-RU']`
- Legacy inactive locale files: `['en-US', 'ja-JP', 'zh-CN', 'zh-MIAO', 'zh-TW']`
- Foreign runtime fallback: `False`
- UI locale linked to game server: `False`
- Event-name source: `en`

## Locale inventory

| Locale | Path | Runtime status | String keys |
|---|---|---|---:|
| `en-US` | `module/config/i18n/en-US.json` | legacy_inactive_locale_file | 4166 |
| `ja-JP` | `module/config/i18n/ja-JP.json` | legacy_inactive_locale_file | 4166 |
| `ru-RU` | `module/config/i18n/ru-RU.json` | active_runtime_locale | 4166 |
| `zh-CN` | `module/config/i18n/zh-CN.json` | legacy_inactive_locale_file | 4166 |
| `zh-MIAO` | `module/config/i18n/zh-MIAO.json` | legacy_inactive_locale_file | 4166 |
| `zh-TW` | `module/config/i18n/zh-TW.json` | legacy_inactive_locale_file | 4166 |

Locale files with missing keys against union: **0**.

## Locale / server / OCR dependency map

| Связь | Runtime state | Evidence entries |
|---|---|---:|
| UI locale → translation loader | активна | 30 |
| translation loader → deploy Language | разорвана | 15 |
| deploy Language → config generator | разорвана | 30 |
| config generator → event-name source | разорвана | 30 |
| event-name source → game server | активна | 30 |
| game server → OCR profile/model | активна | 30 |
| OCR profile/model → package/server options | активна | 30 |
| package/server options → assets | активна | 30 |

Архитектурный вывод: UI locale отделён от deploy compatibility value, event-name source, game server, OCR profile и package options. Server-specific связи ниже по цепочке сохранены.

## Пользовательские строки

Разбиение по подсистемам: `{'scheduler_and_config': 22905, 'webui_and_process_lifecycle': 296, 'deploy_and_dependencies': 114, 'other': 10, 'tests': 9}`.

Inventory содержит путь, строку/ключ, источник, текст, language guess, classification, runtime visibility, generated flag и решение о необходимости перевода. Эвристика не считает любой ASCII-текст пользовательским английским: identifiers, paths, commands и technical values отделены.

## First-party логи

Разбиение по подсистемам: `{'game_tasks': 2652, 'operation_siren': 815, 'device_adb_emulator': 638, 'campaign_combat_fleet': 569, 'deploy_and_dependencies': 320, 'webui_and_process_lifecycle': 320, 'other': 274, 'ocr': 107, 'scheduler_and_config': 45, 'tests': 9, 'screenshot_and_control': 4}`.

Сырые stdout/stderr/traceback отмечаются отдельно и должны сохраняться без перевода. В будущих Stage русифицируется только first-party контекст вокруг них.

## Assets

Decision counts: `{'confirmed_keep': 7784, 'needs_manual_review': 712, 'probable_delete_candidate': 1231, 'probable_keep': 736}`.

Scope counts: `{'cn': 3943, 'en': 1673, 'jp': 1457, 'multi_server': 278, 'shared': 109, 'tw': 1445, 'unknown': 1558}`.

`confirmed_delete_candidate` намеренно не присваивается на основании имени, CJK или суффикса. Наличие server marker без runtime evidence даёт максимум `probable_delete_candidate` и `manual_review_required: true`.

### Первые probable delete candidates

| Path | Scope | Type | Confidence |
|---|---|---|---:|
| `assets/cn/awaken/AWAKEN_FINISH.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/combat/BATTLE_PREPARATION.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/combat/BATTLE_STATUS_A.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/combat/BATTLE_STATUS_B.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/combat/BATTLE_STATUS_C.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/combat/BATTLE_STATUS_D.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/combat/EMERGENCY_REPAIR_AVAILABLE.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/combat/EXP_INFO_A.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/combat/EXP_INFO_B.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/combat/EXP_INFO_C.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/combat/EXP_INFO_D.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/combat/EXP_INFO_S.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/combat/GET_ITEMS_1.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/combat/GET_ITEMS_1_RYZA.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/combat/GET_ITEMS_2.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/combat/GET_ITEMS_3.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/combat/GET_SHIP.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/combat/OPTS_INFO_D.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/combat_ui/PAUSE.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/combat_ui/QUIT.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/event_hospital/GET_CLUE.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/freebies/FREE_SUPPLY_PACK.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/guild/BATTLE_STATUS_CF.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/guild/EXP_INFO_CF.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/guild/GUILD_MISSION_SELECT.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/guild/GUILD_OPERATIONS_SOLOMON.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/island_daily_interact/WEEKLY_PHOTO_TASK_CHECK.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/island_daily_order/DAILY_ORDER_CHALLENGE_EASY_SPECIAL_CHECK.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/island_daily_order/DAILY_ORDER_URGENT_SPECIAL_CHECK.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/island_farm/ISLAND_FARM_POST2.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/island_farm/ISLAND_FARM_POST3.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/island_farm/ISLAND_FARM_POST4.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/island_farm/ISLAND_NURSERY_POST1.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/island_farm/ISLAND_NURSERY_POST2.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/island_farm/ISLAND_ORCHARD_POST1.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/island_farm/ISLAND_ORCHARD_POST2.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/island_farm/ISLAND_ORCHARD_POST3.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/island_farm/ISLAND_ORCHARD_POST4.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/island_grill/ISLAND_GRILL_POST1.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/island_grill/ISLAND_GRILL_POST2.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/island_juu_coffee/ISLAND_JUU_COFFEE_POST1.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/island_juu_coffee/ISLAND_JUU_COFFEE_POST2.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/island_juu_eatery/ISLAND_JUU_EATERY_POST1.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/island_juu_eatery/ISLAND_JUU_EATERY_POST2.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/island_manufacture/ISLAND_ELECTRONIC_PROCESSING_POST1.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/island_manufacture/ISLAND_ELECTRONIC_PROCESSING_POST2.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/island_manufacture/ISLAND_HANDMADE_POST1.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/island_manufacture/ISLAND_HANDMADE_POST2.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/island_manufacture/ISLAND_INDUSTRIAL_POST1.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
| `assets/cn/island_manufacture/ISLAND_INDUSTRIAL_POST2.BUTTON.png` | cn | recognition screenshot/template | 0.55 |

Committed `asset_manifest.json` содержит агрегаты и review/delete findings с ограниченными evidence samples. Полный manifest воспроизводится командой из файла и сверяется по SHA-256. Решения: `asset_decisions.json`. Ресурсы EN/shared: `en_global_required.json`.

## Доказательные ограничения

- Статические ссылки извлекаются из tracked UTF-8 text files и отличаются от dynamic loader evidence.
- Glob/path-convention/importlib/getattr/listdir evidence помечается как dynamic и запрещает автоматический вывод об удалении.
- Тестовые ссылки отделены от runtime/generated references.
- Binary semantic contents не распознаются; спорные ресурсы остаются manual review.
- Реальная необходимость OCR fallback окончательно подтверждается только EN/Global runtime smoke на Stage 9.

## Следующие этапы

Stage 5 использует dependency map и migration plan; Stage 6 — UI inventory и terminology; Stage 7–8 — log inventory; Stage 9 — asset decisions и EN/Global keep list. Stage 4 ничего из этого не реализует.
