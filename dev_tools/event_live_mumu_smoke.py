from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cv2

from module.base.timer import Timer
from module.campaign.run import CampaignRun
from module.config.config import AzurLaneConfig
from module.config.time_source import now as current_time
from module.config.utils import DEFAULT_CONFIG_NAME
from module.device.device import Device
from module.event_datamine.campaign_selector import resolve_generated_campaign_module
from module.event_datamine.registry import EventArtifactRegistry
from module.logger import logger
from module.map.assets import MAP_PREPARATION
from module.shop.assets import NAV_EVENT, NAV_GENERAL
from module.shop_event.assets import NO_NAV_EVENT_CHECK
from module.shop_event.shop_event import EventShop
from module.ui.assets import SHOP_GOTO_MUNITIONS
from module.ui.page import page_munitions, page_shop
from module.ui.ui import UI


@dataclass(frozen=True)
class ShopIdentity:
    tab: int
    name: str
    amount: int
    price: int
    cost: str
    total_count: int
    group: str
    sub_genre: str
    tier: str


@dataclass(frozen=True)
class GeneratedStage:
    stage: str
    expected_module: str


class SmokeFailure(RuntimeError):
    pass


class SmokeReport:
    def __init__(self, root: Path, config_name: str) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "report.json"
        self.data: dict[str, Any] = {
            "schema_version": 1,
            "started_at": datetime.now().astimezone().isoformat(),
            "config": config_name,
            "checks": [],
        }

    def add(self, section: str, name: str, status: str, **details: Any) -> None:
        record = {
            "section": section,
            "name": name,
            "status": status,
            **details,
        }
        self.data["checks"].append(record)
        logger.info(f"[Live smoke] {section}/{name}: {status}")
        if details:
            logger.info(f"[Live smoke] {details}")
        self.flush()

    def screenshot(self, device: Device, label: str) -> str | None:
        safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in label)
        path = self.root / f"{safe}.png"
        try:
            device.screenshot()
            image = getattr(device, "image", None)
            if image is None:
                return None
            cv2.imwrite(str(path), cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
            return str(path)
        except (OSError, TypeError, ValueError, cv2.error) as exc:
            logger.warning(f"[Live smoke] Не удалось сохранить screenshot {label}: {exc}")
            return None

    def flush(self) -> None:
        self.data["updated_at"] = datetime.now().astimezone().isoformat()
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def finish(self) -> dict[str, int]:
        counters: dict[str, int] = {}
        for item in self.data["checks"]:
            status = str(item.get("status") or "UNKNOWN")
            counters[status] = counters.get(status, 0) + 1
        self.data["finished_at"] = datetime.now().astimezone().isoformat()
        self.data["summary"] = counters
        self.flush()
        return counters


def _parse_quantities(raw: str) -> list[int]:
    values: list[int] = []
    for token in str(raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        value = int(token)
        if value <= 0:
            raise argparse.ArgumentTypeError("Количество покупки должно быть положительным")
        values.append(value)
    if not values:
        raise argparse.ArgumentTypeError("Нужно указать хотя бы одно количество")
    return values


def _item_int(item: Any, name: str, default: int = 0) -> int:
    try:
        return int(getattr(item, name, default) or 0)
    except (TypeError, ValueError, OverflowError):
        return default


def _item_text(item: Any, name: str) -> str:
    return str(getattr(item, name, "") or "")


def _shop_identity(item: Any, tab: int) -> ShopIdentity:
    return ShopIdentity(
        tab=tab,
        name=_item_text(item, "name"),
        amount=_item_int(item, "amount", 1),
        price=_item_int(item, "price"),
        cost=_item_text(item, "cost"),
        total_count=_item_int(item, "total_count"),
        group=_item_text(item, "group"),
        sub_genre=_item_text(item, "sub_genre"),
        tier=_item_text(item, "tier"),
    )


def _identity_dict(identity: ShopIdentity) -> dict[str, Any]:
    return {
        "tab": identity.tab,
        "name": identity.name,
        "amount": identity.amount,
        "price": identity.price,
        "cost": identity.cost,
        "total_count": identity.total_count,
        "group": identity.group,
        "sub_genre": identity.sub_genre,
        "tier": identity.tier,
    }


def _same_identity(item: Any, identity: ShopIdentity, tab: int) -> bool:
    return _shop_identity(item, tab) == identity


def _safe_purchase_item(item: Any, quantity: int, max_unit_price: int) -> bool:
    name = _item_text(item, "name")
    price = _item_int(item, "price")
    count = _item_int(item, "count")
    total_count = _item_int(item, "total_count")
    if not name or name.isdecimal():
        return False
    if _item_text(item, "cost") != "pt":
        return False
    if bool(getattr(item, "is_ship", False)):
        return False
    if _item_text(item, "tag"):
        return False
    if price <= 0 or price > max_unit_price:
        return False
    if count < quantity or total_count <= 1:
        return False
    return True


def _open_event_shop(shop: EventShop, report: SmokeReport) -> tuple[int, Any]:
    logger.hr("Live smoke: переход в Event Shop", level=1)
    shop.ui_goto_main()
    report.screenshot(shop.device, "shop_00_main")
    shop.ui_ensure(page_shop)

    reached_munitions = False
    timeout = Timer(2, count=4)
    for _ in shop.loop(timeout=30):
        if shop.appear(page_munitions.check_button, threshold=20):
            reached_munitions = True
            break
        if timeout.reached():
            shop.device.click(SHOP_GOTO_MUNITIONS)
            timeout.reset()
    if not reached_munitions:
        raise SmokeFailure("Не удалось открыть страницу Munitions из главного меню")

    if shop.appear(NAV_GENERAL, offset=(5, 5)):
        if shop.appear(NO_NAV_EVENT_CHECK, offset=(5, 5)):
            raise SmokeFailure("Активный Event Shop отсутствует")
        shop.ui_click(NAV_EVENT, check_button=NAV_EVENT, appear_button=NAV_GENERAL)

    shop.device.screenshot()
    count, navbar = shop.event_shop_tab_count_and_navbar
    if int(count) <= 0:
        raise SmokeFailure("Не удалось определить вкладки Event Shop")
    report.screenshot(shop.device, "shop_01_event_shop")
    return int(count), navbar


def _scan_shop_tab(
    shop: EventShop,
    navbar: Any,
    tab: int,
    *,
    report: SmokeReport | None = None,
    screenshot_label: str | None = None,
) -> list[Any]:
    navbar.set(main=shop, left=tab)
    shop.pt_preserved = 0
    shop.get_current_pts()
    runtime_items = shop.scan_all()
    observations = list(getattr(runtime_items, "observation_items", runtime_items))
    if report is not None and screenshot_label:
        report.screenshot(shop.device, screenshot_label)
    return observations


def _collect_shop_inventory(
    shop: EventShop,
    count: int,
    navbar: Any,
) -> list[tuple[int, Any]]:
    inventory: list[tuple[int, Any]] = []
    for tab in range(1, count + 1):
        for item in _scan_shop_tab(shop, navbar, tab):
            inventory.append((tab, item))
    return inventory


def _find_fresh_item(
    shop: EventShop,
    navbar: Any,
    identity: ShopIdentity,
) -> Any:
    items = _scan_shop_tab(shop, navbar, identity.tab)
    matches = [item for item in items if _same_identity(item, identity, identity.tab)]
    if len(matches) != 1:
        raise SmokeFailure(
            "Live inventory не содержит ровно один товар с выбранной identity: "
            f"{_identity_dict(identity)}, matches={len(matches)}"
        )
    return matches[0]


def _choose_purchase_candidate(
    inventory: list[tuple[int, Any]],
    *,
    quantity: int,
    used_names: set[str],
    budget_left: int,
    current_pt: int,
    max_unit_price: int,
) -> tuple[ShopIdentity, int] | None:
    candidates: list[tuple[int, ShopIdentity]] = []
    for tab, item in inventory:
        if not _safe_purchase_item(item, quantity, max_unit_price):
            continue
        identity = _shop_identity(item, tab)
        if identity.name in used_names:
            continue
        spend = identity.price * quantity
        if spend > budget_left or spend > current_pt:
            continue
        candidates.append((spend, identity))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: (pair[0], pair[1].price, pair[1].name, pair[1].tab))
    spend, identity = candidates[0]
    return identity, spend


def _remaining_count(
    shop: EventShop,
    navbar: Any,
    identity: ShopIdentity,
) -> tuple[int, int]:
    items = _scan_shop_tab(shop, navbar, identity.tab)
    matches = [item for item in items if _same_identity(item, identity, identity.tab)]
    if len(matches) > 1:
        raise SmokeFailure(
            f"После покупки найдено несколько товаров одной identity: {_identity_dict(identity)}"
        )
    if not matches:
        return 0, 0
    return _item_int(matches[0], "count"), 1


def _run_purchase_scenario(
    shop: EventShop,
    navbar: Any,
    identity: ShopIdentity,
    quantity: int,
    *,
    execute: bool,
    report: SmokeReport,
    index: int,
) -> int:
    item = _find_fresh_item(shop, navbar, identity)
    before_count = _item_int(item, "count")
    shop.pt_preserved = 0
    shop.get_current_pts()
    before_pt = int(shop.pt)
    affordable = int(shop.calculate_affordable_amount(item))
    if affordable < quantity:
        raise SmokeFailure(
            f"Перед покупкой production guard разрешает только {affordable}, "
            f"запрошено {quantity}: {_identity_dict(identity)}"
        )

    expected_spend = identity.price * quantity
    details = {
        "item": _identity_dict(identity),
        "quantity": quantity,
        "before_count": before_count,
        "before_pt": before_pt,
        "expected_spend": expected_spend,
    }
    if not execute:
        report.add("shop", f"purchase_{index}_x{quantity}", "PLANNED", **details)
        return 0

    report.screenshot(shop.device, f"shop_purchase_{index}_before")
    shop.event_shop_buy_item(item, amount=quantity)
    shop.get_current_pts()
    after_pt = int(shop.pt)
    after_count, match_count = _remaining_count(shop, navbar, identity)
    expected_count = max(before_count - quantity, 0)
    actual_spend = before_pt - after_pt

    if actual_spend != expected_spend:
        raise SmokeFailure(
            f"PT delta после покупки неверен: expected={expected_spend}, actual={actual_spend}"
        )
    if after_count != expected_count:
        raise SmokeFailure(
            f"Остаток товара после покупки неверен: expected={expected_count}, actual={after_count}"
        )
    if expected_count > 0 and match_count != 1:
        raise SmokeFailure("Товар с ненулевым остатком исчез после покупки")

    report.screenshot(shop.device, f"shop_purchase_{index}_after")
    report.add(
        "shop",
        f"purchase_{index}_x{quantity}",
        "PASS",
        **details,
        after_count=after_count,
        after_pt=after_pt,
        actual_spend=actual_spend,
    )
    return actual_spend


def _run_insufficient_guard_scenario(
    shop: EventShop,
    count: int,
    navbar: Any,
    *,
    max_unit_price: int,
    report: SmokeReport,
) -> None:
    inventory = _collect_shop_inventory(shop, count, navbar)
    eligible = [
        (tab, item)
        for tab, item in inventory
        if _safe_purchase_item(item, 1, max_unit_price)
    ]
    if not eligible:
        report.add(
            "shop",
            "insufficient_funds_guard",
            "SKIP",
            reason="Нет безопасного PT-товара для проверки",
        )
        return

    natural: tuple[int, Any] | None = None
    shop.pt_preserved = 0
    shop.get_current_pts()
    real_pt = int(shop.pt)
    for tab, item in eligible:
        if _item_int(item, "price") > real_pt:
            natural = (tab, item)
            break

    tab, selected = natural or min(
        eligible,
        key=lambda pair: (_item_int(pair[1], "price"), _item_text(pair[1], "name")),
    )
    identity = _shop_identity(selected, tab)
    item = _find_fresh_item(shop, navbar, identity)
    shop.get_current_pts()
    before_pt = int(shop.pt)
    before_count = _item_int(item, "count")

    if natural is None:
        shop.pt_preserved = before_pt
        mode = "synthetic_reservation"
    else:
        shop.pt_preserved = 0
        mode = "natural_balance"

    affordable = int(shop.calculate_affordable_amount(item))
    target_amount = 1
    buy_amount = min(affordable, target_amount)
    if buy_amount != 0:
        raise SmokeFailure(
            f"Production guard разрешил покупку при недостатке доступных PT: affordable={affordable}"
        )

    # Это именно production-level попытка: кандидат дошёл до того же affordability
    # guard, который используется EventShop._run(). При нуле дальнейший click/confirm
    # запрещён — smoke не должен создавать отдельный небезопасный путь.
    shop.pt_preserved = 0
    shop.get_current_pts()
    after_pt = int(shop.pt)
    after_count, _ = _remaining_count(shop, navbar, identity)
    if after_pt != before_pt or after_count != before_count:
        raise SmokeFailure("Отказ по недостатку средств изменил PT или остаток товара")

    report.add(
        "shop",
        "insufficient_funds_guard",
        "PASS",
        mode=mode,
        item=_identity_dict(identity),
        real_pt=before_pt,
        effective_affordable=affordable,
        stock=before_count,
    )


def run_shop_smoke(
    config: AzurLaneConfig,
    device: Device,
    *,
    execute: bool,
    quantities: list[int],
    max_spend: int,
    max_unit_price: int,
    strict: bool,
    report: SmokeReport,
) -> None:
    logger.hr("LIVE MUMU SMOKE — EVENT SHOP", level=0)
    config.init_task("EventShop")
    config.override(SHOP_EXTRACT_TEMPLATE=False)
    shop = EventShop(config=config, device=device)
    shop._begin_event_shop_pass_context()

    count, navbar = _open_event_shop(shop, report)
    report.add("shop", "open_from_main", "PASS", tabs=count)

    used_names: set[str] = set()
    spent_total = 0
    for index, quantity in enumerate(quantities, start=1):
        inventory = _collect_shop_inventory(shop, count, navbar)
        shop.pt_preserved = 0
        shop.get_current_pts()
        current_pt = int(shop.pt)
        candidate = _choose_purchase_candidate(
            inventory,
            quantity=quantity,
            used_names=used_names,
            budget_left=max(max_spend - spent_total, 0),
            current_pt=current_pt,
            max_unit_price=max_unit_price,
        )
        if candidate is None:
            report.add(
                "shop",
                f"purchase_{index}_x{quantity}",
                "SKIP",
                reason="Нет безопасного distinct PT-товара в заданном бюджете/остатке",
                current_pt=current_pt,
                budget_left=max(max_spend - spent_total, 0),
            )
            if strict:
                raise SmokeFailure(f"Не найден товар для обязательного сценария x{quantity}")
            continue

        identity, planned_spend = candidate
        used_names.add(identity.name)
        if planned_spend + spent_total > max_spend:
            raise SmokeFailure("Внутренняя ошибка smoke: выбран товар выше общего PT-бюджета")
        actual_spend = _run_purchase_scenario(
            shop,
            navbar,
            identity,
            quantity,
            execute=execute,
            report=report,
            index=index,
        )
        spent_total += actual_spend

    _run_insufficient_guard_scenario(
        shop,
        count,
        navbar,
        max_unit_price=max_unit_price,
        report=report,
    )

    shop.ui_goto_main()
    report.screenshot(device, "shop_99_back_main")
    report.add(
        "shop",
        "return_to_main",
        "PASS",
        executed=execute,
        spent_pt=spent_total,
        max_spend=max_spend,
    )


def _verified_generated_stages(artifact: dict[str, Any]) -> list[GeneratedStage]:
    metadata = artifact.get("metadata")
    if not isinstance(metadata, dict):
        raise SmokeFailure("Current Event artifact не содержит metadata")
    generated = metadata.get("generated_maps")
    if not isinstance(generated, list):
        raise SmokeFailure("Current Event artifact не содержит generated_maps")

    stages: list[GeneratedStage] = []
    seen: set[str] = set()
    for raw in generated:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("source_status") or "") != "verified":
            continue
        module = str(raw.get("module") or "").strip()
        if not module:
            continue
        path = PurePosixPath(module)
        stage = path.stem.lower()
        expected = "campaign.generated_event." + ".".join(path.with_suffix("").parts)
        if stage in seen:
            raise SmokeFailure(f"Generated stages содержат повторяющийся stem: {stage}")
        seen.add(stage)
        stages.append(GeneratedStage(stage=stage, expected_module=expected))
    if not stages:
        raise SmokeFailure("Current Event artifact не содержит verified generated maps")
    return stages


