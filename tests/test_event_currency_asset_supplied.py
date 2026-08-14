import json
from pathlib import Path

from module.webui.event_assets import PLACEHOLDER_URL, event_asset_url


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "module" / "event_datamine" / "data" / "production" / "en-51101.json"


def test_current_en_currency_uses_supplied_local_asset():
    data = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    currency = data["event_spec"]["currencies"][0]

    resolved = event_asset_url(currency["asset"])

    assert resolved != PLACEHOLDER_URL
    assert resolved == "/static/assets/webui/event_shop/activity_currency-741.webp"
    assert (ROOT / "assets" / "webui" / "event_shop" / "activity_currency-741.webp").is_file()
