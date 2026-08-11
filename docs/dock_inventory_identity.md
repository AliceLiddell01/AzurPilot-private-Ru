# Dock Inventory: canonical ship identity

Stage 4 распознаёт только canonical ship family, а не конкретную серверную
копию корабля. Identity имеет вид `azur_lane_ship_group:<group_type>`.

Источник semantics — `ship_data_template.lua` из upstream-генератора
`dev_tools/ship_data_extractor.py`: top-level `ship_id` описывает конкретное
состояние template/progression, а `group_type` объединяет состояния одной
семьи и её retrofit. Реальные отдельные варианты (включая II/META) имеют
собственные группы. Одинаковое EN display name у разных групп не схлопывается:
resolver возвращает `AMBIGUOUS`.

Runtime читает компактный offline-каталог
`assets/ship/dock_identity_catalog.json`. В нём сохранены точные source commit,
Git blob/SHA-256, путь исходного upstream asset и контракт отбора. Новая
`Nürnberg META`, которой ещё нет в derived asset AzurPilot, извлекается
генератором из точного EN Lua blob `fleet_tech_ship_class.lua`: generator сам
проверяет commit, blob, group/id/ships и EN name до построения записи. Commit и
blob дополнительного источника также входят в provenance. Обновление:

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