def _load_event_args() -> dict[str, Any]:
    path = ROOT / "module" / "config" / "argument" / "args.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SmokeFailure(f"Не удалось прочитать {path}: {exc}") from exc


def _find_current_event_selector(
    server: str,
    stages: list[GeneratedStage],
    now: datetime,
) -> str:
    args_data = _load_event_args()
    event_options = (
        args_data.get("Event", {})
        .get("Campaign", {})
        .get("Event", {})
    )
    if not isinstance(event_options, dict):
        raise SmokeFailure("args.json не содержит Event.Campaign.Event")
    options = event_options.get(f"option_{server.lower()}")
    if not isinstance(options, list):
        raise SmokeFailure(f"args.json не содержит event selectors для сервера {server}")

    probes = stages[: min(3, len(stages))]
    matches: list[str] = []
    for selector_raw in options:
        selector = str(selector_raw or "").strip()
        if not selector.startswith("event_"):
            continue
        resolved_all = True
        for probe in probes:
            resolved = resolve_generated_campaign_module(
                selector,
                probe.stage,
                now=now,
                args_data=args_data,
            )
            if resolved != probe.expected_module:
                resolved_all = False
                break
        if resolved_all:
            matches.append(selector)

    if len(matches) != 1:
        raise SmokeFailure(
            f"Current generated Event selector разрешён неоднозначно: {matches}"
        )
    return matches[0]


