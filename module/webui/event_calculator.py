"""WebUI 活动计算器，从碧蓝航线 Wiki 同步活动数据并在本地渲染计算器。
支持活动商店物品的价格解析、资源需求计算，
以及计算结果的 HTML 界面展示。"""

# 从碧蓝航线 Wiki 活动计算器同步数据，并在 WebUI 中渲染本地计算器。
import json
import os
import re
from datetime import datetime
from html import escape
from typing import Any, Dict, List

import requests

from module.logger import logger


WIKI_RAW_URL = (
    "https://wiki.biligame.com/blhx/"
    "%E6%B4%BB%E5%8A%A8%E8%AE%A1%E7%AE%97%E5%99%A8?action=raw"
)
CACHE_FILE = "./cache/wiki_event_calculator.json"
CACHE_VERSION = 2

EVENT_ITEM_RU_MAP = {
    "深潜许可": "Разрешение на глубоководное погружение",
    "建造券": "Купон на постройку",
    "魔方": "Куб",
    "心智单元II": "Когнитивный модуль II",
    "心智单元": "Когнитивный модуль",
    "外观装备箱": "Ящик декоративного снаряжения",
    "定向蓝图·八期": "Направленный чертёж · сезон 8",
    "高级定向蓝图·八期": "Продвинутый направленный чертёж · сезон 8",
    "定向蓝图": "Направленный чертёж",
    "高级定向蓝图": "Продвинутый направленный чертёж",
    "特殊兵装核心": "Ядро особого вооружения",
    "兵装强化石T2": "Камень усиления вооружения T2",
    "兵装重构核心T2": "Ядро реконструкции вооружения T2",
    "兵装重构核心T1": "Ядро реконструкции вооружения T1",
    "喵箱SSR": "Ящик мяуфицера SSR",
    "喵箱SR": "Ящик мяуфицера SR",
    "喵箱R": "Ящик мяуфицера R",
    "科技箱T4": "Технический ящик T4",
    "通用部件T3": "Универсальные детали T3",
    "主炮部件T3": "Детали главного калибра T3",
    "鱼雷部件T3": "Детали торпед T3",
    "防空炮部件T3": "Детали ПВО T3",
    "舰载机部件T3": "Детали палубной авиации T3",
    "物资": "Монеты",
    "石油": "Нефть",
    "酸素可乐": "Кислородная кола",
    "建造3次": "Выполнить 3 постройки",
    "出击胜利15次": "Победить в 15 боевых выходах",
    "通关1次困难关卡": "Пройти 1 сложный этап",
}

EVENT_SHOP_FILTER_MAP = [
    ("深潜许可", "URpt"),
    ("建造券", "GachaTicket"),
    ("魔方", "Cube"),
    ("心智单元II", "Chip"),
    ("心智单元", "Array"),
    ("外观装备箱", "SkinBox"),
    ("META", "Meta"),
    ("定向蓝图·八期", "PRS8"),
    ("高级定向蓝图·八期", "DRS8"),
    ("定向蓝图", "PR"),
    ("高级定向蓝图", "DR"),
    ("特殊兵装核心", "AugmentCoreT3"),
    ("兵装强化石T2", "AugmentEnhanceT2"),
    ("兵装重构核心T2", "AugmentChangeT2"),
    ("兵装重构核心T1", "AugmentChangeT1"),
    ("喵箱SSR", "CatT3"),
    ("喵箱SR", "CatT2"),
    ("喵箱R", "CatT1"),
    ("科技箱T4", "BoxT4"),
    ("通用部件T3", "PlateGeneralT3"),
    ("主炮部件T3", "PlateGunT3"),
    ("鱼雷部件T3", "PlateTorpedoT3"),
    ("防空炮部件T3", "PlateAntiairT3"),
    ("舰载机部件T3", "PlatePlaneT3"),
    ("物资", "Coin"),
    ("石油", "Oil"),
    ("酸素可乐", "FoodT1"),
]


def _clean_wikitext(raw: str) -> str:
    return re.sub(r"<!--.*?-->", "", raw, flags=re.S)


