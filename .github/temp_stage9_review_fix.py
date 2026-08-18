from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: ожидалось одно совпадение, найдено {count}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def append_once(path: str, marker: str, addition: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if addition.strip() in text:
        raise SystemExit(f"{path}: добавляемый блок уже присутствует")
    if text.count(marker) != 1:
        raise SystemExit(f"{path}: marker должен встречаться один раз")
    target.write_text(text.replace(marker, marker + addition, 1), encoding="utf-8")


# Документация: переводим только описательную прозу, сохраняя имена API/модулей.
replace_once(
    ".codex/context/01-PROJECT-MAP.md",
    "- `module/event_datamine/` — безопасный ShareCfg loader, structural current-event discovery, normalized `EventSpec`, lifecycle registry, canonical local asset catalog, map compiler/generator, provenance и атомарные локальные artifacts. Historical artifacts с ролью `demo` не участвуют в production resolution.",
    "- `module/event_datamine/` — безопасная загрузка ShareCfg, структурное обнаружение текущего события, нормализованный `EventSpec`, реестр жизненного цикла, канонический локальный каталог ассетов, компилятор и генератор карт, происхождение данных и атомарные локальные артефакты. Исторические артефакты с ролью `demo` не участвуют в выборе production-события.",
)

# Artifact: явное пустое metadata является частью envelope-контракта.
replace_once(
    "module/event_datamine/artifact.py",
    '    if metadata:\n        result["metadata"] = _normalize_json(metadata)\n',
    '    if metadata is not None:\n        result["metadata"] = _normalize_json(metadata)\n',
)
append_once(
    "tests/test_event_datamine_artifact.py",
    '    with pytest.raises(ValueError, match="Дублирующийся JSON key"):\n        build_artifact({"id": "en:1", "nested": {1: "a", "1": "b"}})\n',
    '''\n\n\ndef test_artifact_preserves_explicit_empty_metadata():\n    artifact = build_artifact({"id": "en:1"}, metadata={})\n\n    assert artifact["metadata"] == {}\n    assert artifact["digest"] == artifact_digest(artifact)\n''',
)

# Compatibility data: единый импорт digest и точные ошибки id.
replace_once(
    "module/event_datamine/patches.py",
    "from functools import lru_cache\nfrom pathlib import Path, PurePosixPath\n",
    "from functools import lru_cache\nfrom hashlib import sha256\nfrom pathlib import Path, PurePosixPath\n",
)
replace_once(
    "module/event_datamine/patches.py",
    'def compatibility_digest(data: dict[str, Any]) -> str:\n    from hashlib import sha256\n\n    clean = dict(data)\n',
    'def compatibility_digest(data: dict[str, Any]) -> str:\n    clean = dict(data)\n',
)
replace_once(
    "module/event_datamine/patches.py",
    '        patch_id = str(raw.get("id") or "").strip()\n        if not patch_id or patch_id in ids:\n            raise CompatibilityDataError(f"Неуникальный compatibility patch id: {patch_id!r}")\n',
    '        patch_id = str(raw.get("id") or "").strip()\n        if not patch_id:\n            raise CompatibilityDataError("Compatibility patch не содержит id")\n        if patch_id in ids:\n            raise CompatibilityDataError(\n                f"Неуникальный compatibility patch id: {patch_id!r}"\n            )\n',
)
append_once(
    "tests/test_event_compatibility_data.py",
    '    with pytest.raises(CompatibilityDataError, match="Digest"):\n        load_compatibility_data("en:1", root=root)\n',
    '''\n\n\ndef _compatibility_snapshot(*patches):\n    data = {\n        "compatibility_schema_version": 1,\n        "event_id": "en:1",\n        "evidence": {\n            "repository": "example/repository",\n            "revision": "1" * 40,\n        },\n        "patches": list(patches),\n    }\n    data["digest"] = compatibility_digest(data)\n    return data\n\n\ndef _compatibility_patch(patch_id: str, *, map_id: int = 10):\n    return {\n        "id": patch_id,\n        "map_id": map_id,\n        "ignored_land_rotations": [10],\n        "reason": "Проверяемое структурное исключение.",\n        "source_path": "campaign/event/example.py",\n    }\n\n\ndef test_compatibility_data_distinguishes_missing_patch_id(tmp_path: Path):\n    root = tmp_path / "compatibility"\n    root.mkdir()\n    data = _compatibility_snapshot(_compatibility_patch(""))\n    (root / "en-1.json").write_text(\n        json.dumps(data, ensure_ascii=False), encoding="utf-8"\n    )\n\n    with pytest.raises(CompatibilityDataError, match="не содержит id"):\n        load_compatibility_data("en:1", root=root)\n\n\ndef test_compatibility_data_distinguishes_duplicate_patch_id(tmp_path: Path):\n    root = tmp_path / "compatibility"\n    root.mkdir()\n    data = _compatibility_snapshot(\n        _compatibility_patch("duplicate", map_id=10),\n        _compatibility_patch("duplicate", map_id=11),\n    )\n    (root / "en-1.json").write_text(\n        json.dumps(data, ensure_ascii=False), encoding="utf-8"\n    )\n\n    with pytest.raises(CompatibilityDataError, match="Неуникальный compatibility patch id"):\n        load_compatibility_data("en:1", root=root)\n''',
)

# WebUI source: assert не должен управлять runtime-потоком.
replace_once(
    "module/webui/app_event_datamine.py",
    '            if artifact is None:\n                assert unavailable is not None\n                return unavailable\n',
    '            if artifact is None:\n                if unavailable is None:\n                    raise RuntimeError(\n                        "Resolver текущего Event artifact вернул пустой результат без причины"\n                    )\n                return unavailable\n',
)

# Один канонический helper строк каталога.
catalog_path = Path("module/shop_event/catalog.py")
catalog_text = catalog_path.read_text(encoding="utf-8")
if "def _catalog_rows(" not in catalog_text:
    raise SystemExit("module/shop_event/catalog.py: приватный catalog helper не найден")
catalog_text = catalog_text.replace("def _catalog_rows(", "def catalog_rows(", 1)
catalog_text = catalog_text.replace("_catalog_rows(", "catalog_rows(")
catalog_path.write_text(catalog_text, encoding="utf-8")

replace_once(
    "module/webui/event_shop_observation.py",
    'from module.shop_event.catalog import (\n    bind_catalog_source,\n    int_attr,\n    resolve_catalog_claim,\n    source_row_compatible,\n)\n',
    'from module.shop_event.catalog import (\n    bind_catalog_source,\n    catalog_rows,\n    int_attr,\n    resolve_catalog_claim,\n    source_row_compatible,\n)\n',
)
replace_once(
    "module/webui/event_shop_observation.py",
    '''\n\ndef _catalog_rows(spec: Mapping[str, Any]) -> list[Mapping[str, Any]]:\n    return [\n        item\n        for item in spec.get("shop_items", [])\n        if isinstance(item, Mapping)\n    ]\n''',
    "",
)
observation_path = Path("module/webui/event_shop_observation.py")
observation_text = observation_path.read_text(encoding="utf-8")
if "_catalog_rows(" not in observation_text:
    raise SystemExit("event_shop_observation.py: вызов локального helper не найден")
observation_path.write_text(
    observation_text.replace("_catalog_rows(", "catalog_rows("), encoding="utf-8"
)

# LogRes: EventShop не будит мост, но основной Dashboard-контракт сохраняется.
replace_once(
    "tests/test_event_currency_wakeup.py",
    '    LogRes(config).Pt = 150\n\n    assert calls == []\n',
    '    LogRes(config).Pt = 150\n\n    assert config.modified["Dashboard.Pt.Value"] == 150\n    assert "Dashboard.Pt.Record" in config.modified\n    assert calls == []\n',
)

# Runtime semantics: числовые границы detector policy должны оставаться fail-closed.
append_once(
    "tests/test_event_runtime_semantics.py",
    '        )\n\n\ndef test_battle_plan_rejects_python_like_filter_payload():\n',
    '''\n\n\ndef _valid_detector_policy():\n    return {\n        "internal_lines": {\n            "height": [80, 238],\n            "width": [0.9, 10],\n            "prominence": 10,\n            "distance": 35,\n        },\n        "edge_lines": {\n            "height": [238, 255],\n            "prominence": 10,\n            "distance": 50,\n            "wlen": 1000,\n        },\n        "swipe": {\n            "adb": [1.0, 1.1],\n            "minitouch": [1.0, 1.1],\n            "maatouch": [1.0, 1.1],\n        },\n    }\n\n\n@pytest.mark.parametrize(\n    ("field", "value"),\n    (\n        ("height", [200, 300]),\n        ("height", [200, 100]),\n        ("prominence", 0),\n    ),\n)\ndef test_detector_policy_rejects_invalid_line_peak_bounds(field, value):\n    policy = _valid_detector_policy()\n    policy["internal_lines"][field] = value\n\n    with pytest.raises(EventRuntimePolicyError):\n        parse_detector_calibration(\n            policy,\n            map_id=1,\n            error_type=EventRuntimePolicyError,\n        )\n''',
)

# OCR contract должен проверять именно смещение текстовой области цены.
replace_once(
    "tests/test_event_shop_catalog_evidence.py",
    '    assert grid.price_area[0] >= 0\n    assert grid.price_area[2] <= ITEM_SHAPE[0] + 20\n    assert grid.amount_area[3] < ITEM_SHAPE[1]\n',
    '    # Текст цены начинается правее иконки валюты.\n    assert grid.price_area[0] > grid.cost_area[0]\n    assert grid.price_area[2] == grid.cost_area[2]\n    assert grid.amount_area[3] < ITEM_SHAPE[1]\n',
)

# Supplemental malformed cases должны быть независимыми pytest-case.
replace_once(
    "tests/test_event_supplemental_fail_closed.py",
    "import copy\nfrom pathlib import Path\n\n",
    "import copy\nfrom pathlib import Path\n\nimport pytest\n\n",
)
replace_once(
    "tests/test_event_supplemental_fail_closed.py",
    '''def test_malformed_supplemental_cases_fall_back_to_raw_artifact(tmp_path: Path) -> None:\n    artifact = production_artifact()\n    event_id = artifact["event_spec"]["id"]\n    source = load_supplemental(event_id)\n    assert source is not None\n\n    cases = (\n        "schema_version",\n        "task_expected_points",\n        "farm_base_points",\n        "milestone_threshold",\n        "base_map_count",\n        "resource_identity",\n    )\n    for case in cases:\n        supplemental = copy.deepcopy(source)\n        _corrupt_supplemental(supplemental, case)\n        case_root = tmp_path / case\n        write_split_supplemental(case_root, supplemental)\n\n        resolved, resolution = resolve_supplemental_artifact(\n            artifact,\n            supplemental_root=case_root,\n        )\n\n        assert resolution["kind"] == "supplemental_rejected", case\n        assert resolution["error"], case\n        assert resolved["event_spec"]["source_status"] == artifact["event_spec"][\n            "source_status"\n        ], case\n        assert resolved["event_spec"]["provenance"]["revision"] == artifact[\n            "event_spec"\n        ]["provenance"]["revision"], case\n        assert any(\n            item.get("code") == "supplemental_rejected"\n            for item in resolved["event_spec"]["findings"]\n        ), case\n        assert validate_artifact(resolved) == resolved, case\n''',
    '''@pytest.mark.parametrize(\n    "case",\n    (\n        "schema_version",\n        "task_expected_points",\n        "farm_base_points",\n        "milestone_threshold",\n        "base_map_count",\n        "resource_identity",\n    ),\n)\ndef test_malformed_supplemental_case_falls_back_to_raw_artifact(\n    tmp_path: Path, case: str\n) -> None:\n    artifact = production_artifact()\n    event_id = artifact["event_spec"]["id"]\n    source = load_supplemental(event_id)\n    assert source is not None\n\n    supplemental = copy.deepcopy(source)\n    _corrupt_supplemental(supplemental, case)\n    case_root = tmp_path / case\n    write_split_supplemental(case_root, supplemental)\n\n    resolved, resolution = resolve_supplemental_artifact(\n        artifact,\n        supplemental_root=case_root,\n    )\n\n    assert resolution["kind"] == "supplemental_rejected"\n    assert resolution["error"]\n    assert resolved["event_spec"]["source_status"] == artifact["event_spec"][\n        "source_status"\n    ]\n    assert resolved["event_spec"]["provenance"]["revision"] == artifact[\n        "event_spec"\n    ]["provenance"]["revision"]\n    assert any(\n        item.get("code") == "supplemental_rejected"\n        for item in resolved["event_spec"]["findings"]\n    )\n    assert validate_artifact(resolved) == resolved\n''',
)

# Generated UI policy: все поддержанные layout и неизвестный fail-closed.
replace_once(
    "tests/test_generated_event_ui_policy.py",
    "from types import ModuleType\n\n",
    "from types import ModuleType\n\nimport pytest\n\n",
)
append_once(
    "tests/test_generated_event_ui_policy.py",
    '    assert Config.MAP_HAS_MODE_SWITCH is True\n',
    '''\n\n\ndef test_20260326_layout_switches_chapter_flags():\n    class Config:\n        pass\n\n    _apply_generated_campaign_ui_policy(_module_with_config(Config), "20260326")\n\n    assert Config.MAP_CHAPTER_SWITCH_20260326 is True\n    assert Config.MAP_CHAPTER_SWITCH_20241219 is False\n    assert Config.MAP_CHAPTER_SWITCH_20241219_SP is False\n    assert Config.MAP_CHAPTER_SWITCH_20241219_SPEX is False\n\n\n@pytest.mark.parametrize("layout", (None, "legacy"))\ndef test_legacy_or_missing_layout_keeps_config_untouched(layout):\n    class Config:\n        marker = object()\n\n    marker = Config.marker\n    _apply_generated_campaign_ui_policy(_module_with_config(Config), layout)\n\n    assert vars(Config) == {\n        "__module__": Config.__module__,\n        "marker": marker,\n        "__dict__": vars(Config)["__dict__"],\n        "__weakref__": vars(Config)["__weakref__"],\n        "__doc__": None,\n    }\n\n\ndef test_unknown_layout_fails_closed():\n    class Config:\n        pass\n\n    with pytest.raises(ValueError, match="Неподдерживаемая раскладка"):\n        _apply_generated_campaign_ui_policy(_module_with_config(Config), "20991231")\n''',
)

# Источники PT: делим за один проход, без equality membership словарей.
replace_once(
    "module/webui/app_event_general_presentation.py",
    '''        map_sources = [\n            item\n            for item in overview\n            if item.get("kind") == "repeatable_map_clear"\n            or self._map_group_key(item.get("name")) != "OTHER"\n        ]\n        other_sources = [item for item in overview if item not in map_sources]\n''',
    '''        map_sources = []\n        other_sources = []\n        for item in overview:\n            if (\n                item.get("kind") == "repeatable_map_clear"\n                or self._map_group_key(item.get("name")) != "OTHER"\n            ):\n                map_sources.append(item)\n            else:\n                other_sources.append(item)\n''',
)

# Награды: следующий milestone не зависит от порядка входного списка.
replace_once(
    "module/webui/app_event_general_v2.py",
    '''        next_threshold = next(\n            (\n                int(item.get("threshold", 0) or 0)\n                for item in milestones\n                if current_pt is not None\n                and int(item.get("threshold", 0) or 0) > current_pt\n            ),\n            None,\n        )\n''',
    '''        next_threshold = min(\n            (\n                int(item.get("threshold", 0) or 0)\n                for item in milestones\n                if current_pt is not None\n                and int(item.get("threshold", 0) or 0) > current_pt\n            ),\n            default=None,\n        )\n''',
)

# Event map layout: неизвестный PackageName не должен падать; docstring отражает код.
replace_once(
    "module/webui/app_event_layout.py",
    '''    def _current_event_name(self, config: Mapping[str, Any]) -> str | None:\n        """Получить отображаемое имя текущего события из активного Event artifact."""\n        if is_demo_mode():\n            return None\n        package_name = str(\n            deep_get(config, ["Alas", "Emulator", "PackageName"], "") or ""\n        ).strip()\n        if not package_name:\n            return None\n        server = str(to_server(package_name) or "").strip().upper()\n        if not server:\n            return None\n''',
    '''    @staticmethod\n    def _event_package_server(config: Mapping[str, Any]) -> str | None:\n        """Безопасно определить сервер текущего PackageName для Event UI."""\n        package_name = str(\n            deep_get(config, ["Alas", "Emulator", "PackageName"], "") or ""\n        ).strip()\n        if not package_name:\n            return None\n        try:\n            server = str(to_server(package_name) or "").strip().upper()\n        except ValueError:\n            logger.warning(\n                f"[WebUI — ивент] Неизвестный PackageName для определения сервера: {package_name}"\n            )\n            return None\n        return server or None\n\n    def _current_event_name(self, config: Mapping[str, Any]) -> str | None:\n        """Получить отображаемое имя текущего события из активного Event artifact."""\n        if is_demo_mode():\n            return None\n        server = self._event_package_server(config)\n        if server is None:\n            return None\n''',
)
replace_once(
    "module/webui/app_event_layout.py",
    '        """Скрыть stale selector локально, не меняя config и глобальный i18n."""\n',
    '        """Скрыть доступный selector локально, не меняя config и глобальный i18n."""\n',
)
replace_once(
    "module/webui/app_event_layout.py",
    '''        options = {\n            str(item)\n            for field in (\n                "option",\n                f"option_{to_server(str(deep_get(config, ['Alas', 'Emulator', 'PackageName'], '')))}",\n            )\n            for item in (event_arg.get(field) or [])\n        }\n''',
    '''        server = self._event_package_server(config)\n        if server is None:\n            return task_args, config, None\n        options = {\n            str(item)\n            for field in ("option", f"option_{server.lower()}")\n            for item in (event_arg.get(field) or [])\n        }\n''',
)

# Удаляем MRO-shadowed legacy DOM patch из EventPlannerMixin без shim.
planner_path = Path("module/webui/app_event_planner.py")
planner_text = planner_path.read_text(encoding="utf-8")
start = planner_text.find("    def _patch_event_shop_plan_values(\n")
end = planner_text.find("    @staticmethod\n    def _stale_plan_message()", start)
if start < 0 or end < 0:
    raise SystemExit("app_event_planner.py: дублирующий DOM patch не найден")
planner_text = planner_text[:start] + planner_text[end:]
if "json." not in planner_text and "import json\n" in planner_text:
    planner_text = planner_text.replace("import json\n", "", 1)
planner_path.write_text(planner_text, encoding="utf-8")

# Runtime asset resolver следует только validated canonical catalog.
event_assets_path = Path("module/webui/event_assets.py")
event_assets = event_assets_path.read_text(encoding="utf-8")
event_assets = event_assets.replace("import json\nimport re\n", "import json\n", 1)
event_assets = event_assets.replace(
    '_SAFE_DISPLAY_KIND = re.compile(r"[A-Za-z0-9_-]+")\n_DISPLAY_EXTENSIONS = (".png", ".svg", ".webp")\n\n\n',
    "",
    1,
)
helper_start = event_assets.find("def _event_shop_display_url(\n")
helper_end = event_assets.find("\ndef event_asset_url(\n", helper_start)
if helper_start < 0 or helper_end < 0:
    raise SystemExit("event_assets.py: прямой display resolver не найден")
event_assets = event_assets[:helper_start] + event_assets[helper_end + 1 :]
event_assets = event_assets.replace(
    '    display_url = _event_shop_display_url(asset, asset_root=asset_root)\n    if display_url:\n        return display_url\n',
    "",
    1,
)
event_assets_path.write_text(event_assets, encoding="utf-8")

# Asset tests больше не зависят от production catalog и фиксируют canonical приоритет.
Path("tests/test_event_webui_display_assets.py").write_text(
    '''from __future__ import annotations\n\nimport json\nfrom pathlib import Path\n\nfrom module.event_datamine.assets import asset_catalog_digest\nfrom module.webui.event_assets import event_asset_url\n\n\ndef _write_catalog(path: Path, entries: dict[str, str]) -> None:\n    data = {\n        "asset_catalog_schema_version": 1,\n        "entries": entries,\n    }\n    data["digest"] = asset_catalog_digest(data)\n    path.write_text(json.dumps(data), encoding="utf-8")\n\n\ndef test_webui_display_files_cannot_bypass_ambiguous_canonical_catalog(tmp_path: Path):\n    asset_root = tmp_path / "assets"\n    display_root = asset_root / "webui" / "event_shop"\n    scanner_root = asset_root / "stats_basic"\n    display_root.mkdir(parents=True)\n    scanner_root.mkdir(parents=True)\n    (display_root / "item-30014.svg").write_text("eagle", encoding="utf-8")\n    (display_root / "item-30024.svg").write_text("royal", encoding="utf-8")\n    (scanner_root / "BoxT4.png").write_bytes(b"scanner")\n\n    catalog_path = tmp_path / "assets.json"\n    _write_catalog(\n        catalog_path,\n        {"item:Props/30004": "/static/assets/stats_basic/BoxT4.png"},\n    )\n    eagle = {"kind": "item", "game_id": "30014", "source_path": "Props/30004"}\n    royal = {"kind": "item", "game_id": "30024", "source_path": "Props/30004"}\n\n    assert event_asset_url(\n        eagle, catalog_path=catalog_path, asset_root=asset_root\n    ) == "/static/assets/stats_basic/BoxT4.png"\n    assert event_asset_url(\n        royal, catalog_path=catalog_path, asset_root=asset_root\n    ) == "/static/assets/stats_basic/BoxT4.png"\n\n\ndef test_webui_unique_display_asset_is_used_only_through_catalog(tmp_path: Path):\n    asset_root = tmp_path / "assets"\n    display_root = asset_root / "webui" / "event_shop"\n    display_root.mkdir(parents=True)\n    (display_root / "item-30034.svg").write_text("display", encoding="utf-8")\n\n    catalog_path = tmp_path / "assets.json"\n    _write_catalog(\n        catalog_path,\n        {\n            "item:Props/30034": "/static/assets/webui/event_shop/item-30034.svg",\n        },\n    )\n    asset = {"kind": "item", "game_id": "30034", "source_path": "Props/30034"}\n\n    assert event_asset_url(\n        asset, catalog_path=catalog_path, asset_root=asset_root\n    ) == "/static/assets/webui/event_shop/item-30034.svg"\n\n\ndef test_webui_display_identity_keeps_canonical_fallback(tmp_path: Path):\n    asset_root = tmp_path / "assets"\n    scanner_root = asset_root / "stats_basic"\n    scanner_root.mkdir(parents=True)\n    (scanner_root / "BoxT4.png").write_bytes(b"scanner")\n    catalog_path = tmp_path / "assets.json"\n    _write_catalog(\n        catalog_path,\n        {"item:Props/30004": "/static/assets/stats_basic/BoxT4.png"},\n    )\n    asset = {"kind": "item", "game_id": "30034", "source_path": "Props/30004"}\n\n    assert event_asset_url(\n        asset, catalog_path=catalog_path, asset_root=asset_root\n    ) == "/static/assets/stats_basic/BoxT4.png"\n''',
    encoding="utf-8",
)
