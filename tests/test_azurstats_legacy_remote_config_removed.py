import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_legacy_remote_azurstats_config_is_absent():
    generated_and_source = (
        "module/config/argument/argument.yaml",
        "module/config/argument/args.json",
        "module/config/config_generated.py",
        "config/template.json",
        "module/config/config_updater.py",
        "module/config/redirect_utils/utils.py",
    )
    forbidden = (
        "AzurStats" + "ID",
        "cn_gz_" + "reverse_proxy",
    )
    hits = {}
    for relative in generated_and_source:
        text = (ROOT / relative).read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                hits.setdefault(token, []).append(relative)

    for filename in ("ru-RU.json", "en-US.json"):
        data = json.loads(
            (ROOT / "module/config/i18n" / filename).read_text(encoding="utf-8")
        )
        drop_record = data.get("DropRecord", {})
        assert "AzurStatsID" not in drop_record
        assert "API" not in drop_record

    assert not hits, "Legacy remote AzurStats configuration returned: " + repr(hits)