def _extract_table(raw: str, table_id: str) -> str:
    match = re.search(rf'\{{\|[^\n]*id="{re.escape(table_id)}"[^\n]*\n', raw)
    if match is None:
        return ""
    start = match.end()
    end_candidates = []
    for marker in ("\n{|", "\n|}", "\n=="):
        pos = raw.find(marker, start)
        if pos >= 0:
            end_candidates.append(pos)
    end = min(end_candidates) if end_candidates else len(raw)
    return raw[start:end]


def _strip_cell_attr(cell: str) -> str:
    if re.match(r'^[A-Za-z0-9_:-]+="[^"]*"\|', cell):
        return cell.split("|", 1)[1]
    return cell


def _parse_table_rows(table: str) -> List[List[str]]:
    rows: List[List[str]] = []
    for block in re.split(r"\n\|-\s*\n", table):
        cells: List[str] = []
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("!") or line.startswith("|}"):
                continue
            if line.startswith("|-"):
                continue
            if line.startswith("||"):
                cell_line = line[2:]
            elif line.startswith("|"):
                cell_line = line[1:]
            else:
                continue
            cell_line = _strip_cell_attr(cell_line.strip())
            cells.extend(part.strip() for part in cell_line.split("||"))
        if cells:
            rows.append(cells)
    return rows


def _clean_name(text: str) -> str:
    text = re.sub(r"\[\[文件:[^\]]+\]\]", "", text)
    text = re.sub(r"\[\[[^\]|]+\|([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\{\{[^{}|]+\|([^{}]+?)\}\}", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("'''", "").replace("''", "")
    return text.strip()


def _translate_wiki_name(text: str) -> str:
    clean = _clean_name(text)
    return EVENT_ITEM_RU_MAP.get(clean, clean)


def _to_int(value: str, default: int = 0) -> int:
    match = re.search(r"-?\d+", str(value).replace(",", ""))
    if match is None:
        return default
    return int(match.group(0))


def match_event_shop_filter(name: str, price: int, quantity: int) -> str:
    for pattern, filter_name in EVENT_SHOP_FILTER_MAP:
        if pattern in name:
            return filter_name
    if price == 8000 and quantity <= 2:
        return "ShipSSR"
    if price == 2000 and quantity == 1:
        return "EquipSSR"
    if price == 10000:
        return "EquipUR"
    return ""


def _parse_shop(rows: List[List[str]]) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        if len(row) < 3:
            continue
        price = _to_int(row[1])
        quantity = _to_int(row[2])
        if price <= 0 or quantity < 0:
            continue
        source_name = _clean_name(row[0])
        out.append(
            {
                "name": _translate_wiki_name(row[0]) or "Безымянный предмет",
                "price": price,
                "quantity": quantity,
                "filter": match_event_shop_filter(source_name, price, quantity),
            }
        )
    return out


def _parse_points(rows: List[List[str]], key_name: str) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        if len(row) < 2:
            continue
        points = _to_int(row[1])
        if points <= 0:
            continue
        out.append({"name": _translate_wiki_name(row[0]), key_name: points})
    return out


def _parse_event_name(raw: str) -> str:
    match = re.search(r"当前活动：\[\[[^\]|]+(?:\|([^\]]+))?\]\]", raw)
    if match is None:
        return ""
    return _clean_name(match.group(1) or match.group(0))


def parse_event_calculator(raw: str) -> Dict[str, Any]:
    """解析 Wiki 活动计算器页面原文。"""
    cleaned = _clean_wikitext(raw)
    time_rows = _parse_table_rows(_extract_table(cleaned, "ECALCTime"))
    end_date = time_rows[0][0] if time_rows and time_rows[0] else ""

    data = {
        "event_name": _parse_event_name(cleaned),
        "end_date": end_date.replace("/", "-"),
        "shop_items": _parse_shop(_parse_table_rows(_extract_table(cleaned, "ECALCPt"))),
        "daily": _parse_points(
            _parse_table_rows(_extract_table(cleaned, "ECALCDaily")), "points"
        ),
        "extra": _parse_points(
            _parse_table_rows(_extract_table(cleaned, "ECALCExtra")), "points"
        ),
        "stages": _parse_points(
            _parse_table_rows(_extract_table(cleaned, "ECALC")), "points"
        ),
        "source_url": WIKI_RAW_URL,
        "updated_at": datetime.now().replace(microsecond=0).isoformat(sep=" "),
    }
    data["shop_total"] = sum(
        item["price"] * item["quantity"] for item in data["shop_items"]
    )
    return data


def _read_cache() -> Dict[str, Any]:
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"[WebUI — Калькулятор] Не удалось прочитать кэш калькулятора события Wiki: {e}")
        return {}