def _probe_stage_preparation(
    campaign: Any,
    *,
    stage: str,
    report: SmokeReport,
) -> None:
    logger.hr(f"Live smoke: preparation probe {stage}", level=2)
    campaign.ENTRANCE.area = campaign.ENTRANCE.button
    campaign.device.click(campaign.ENTRANCE)
    found = False
    for _ in campaign.loop(skip_first=False, timeout=25):
        if campaign.appear(MAP_PREPARATION, offset=(20, 20)):
            found = True
            break
        if campaign.handle_story_skip():
            continue
        if campaign.handle_info_bar():
            continue
    if not found:
        raise SmokeFailure(f"Для этапа {stage} не появился экран подготовки карты")

    campaign.map_get_info()
    report.screenshot(campaign.device, f"map_preparation_{stage}")
    campaign.enter_map_cancel(skip_first_screenshot=True)


def run_map_smoke(
    config: AzurLaneConfig,
    device: Device,
    *,
    requested_stages: list[str] | None,
    preparation_probe: bool,
    report: SmokeReport,
) -> None:
    logger.hr("LIVE MUMU SMOKE — CURRENT EVENT MAPS", level=0)
    config.init_task("Event")
    navigator = UI(config=config, device=device)
    navigator.ui_goto_main()
    report.screenshot(device, "map_00_main")

    server = str(getattr(config, "SERVER", "EN") or "EN").upper()
    now = current_time()
    artifact = EventArtifactRegistry().resolve_current(server, now, supplemental=False)
    if artifact is None:
        raise SmokeFailure(f"Current Event artifact не найден для {server}")
    stages = _verified_generated_stages(artifact)
    selector = _find_current_event_selector(server, stages, now)

    if requested_stages:
        wanted = {stage.lower() for stage in requested_stages}
        stages = [item for item in stages if item.stage in wanted]
        missing = sorted(wanted - {item.stage for item in stages})
        if missing:
            raise SmokeFailure(f"Запрошенные generated stages отсутствуют: {missing}")
    if not stages:
        raise SmokeFailure("После фильтра stage list пуст")

    spec = artifact.get("event_spec", {})
    report.add(
        "map",
        "resolve_current_event",
        "PASS",
        server=server,
        event_id=str(spec.get("id") or ""),
        event_name=str(spec.get("name") or ""),
        selector=selector,
        stage_count=len(stages),
    )

    runner = CampaignRun(config=config, device=device)
    prepared_stage: str | None = None
    for index, generated in enumerate(stages, start=1):
        try:
            loaded = runner.load_campaign(generated.stage, folder=selector)
            if not loaded and runner.module is None:
                raise SmokeFailure(f"CampaignRun не загрузил {generated.stage}")
            actual_module = str(getattr(runner.module, "__name__", ""))
            if actual_module != generated.expected_module:
                raise SmokeFailure(
                    f"Alias {selector}/{generated.stage} -> {actual_module}, "
                    f"ожидался {generated.expected_module}"
                )
            campaign = runner.campaign
            campaign.ensure_campaign_ui(
                generated.stage,
                mode="normal",
                skip_first_screenshot=False,
            )
            entrance = getattr(campaign, "ENTRANCE", None)
            if entrance is None or not getattr(entrance, "button", None):
                raise SmokeFailure(f"Для {generated.stage} не разрешён stage entrance")

            report.screenshot(device, f"map_{index:02d}_{generated.stage}")
            report.add(
                "map",
                f"stage_{generated.stage}",
                "PASS",
                selector=selector,
                expected_module=generated.expected_module,
                actual_module=actual_module,
                map_name=str(getattr(campaign.MAP, "name", "")),
            )

            if preparation_probe and prepared_stage is None:
                _probe_stage_preparation(
                    campaign,
                    stage=generated.stage,
                    report=report,
                )
                prepared_stage = generated.stage
                report.add(
                    "map",
                    "preparation_probe",
                    "PASS",
                    stage=generated.stage,
                    note="MAP_PREPARATION открыт и отменён до входа на карту; бой не запускался",
                )
        except Exception as exc:
            report.add(
                "map",
                f"stage_{generated.stage}",
                "FAIL",
                error=f"{type(exc).__name__}: {exc}",
                expected_module=generated.expected_module,
            )
            raise

    navigator.ui_goto_main()
    report.screenshot(device, "map_99_back_main")
    report.add("map", "return_to_main", "PASS")


