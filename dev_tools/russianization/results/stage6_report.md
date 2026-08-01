# Stage 6 — полный русский active UI

Статус: **PASS**

Base SHA: `4764f66baeafb0dd2152599839afec739af8ab40`

## Архитектурные инварианты

- active runtime locale: `ru-RU`;
- foreign runtime fallback: `false`;
- UI locale linked to game server: `false`;
- исходные server/package/event metadata сохранены отдельно от UI locale;
- legacy locales и assets сохранены до Stage 9;
- runtime logs остаются предметом Stage 7–8.

## Итоговые метрики

- catalog keys: 4168;
- translated active UI: 2959;
- missing translation keys: 0;
- empty replacements: 0;
- unresolved active UI: 0;
- unreviewed English: 0;
- unreviewed CJK: 0;
- placeholder/markup mismatches: 0;
- raw translation keys rendered: 0;
- `Gui.Missing` rendered: 0.

## Точечные reviewed exceptions

- technical values: 543;
- proper names: 263;
- original metadata: 238;
- external content: 4.

Полный machine-readable реестр: `ui_translation_exceptions.json`. Каждая запись содержит
конкретный путь, ключ или устойчивый идентификатор, текст, категорию, причину, runtime-контекст,
этап и доказательство. Wildcard- и directory-wide исключения запрещены тестом Stage 6.

## Сохранённый объём

- legacy locale files: 5;
- assets: 10464;
- first-party log messages requiring later translation: 5066.

## Gate

`python -m dev_tools.stage6_ui_audit --check` работает только на чтение и сравнивает
пересчитанные артефакты с committed baseline. Любой missing key, обычный English/CJK без
точечной классификации, повреждённый placeholder/markup или direct UI literal завершает gate
с ошибкой.