def _write_cache(data: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        data["cache_version"] = CACHE_VERSION
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[WebUI — Калькулятор] Не удалось записать кэш калькулятора события Wiki: {e}")


def load_event_calculator(force_refresh: bool = False) -> Dict[str, Any]:
    """Прочитать локальный кэш; обращаться к внешней Wiki только по явному запросу."""
    cache = _read_cache()
    cache_valid = cache.get("cache_version") == CACHE_VERSION
    if cache and cache_valid and not force_refresh:
        return {**cache, "from_cache": True}

    if not force_refresh:
        return {
            "error": (
                "Локальный кэш калькулятора события отсутствует или устарел. "
                "Нажмите «Загрузить данные Wiki», чтобы явно выполнить внешний запрос "
                "к wiki.biligame.com."
            ),
            "from_cache": False,
            "needs_refresh": True,
        }

    try:
        response = requests.get(WIKI_RAW_URL, timeout=10)
        response.raise_for_status()
        data = parse_event_calculator(response.text)
        if not data["shop_items"] or not data["stages"]:
            raise ValueError("таблица калькулятора события в Wiki заполнена не полностью")
        _write_cache(data)
        return {**data, "from_cache": False}
    except Exception as e:
        logger.warning(f"[WebUI — Калькулятор] Не удалось получить калькулятор события Wiki: {e}")
        if cache and cache_valid:
            return {**cache, "from_cache": True, "error": str(e)}
        return {"error": str(e), "from_cache": False}


def build_event_calculator_html(scope_id: str) -> str:
    return f"""
<style>
#{scope_id}.event-calculator {{
  margin-top: 18px;
  padding-bottom: 8px;
  color: inherit;
}}
#{scope_id} .event-calc-toolbar {{
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-bottom: 10px;
}}
#{scope_id} .event-calc-write-actions {{
  display: inline-flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}}
#{scope_id} .event-calc-write-actions > div {{
  display: inline-flex;
  gap: 8px;
  align-items: center;
}}
#{scope_id} .event-calc-badge {{
  display: inline-flex;
  align-items: center;
  min-height: 26px;
  padding: 3px 8px;
  border: 1px solid rgba(128, 128, 128, .28);
  border-radius: 4px;
  font-size: 12px;
  opacity: .82;
}}
#{scope_id} .event-calc-grid {{
  display: grid;
  grid-template-columns: minmax(260px, 380px) minmax(360px, 1fr);
  gap: 14px;
}}
#{scope_id} .event-calc-panel {{
  border: 1px solid rgba(128, 128, 128, .24);
  border-radius: 6px;
  padding: 10px;
  overflow: auto;
  background: rgba(128, 128, 128, .03);
}}
#{scope_id} .event-calc-title {{
  font-weight: 600;
  margin-bottom: 8px;
}}
#{scope_id} .event-calc-fields {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-bottom: 10px;
}}
#{scope_id} label {{
  display: grid;
  gap: 3px;
  margin: 0;
  font-size: 12px;
}}
#{scope_id} input[type="number"],
#{scope_id} input[type="date"] {{
  min-width: 0;
  height: 30px;
  padding: 3px 6px;
  border: 1px solid rgba(128, 128, 128, .45);
  border-radius: 4px;
  color: inherit;
  background: rgba(128, 128, 128, .16);
}}
#{scope_id} button {{
  min-height: 28px;
  border: 1px solid rgba(128, 128, 128, .38);
  border-radius: 4px;
  color: inherit !important;
  background: rgba(128, 128, 128, .14) !important;
  box-shadow: none !important;
  cursor: pointer;
}}
#{scope_id} button:hover {{
  background: rgba(128, 128, 128, .22) !important;
}}
#{scope_id} table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}}
#{scope_id} th,
#{scope_id} td {{
  border: 1px solid rgba(128, 128, 128, .25);
  padding: 4px 6px;
  vertical-align: middle;
}}
#{scope_id} th {{
  font-weight: 600;
  background: rgba(128, 128, 128, .12);
}}
#{scope_id} .event-calc-number {{
  width: 70px;
}}
#{scope_id} .event-calc-range {{
  width: 100%;
}}
#{scope_id} .event-calc-total {{
  font-weight: 700;
  color: #e53935;
}}
#{scope_id} input[type="range"] {{
  accent-color: #3b82f6;
}}
#{scope_id} input[type="checkbox"] {{
  accent-color: #6f5bb8;
}}
#{scope_id} .event-calc-muted {{
  opacity: .68;
}}
@media (max-width: 860px) {{
  #{scope_id} .event-calc-grid {{
    grid-template-columns: 1fr;
  }}
}}
</style>
<div id="{scope_id}" class="event-calculator">
  <div class="event-calc-toolbar">
    <span class="event-calc-badge" data-role="event-name"></span>
    <span class="event-calc-badge" data-role="source"></span>
    <span class="event-calc-badge">Итого <span class="event-calc-total" data-role="shop-total" style="margin-left:4px;"></span></span>
    <label class="event-calc-badge" style="gap:6px;">
      <input data-field="auto-target" type="checkbox" checked>
      Синхронизировать цель с магазином
    </label>
    <button type="button" data-action="import-shop">Взять итог магазина</button>
    <button type="button" data-action="clear-shop">Очистить покупки</button>
    <button type="button" data-action="fill-shop">Выбрать всё</button>
    <span id="pywebio-scope-{scope_id}_write_actions" class="event-calc-write-actions"></span>
  </div>
  <div class="event-calc-grid">
    <div class="event-calc-panel">
      <div class="event-calc-title">Цель и ежедневный доход</div>
      <div class="event-calc-fields">
        <label>Целевые очки<input data-field="target" type="number" min="0" step="1"></label>
        <label>Уже получено<input data-field="owned" type="number" min="0" step="1"></label>
        <label>Дата окончания<input data-field="end-date" type="date"></label>
        <label>Осталось дней<input data-field="remaining-days" type="number" readonly></label>
      </div>
      <div class="event-calc-title">Ежедневные задания</div>
      <table data-role="daily"></table>
      <div class="event-calc-title" style="margin-top:10px;">Дополнительно за день</div>
      <table data-role="extra"></table>
    </div>
    <div class="event-calc-panel">
      <div class="event-calc-title">Магазин обмена</div>
      <table data-role="shop"></table>
    </div>
    <div class="event-calc-panel">
      <div class="event-calc-title">Расчёт боевых выходов</div>
      <table data-role="stages"></table>
    </div>
  </div>
</div>
"""


def build_event_calculator_js(scope_id: str, data: Dict[str, Any], initial: Dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    initial_payload = json.dumps(initial, ensure_ascii=False, default=str)
    escaped_scope = json.dumps(scope_id)
    return f"""
(function() {{
  const scopeId = {escaped_scope};
  const data = {payload};
  const initial = {initial_payload};
  const root = document.getElementById(scopeId);
  if (!root) return;
  window.alasEventCalculator = window.alasEventCalculator || {{}};

  const dailyDefaults = initial.daily || {{}};
  const extraDefaults = initial.extra || {{}};
  const state = {{
    shop: (data.shop_items || []).map(item => Object.assign({{}}, item, {{ value: item.quantity || 0 }})),
    daily: (data.daily || []).map(item => {{
      const defaults = dailyDefaults[item.name] || {{}};
      return Object.assign({{}}, item, {{
        never: Boolean(defaults.never),
        already: Boolean(defaults.already)
      }});
    }}),
    extra: (data.extra || []).map(item => {{
      const defaults = extraDefaults[item.name] || {{}};
      return Object.assign({{}}, item, {{
        never: Boolean(defaults.never),
        already: Boolean(defaults.already)
      }});
    }})
  }};

  function intValue(value) {{
    const number = Number.parseInt(value, 10);
    return Number.isFinite(number) ? number : 0;
  }}

  function escapeHtml(value) {{
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }}

  function remainingDays(dateValue) {{
    if (!dateValue) return 0;
    const end = new Date(dateValue + "T00:00:00+08:00");
    const now = new Date();
    const chinaNow = new Date(now.getTime() + (8 + now.getTimezoneOffset() / 60) * 3600000);
    return Math.max(Math.ceil((end - chinaNow) / 86400000) + 1, 0);
  }}

  function shopTotal() {{
    return state.shop.reduce((sum, item) => sum + intValue(item.price) * intValue(item.value), 0);
  }}

  function selectedShopFilter() {{
    const amounts = new Map();
    const order = [];
    const missing = [];
    for (const item of state.shop) {{
      const value = intValue(item.value);
      if (value <= 0) continue;
      if (item.filter && item.filter !== "URpt") {{
        if (!amounts.has(item.filter)) order.push(item.filter);
        amounts.set(item.filter, (amounts.get(item.filter) || 0) + value);
      }} else if (item.filter === "URpt") {{
        missing.push(`${{item.name || "URpt"}} (обрабатывается отдельно логикой обмена UR)`);
      }} else {{
        missing.push(item.name || "Безымянный предмет");
      }}
    }}
    return {{
      filters: order.map(name => `${{name}}:${{amounts.get(name)}}`),
      missing
    }};
  }}

  function syncTargetFromShop() {{
    const autoTarget = root.querySelector('[data-field="auto-target"]');
    if (autoTarget && autoTarget.checked) {{
      root.querySelector('[data-field="target"]').value = shopTotal();
    }}
  }}

  function renderDailyTable(role, rows) {{
    const table = root.querySelector(`[data-role="${{role}}"]`);
    table.innerHTML = `<thead><tr><th>Задание</th><th>Очки</th><th>Пропустить</th><th>Уже выполнено сегодня</th></tr></thead><tbody></tbody>`;
    const tbody = table.querySelector("tbody");
    rows.forEach((item, index) => {{
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${{escapeHtml(item.name)}}</td>
        <td>${{item.points || 0}}</td>
        <td style="text-align:center"><input type="checkbox" data-role="${{role}}" data-index="${{index}}" data-field="never" ${{item.never ? "checked" : ""}}></td>
        <td style="text-align:center"><input type="checkbox" data-role="${{role}}" data-index="${{index}}" data-field="already" ${{item.already ? "checked" : ""}}></td>
      `;
      tbody.appendChild(tr);
    }});
  }}

  function renderShopTable() {{
    const table = root.querySelector('[data-role="shop"]');
    table.innerHTML = `<thead><tr><th>Предмет</th><th>Цена</th><th>Количество</th><th></th><th>Итого</th></tr></thead><tbody></tbody>`;
    const tbody = table.querySelector("tbody");
    state.shop.forEach((item, index) => {{
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${{escapeHtml(item.name)}}</td>
        <td>${{item.price || 0}}</td>
        <td><input class="event-calc-number" type="number" min="0" max="${{item.quantity || 0}}" value="${{item.value || 0}}" data-shop-number="${{index}}"></td>
        <td><input class="event-calc-range" type="range" min="0" max="${{item.quantity || 0}}" value="${{item.value || 0}}" data-shop-range="${{index}}"></td>
        <td data-shop-total="${{index}}"></td>
      `;
      tbody.appendChild(tr);
    }});
  }}

  function renderStages() {{
    const table = root.querySelector('[data-role="stages"]');
    table.innerHTML = `<thead><tr><th>Если проходить только</th><th>Очков за раз</th><th>Осталось прохождений</th></tr></thead><tbody></tbody>`;
    const tbody = table.querySelector("tbody");
    (data.stages || []).forEach((item, index) => {{
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${{escapeHtml(item.name)}}</td><td>${{item.points || 0}}</td><td data-stage-result="${{index}}"></td>`;
      tbody.appendChild(tr);
    }});
  }}

  function calculate() {{
    const targetEl = root.querySelector('[data-field="target"]');
    const ownedEl = root.querySelector('[data-field="owned"]');
    const endDateEl = root.querySelector('[data-field="end-date"]');
    const remaining = remainingDays(endDateEl.value);
    root.querySelector('[data-field="remaining-days"]').value = remaining;

    let target = intValue(targetEl.value) - intValue(ownedEl.value);
    for (const item of state.daily) {{
      if (!item.never) {{
        target -= remaining * intValue(item.points);
        if (item.already) target += intValue(item.points);
      }}
    }}
    for (const item of state.extra) {{
      if (!item.never) {{
        target -= remaining * intValue(item.points);
        if (item.already) target += intValue(item.points);
      }}
    }}

    root.querySelector('[data-role="shop-total"]').textContent = shopTotal();
    state.shop.forEach((item, index) => {{
      const total = intValue(item.price) * intValue(item.value);
      const totalEl = root.querySelector(`[data-shop-total="${{index}}"]`);
      if (totalEl) totalEl.textContent = total;
    }});
    (data.stages || []).forEach((item, index) => {{
      const resultEl = root.querySelector(`[data-stage-result="${{index}}"]`);
      if (resultEl) resultEl.textContent = Math.max(Math.ceil(target / intValue(item.points)), 0);
    }});
  }}

  function setShopValue(index, value) {{
    const item = state.shop[index];
    if (!item) return;
    const max = intValue(item.quantity);
    item.value = Math.min(Math.max(intValue(value), 0), max);
    const number = root.querySelector(`[data-shop-number="${{index}}"]`);
    const range = root.querySelector(`[data-shop-range="${{index}}"]`);
    if (number) number.value = item.value;
    if (range) range.value = item.value;
    syncTargetFromShop();
    calculate();
  }}

  root.querySelector('[data-role="event-name"]').textContent = data.event_name ? `Текущее событие: ${{data.event_name}}` : "Текущее событие: неизвестно";
  root.querySelector('[data-role="source"]').textContent = data.from_cache ? "Источник: кеш" : "Источник: Wiki";
  root.querySelector('[data-field="target"]').value = intValue(initial.target) || intValue(data.shop_total);
  root.querySelector('[data-field="owned"]').value = intValue(initial.owned);
  root.querySelector('[data-field="end-date"]').value = (initial.end_date || data.end_date || "").replaceAll("/", "-").slice(0, 10);

  renderDailyTable("daily", state.daily);
  renderDailyTable("extra", state.extra);
  renderShopTable();
  renderStages();

  root.addEventListener("input", function(event) {{
    const target = event.target;
    if (target.matches("[data-shop-number]")) setShopValue(Number(target.dataset.shopNumber), target.value);
    else if (target.matches("[data-shop-range]")) setShopValue(Number(target.dataset.shopRange), target.value);
    else calculate();
  }});
  root.addEventListener("change", function(event) {{
    const target = event.target;
    const role = target.dataset.role;
    if ((role === "daily" || role === "extra") && target.dataset.field) {{
      state[role][Number(target.dataset.index)][target.dataset.field] = target.checked;
    }}
    calculate();
  }});
  root.querySelector('[data-action="import-shop"]').addEventListener("click", function() {{
    root.querySelector('[data-field="target"]').value = shopTotal();
    calculate();
  }});
  root.querySelector('[data-action="clear-shop"]').addEventListener("click", function() {{
    state.shop.forEach((_, index) => setShopValue(index, 0));
  }});
  root.querySelector('[data-action="fill-shop"]').addEventListener("click", function() {{
    state.shop.forEach((item, index) => setShopValue(index, item.quantity || 0));
  }});

  window.alasEventCalculator[scopeId] = {{
    getState: function() {{
      const shopFilter = selectedShopFilter();
      return {{
        target: intValue(root.querySelector('[data-field="target"]').value),
        owned: intValue(root.querySelector('[data-field="owned"]').value),
        endDate: root.querySelector('[data-field="end-date"]').value,
        shopTotal: shopTotal(),
        shopFilter: shopFilter.filters,
        shopFilterMissing: shopFilter.missing
      }};
    }}
  }};
  calculate();
}})();
"""


def build_error_html(message: str) -> str:
    return (
        '<div style="border:1px solid rgba(200,80,80,.35);border-radius:6px;'
        'padding:10px;margin-top:10px;">'
        f"Не удалось загрузить данные калькулятора события: {escape(message)}"
        "</div>"
    )
