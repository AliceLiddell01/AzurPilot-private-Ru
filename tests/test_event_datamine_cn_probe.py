import json
from pathlib import Path

from module.event_datamine.compiler import EventCompiler
from module.event_datamine.source import ShareCfgLoader, SourceSnapshot

REVISION = "f44b48853d48b400b92738b1f1cf6fcdf1d69169"
ROOT = Path(__file__).resolve().parents[1]


def write_table(root: Path, name: str, rows: str) -> None:
    folder = root / "CN" / "sharecfg"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.lua").write_text(rows, encoding="utf-8")


def test_current_cn_rows_decode_or_fail_with_structured_unsupported(tmp_path: Path):
    time = '{ "timer", {{2026, 8, 13}, {0, 0, 0}}, {{2026, 8, 27}, {23, 59, 59}} }'
    write_table(
        tmp_path,
        "activity_template",
        f"""
pg.base.activity_template[51101] = {{ id = 51101, mark = 20260813, type = 12, config_data = {{2050001}}, time = {time}, config_client = {{ PTID = 741, shopItemID = 71387 }} }}
pg.base.activity_template[51104] = {{ id = 51104, mark = 20260813, type = 14, config_data = {{4131}}, time = {time}, config_client = {{ pt_id = 741 }} }}
pg.base.activity_template[51109] = {{ id = 51109, mark = 20260813, type = 74, config_id = 51109, time = {time}, config_client = {{ shopLinkActID = 51104 }} }}
""",
    )
    write_table(
        tmp_path,
        "activity_shop_template",
        "pg.base.activity_shop_template[4131] = { id = 4131, activity = 51104, commodity_type = 2, commodity_id = 15008, num = 100, resource_type = 741, resource_num = 300, num_limit = 5 }\n",
    )
    write_table(
        tmp_path,
        "activity_event_pt",
        "pg.base.activity_event_pt[51109] = { id = 51109, pt = 741, target = {100}, drop_client = {{2, 15008, 10}} }\n",
    )
    snapshot = SourceSnapshot(
        tmp_path, "CN", "AzurLaneTools/AzurLaneLuaScripts", REVISION
    )

    spec = EventCompiler(ShareCfgLoader(snapshot)).compile(51101)

    assert spec.id == "cn:51101"
    assert spec.provenance.server == "CN"
    assert spec.provenance.revision == REVISION
    assert spec.source_status == "unsupported"
    assert len(spec.shop_items) == 1
    assert len(spec.milestones) == 1
    assert [item.id for item in spec.currencies] == [741]
    assert any(
        item.code in {"table_missing", "event_maps_missing"}
        and item.severity == "error"
        for item in spec.findings
    )
    assert 71387 not in {item.row_id for item in spec.shop_items}


def test_live_current_cn_probe_is_pinned_and_non_production():
    summary = json.loads(
        (
            ROOT / "tests/fixtures/event_datamine/current_cn_probe_summary.json"
        ).read_text(encoding="utf-8")
    )

    assert summary == {
        "activity_id": 51101,
        "currencies": [741],
        "eligible": True,
        "event_id": "cn:51101",
        "map_count": 15,
        "milestone_count": 43,
        "name": "沉溺于星光之城",
        "non_production": True,
        "repository": "AzurLaneTools/AzurLaneLuaScripts",
        "revision": REVISION,
        "server": "CN",
        "shop_count": 26,
        "source_status": "partial",
        "structured_findings": {"asset_unresolved": 5, "map_pt_amount_unavailable": 1},
    }