def _parse_stage_filter(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    stages = [token.strip().lower() for token in raw.split(",") if token.strip()]
    return stages or None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Живой smoke текущего Event на MuMu: EventShop + generated Event maps. "
            "Стартует из главного меню и сохраняет JSON/screenshots в log/."
        )
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG_NAME)
    parser.add_argument("--skip-shop", action="store_true")
    parser.add_argument("--skip-map", action="store_true")
    parser.add_argument(
        "--execute-shop",
        action="store_true",
        help="Разрешить реальные покупки. Без флага выполняются live scan + plan + negative guard.",
    )
    parser.add_argument(
        "--quantities",
        default="1,2,3",
        help="Количество для distinct purchase-сценариев, например 1,2,3.",
    )
    parser.add_argument(
        "--max-shop-spend",
        type=int,
        default=3000,
        help="Жёсткий суммарный PT-бюджет destructive smoke.",
    )
    parser.add_argument(
        "--max-unit-price",
        type=int,
        default=1000,
        help="Максимальная цена одной автоматически выбранной позиции.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Считать отсутствие безопасного товара для purchase-сценария ошибкой.",
    )
    parser.add_argument(
        "--map-stages",
        default=None,
        help="Опциональный список generated stage stems через запятую; по умолчанию все verified.",
    )
    parser.add_argument(
        "--no-map-preparation-probe",
        action="store_true",
        help="Не открывать MAP_PREPARATION первого generated stage.",
    )
    parser.add_argument(
        "--artifacts-dir",
        default="log/event_live_mumu_smoke",
        help="Корень для JSON-отчёта и screenshots.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    quantities = _parse_quantities(args.quantities)
    if args.max_shop_spend < 0:
        parser.error("--max-shop-spend не может быть отрицательным")
    if args.max_unit_price <= 0:
        parser.error("--max-unit-price должен быть положительным")
    if args.skip_shop and args.skip_map:
        parser.error("Нельзя одновременно указать --skip-shop и --skip-map")

    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    report_root = ROOT / args.artifacts_dir / stamp
    report = SmokeReport(report_root, args.config)

    config = AzurLaneConfig(args.config)
    device = Device(config=config)
    failures: list[str] = []

    if not args.skip_shop:
        try:
            run_shop_smoke(
                config,
                device,
                execute=bool(args.execute_shop),
                quantities=quantities,
                max_spend=int(args.max_shop_spend),
                max_unit_price=int(args.max_unit_price),
                strict=bool(args.strict),
                report=report,
            )
        except Exception as exc:
            failures.append(f"shop: {type(exc).__name__}: {exc}")
            report.add(
                "shop",
                "section",
                "FAIL",
                error=f"{type(exc).__name__}: {exc}",
            )
            try:
                UI(config=config, device=device).ui_goto_main()
            except Exception as recovery_exc:
                logger.warning(f"[Live smoke] Shop recovery to main failed: {recovery_exc}")

    if not args.skip_map:
        try:
            run_map_smoke(
                config,
                device,
                requested_stages=_parse_stage_filter(args.map_stages),
                preparation_probe=not bool(args.no_map_preparation_probe),
                report=report,
            )
        except Exception as exc:
            failures.append(f"map: {type(exc).__name__}: {exc}")
            report.add(
                "map",
                "section",
                "FAIL",
                error=f"{type(exc).__name__}: {exc}",
            )
            try:
                UI(config=config, device=device).ui_goto_main()
            except Exception as recovery_exc:
                logger.warning(f"[Live smoke] Map recovery to main failed: {recovery_exc}")

    counters = report.finish()
    logger.hr("LIVE MUMU SMOKE — RESULT", level=0)
    logger.info(f"[Live smoke] Report: {report.path}")
    logger.info(f"[Live smoke] Summary: {counters}")
    if failures:
        for failure in failures:
            logger.error(f"[Live smoke] {failure}")
        return 1
    if args.strict and counters.get("SKIP", 0):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
