# Stage 4 — аудит русификации и карта зависимостей

## Границы

Этот отчёт создан read-only аудитором. Runtime locale, язык по умолчанию, WebUI, логи, OCR-модели, server logic и существующие assets не изменялись и не удалялись.

## Воспроизводимость

```text
uv run python -m dev_tools.russianization_audit --write
uv run python -m dev_tools.russianization_audit --check
```

`--check` генерирует результаты во временном каталоге и побайтово сравнивает их с committed baseline, не изменяя tracked tree.

## Итоговые counts

| Метрика | Значение |
|---|---:|
| Tracked files scanned | 10958 |
| Text files scanned | 2106 |
| Locale files | 5 |
| UI string entries | 19621 |
| UI translation required | 17907 |
| First-party/direct log entries | 5714 |
| Log translation required | 5061 |
| Asset entries | 10458 |
| Asset bytes represented | 585418955 |
| EN/Global required candidates | 3695 |
| Manual review assets | 1930 |
| Probable delete candidates | 1228 |
| Confirmed delete candidates | 0 |

Source fingerprint: `1899e0d5809f67a0d8b6c07aa36727377a43ab30ad19d2df5811df1278ff74b7`

## Locale inventory

| Locale | Path | String keys |
|---|---|---:|
| `en-US` | `module/config/i18n/en-US.json` | 4166 |
| `ja-JP` | `module/config/i18n/ja-JP.json` | 4166 |
| `zh-CN` | `module/config/i18n/zh-CN.json` | 4166 |
| `zh-MIAO` | `module/config/i18n/zh-MIAO.json` | 4166 |
| `zh-TW` | `module/config/i18n/zh-TW.json` | 4166 |

Locale files with missing keys against union: **0**.

## Locale / server / OCR dependency map

| Связь | Фактически найдена | Evidence entries |
|---|---|---:|
| UI locale → translation loader | да | 30 |
| translation loader → deploy Language | да | 15 |
| deploy Language → config generator | да | 30 |
| config generator → event-name source | да | 25 |
| event-name source → game server | да | 28 |
| game server → OCR profile/model | да | 25 |
| OCR profile/model → package/server options | да | 30 |
| package/server options → assets | да | 30 |

Архитектурный вывод: текущие связи должны разрываться только в Stage 5, сохраняя game server, event-name source, OCR profile и package options независимо от UI locale.

## Пользовательские строки

Разбиение по подсистемам: `{'scheduler_and_config': 19200, 'webui_and_process_lifecycle': 301, 'deploy_and_dependencies': 101, 'other': 10, 'tests': 9}`.

Inventory содержит путь, строку/ключ, источник, текст, language guess, classification, runtime visibility, generated flag и решение о необходимости перевода. Эвристика не считает любой ASCII-текст пользовательским английским: identifiers, paths, commands и technical values отделены.

## First-party логи

Разбиение по подсистемам: `{'game_tasks': 2652, 'operation_siren': 815, 'device_adb_emulator': 638, 'campaign_combat_fleet': 569, 'webui_and_process_lifecycle': 314, 'deploy_and_dependencies': 293, 'other': 269, 'ocr': 107, 'scheduler_and_config': 44, 'tests': 9, 'screenshot_and_control': 4}`.

Сырые stdout/stderr/traceback отмечаются отдельно и должны сохраняться без перевода. В будущих Stage русифицируется только first-party контекст вокруг них.

## Assets

Decision counts: `{'confirmed_keep': 7799, 'needs_manual_review': 702, 'probable_delete_candidate': 1228, 'probable_keep': 729}`.

Scope counts: `{'cn': 3943, 'en': 1673, 'jp': 1457, 'multi_server': 278, 'shared': 109, 'tw': 1445, 'unknown': 1553}`.

`confirmed_delete_candidate` намеренно не присваивается на основании имени, CJK или суффикса. Наличие server marker без runtime evidence даёт максимум `probable_delete_candidate` и `manual_review_required: true`.

### Первые probable delete candidates

| Path | Scope | Type | Confidence |
|---|---|---|---:|
| `assets/cn/awaken/AWAKEN_FINISH.BUTTON.png` | cn | recognition screenshot/template | 0.55 |
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
| `assets/cn/island_manufacture/ISLAND_WOOD_PROCESSING_POST1.BUTTON.png` | cn | recognition screenshot/template | 0.55 |

Committed `asset_manifest.json` содержит агрегаты и review/delete findings с ограниченными evidence samples. Полный manifest воспроизводится командой из файла и сверяется по SHA-256. Решения: `asset_decisions.json`. Ресурсы EN/shared: `en_global_required.json`.

## Доказательные ограничения

- Статические ссылки извлекаются из tracked UTF-8 text files и отличаются от dynamic loader evidence.
- Glob/path-convention/importlib/getattr/listdir evidence помечается как dynamic и запрещает автоматический вывод об удалении.
- Тестовые ссылки отделены от runtime/generated references.
- Binary semantic contents не распознаются; спорные ресурсы остаются manual review.
- Реальная необходимость OCR fallback окончательно подтверждается только EN/Global runtime smoke на Stage 9.

## Следующие этапы

Stage 5 использует dependency map и migration plan; Stage 6 — UI inventory и terminology; Stage 7–8 — log inventory; Stage 9 — asset decisions и EN/Global keep list. Stage 4 ничего из этого не реализует.
