# Инвентарь дока: canonical identity корабля

Stage 4 распознаёт только canonical-семью корабля, а не конкретную серверную
копию. Identity имеет вид `azur_lane_ship_group:<group_type>`.

Источник семантики — `ship_data_template.lua` из upstream-генератора
`dev_tools/ship_data_extractor.py`: верхнеуровневый `ship_id` описывает
конкретное состояние шаблона/прогресса, а `group_type` объединяет состояния
одной семьи и её retrofit. Реальные отдельные варианты (включая II/META)
имеют собственные группы. Одинаковое отображаемое имя EN у разных групп не
схлопывается: resolver возвращает `AMBIGUOUS`.

Во время работы читается компактный локальный каталог
`assets/ship/dock_identity_catalog.json`. В нём сохранены точные коммиты
источников, Git blob/SHA-256, путь исходного upstream asset и контракт отбора.
`Nürnberg META`, которой ещё нет в производном asset AzurPilot, извлекается
из точного EN Lua blob `fleet_tech_ship_class.lua`. Генератор до построения
записи проверяет коммит, blob, group/id/ships и имя EN. Коммит и blob
дополнительного источника также входят в provenance. Обновление:

```text
uv run python dev_tools/dock_identity_catalog.py --repo <checkout-with-upstream-commit> --supplemental-repo <AzurLaneLuaScripts-checkout>
uv run python dev_tools/dock_identity_catalog.py --repo <checkout-with-upstream-commit> --supplemental-repo <AzurLaneLuaScripts-checkout> --check
```

OCR использует только `PRESENT` slots Stage 3, вычисляет name ROI относительно
`slot.area`, объединяет белые и oath-pink glyphs и сохраняет raw OCR.
Разрешение выполняется последовательно: exact, явный truncated prefix, затем
fuzzy с минимальными score и runner-up margin. UI-суффикс `(Retrofit)` может
свести имя только к единственному exact base-name той же canonical group;
произвольные суффиксы, `META`, `II` и пунктуация не отбрасываются. Нехватка evidence остаётся
`AMBIGUOUS` или `UNRESOLVED`; принудительного best-match нет.
