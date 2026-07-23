"""WebUI 委托收益统计视图。"""

from module.webui.app_dependencies import (
    alas_instance,
    datetime,
    logger,
    put_button,
    put_buttons,
    put_html,
    put_text,
    t,
    use_scope,
)


from module.webui.app_types import WebUIMixinBase


class CommissionIncomeStatisticsMixin(WebUIMixinBase):
    """WebUI 委托收益统计视图。"""

    def _render_commission_income(self):
        try:
            from datetime import datetime
            from module.statistics.commission_income_stats import (
                get_commission_income_summary,
                get_recent_commission_entries,
                COMMISSION_ITEM_META,
                COMMISSION_ITEM_NAME_MAP,
                COMMISSION_TRACKED_ITEMS,
            )

            instance_name = getattr(self, "alas_name", None)
            if not instance_name:
                from module.config.utils import alas_instance

                all_instances = alas_instance()
                instance_name = all_instances[0] if all_instances else None
            if not instance_name:
                with use_scope("commission_income", clear=True):
                    put_text(t("Gui.Stat.CommissionIncomeNoData"))
                return

            item_name_map = {
                "Gem": t("Gui.Stat.CommissionIncomeItemGem"),
                "Cube": t("Gui.Stat.CommissionIncomeItemCube"),
                "Chip": t("Gui.Stat.CommissionIncomeItemChip"),
                "Oil": t("Gui.Stat.CommissionIncomeItemOil"),
                "Coin": t("Gui.Stat.CommissionIncomeItemCoin"),
            }
            item_icon_map = {
                "Gem": "static/assets/gui/icon/icon_1.png",
                "Cube": "static/assets/gui/icon/icon_2.png",
                "Chip": "static/assets/gui/icon/icon_3.png",
                "Oil": "static/assets/gui/icon/icon_4.png",
                "Coin": "static/assets/gui/icon/icon_5.png",
            }

            period = self._commission_income_period
            summary = get_commission_income_summary(instance_name, period=period)
            recent = get_recent_commission_entries(instance_name, limit=10)

            with use_scope("commission_income", clear=True):
                html = """
                <style>
                    #commission_income_container > div,
                    #commission_income_container table {
                        width: 100% !important;
                        max-width: 100% !important;
                    }
                    #commission_income_container img {
                        background: transparent !important;
                        border: none !important;
                        box-shadow: none !important;
                        margin: 0 !important;
                        padding: 0 !important;
                    }
                </style>
                <div id="commission_income_container" class="commission-income-summary" style="padding: 0; width: 100%; box-sizing: border-box;">
                """

                html += f'<div style="font-size: 1rem; font-weight: 500; color: inherit; margin-bottom: 14px; padding-bottom: 8px; border-bottom: 1px solid rgba(128, 128, 128, 0.2);">{t("Gui.Stat.CommissionIncomeTitle")}</div>'

                rows = summary.get("detail_rows", [])
                has_data = rows and not all(r["total"] == 0 for r in rows)

                html += '<div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr)); gap: 12px; margin-bottom: 20px; width: 100%;">'
                for row in rows:
                    display_name = item_name_map.get(row["name"], row["name"])
                    icon_path = item_icon_map.get(row["name"], "")
                    total_str = f"+{row['total']:,}" if row["total"] > 0 else "0"

                    icon_html = (
                        (
                            f'<div style="width: 34px; height: 34px; display: flex; align-items: center; justify-content: center; background: {row["color"]}1a; border-radius: 8px; flex-shrink: 0;">'
                            f'<img src="{icon_path}" style="width: 24px; height: 24px; object-fit: contain; background: transparent;">'
                            f"</div>"
                        )
                        if icon_path
                        else f'<div style="width: 12px; height: 12px; border-radius: 50%; background: {row["color"]}; flex-shrink: 0;"></div>'
                    )

                    html += f"""
                    <div class="commission-income-metric-card" style="display: flex; align-items: center; gap: 10px; padding: 12px 14px; background: rgba(128, 128, 128, 0.05); border-radius: 6px; border: 1px solid rgba(128, 128, 128, 0.15);">
                        {icon_html}
                        <div style="display: flex; flex-direction: column; gap: 1px;">
                            <span style="font-size: 0.78rem; opacity: 0.65;">{display_name}</span>
                            <span style="font-size: 1.15rem; font-weight: 400; color: inherit;">{total_str}</span>
                        </div>
                    </div>"""
                html += "</div>"

                put_html(html)

                def on_period_click(p):
                    self._commission_income_period = p
                    self._render_commission_income()

                put_buttons(
                    [
                        {
                            "label": t("Gui.Stat.CommissionIncomeDay"),
                            "value": "day",
                            "color": "primary" if period == "day" else "secondary",
                        },
                        {
                            "label": t("Gui.Stat.CommissionIncomeWeek"),
                            "value": "week",
                            "color": "primary" if period == "week" else "secondary",
                        },
                        {
                            "label": t("Gui.Stat.CommissionIncomeMonth"),
                            "value": "month",
                            "color": "primary" if period == "month" else "secondary",
                        },
                    ],
                    onclick=on_period_click,
                    small=True,
                    scope="commission_income",
                )

                html2 = '<div class="commission-income-table-wrap" style="width: 100% !important; max-width: none !important; display: block !important; box-sizing: border-box;">'
                if not has_data:
                    html2 += f'<p style="margin: 12px 0; opacity: 0.6; font-size: 13px;">{t("Gui.Stat.CommissionIncomeNoData")}</p>'
                else:
                    html2 += '<table class="commission-income-table" style="width: 100% !important; max-width: none !important; border-collapse: collapse; font-size: 0.85rem; table-layout: fixed; display: table;">'
                    html2 += '<colgroup><col style="width: 40%;"><col style="width: 20%;"><col style="width: 20%;"><col style="width: 20%;"></colgroup>'
                    html2 += "<thead><tr>"
                    html2 += f'<th style="text-align: left !important; padding: 8px 10px; background: rgba(128, 128, 128, 0.1); border-bottom: 1px solid rgba(128, 128, 128, 0.2); font-weight: 500; opacity: 0.8; font-size: 0.8rem;">{t("Gui.Stat.CommissionIncomeHeaderItem")}</th>'
                    html2 += f'<th style="text-align: right !important; padding: 8px 10px; background: rgba(128, 128, 128, 0.1); border-bottom: 1px solid rgba(128, 128, 128, 0.2); font-weight: 500; opacity: 0.8; font-size: 0.8rem;">{t("Gui.Stat.CommissionIncomeHeaderTotal")}</th>'
                    html2 += f'<th style="text-align: right !important; padding: 8px 10px; background: rgba(128, 128, 128, 0.1); border-bottom: 1px solid rgba(128, 128, 128, 0.2); font-weight: 500; opacity: 0.8; font-size: 0.8rem;">{t("Gui.Stat.CommissionIncomeHeaderCount")}</th>'
                    html2 += f'<th style="text-align: right !important; padding: 8px 10px; background: rgba(128, 128, 128, 0.1); border-bottom: 1px solid rgba(128, 128, 128, 0.2); font-weight: 500; opacity: 0.8; font-size: 0.8rem;">{t("Gui.Stat.CommissionIncomeHeaderAvg")}</th>'
                    html2 += "</tr></thead><tbody>"

                    for row in rows:
                        if row["total"] == 0:
                            continue
                        display_name = item_name_map.get(row["name"], row["name"])
                        icon_path = item_icon_map.get(row["name"], "")

                        icon_html = (
                            (
                                f'<div style="width: 24px; height: 24px; display: flex; align-items: center; justify-content: center; background: {row["color"]}1a; border-radius: 4px; flex-shrink: 0;">'
                                f'<img src="{icon_path}" style="width: 18px; height: 18px; object-fit: contain; background: transparent;">'
                                f"</div>"
                            )
                            if icon_path
                            else f'<div style="width: 8px; height: 8px; border-radius: 50%; background: {row["color"]}; flex-shrink: 0;"></div>'
                        )

                        html2 += '<tr style="border-bottom: 1px solid rgba(128, 128, 128, 0.1);">'
                        html2 += f'<td style="padding: 7px 10px;"><div style="display: flex; align-items: center; gap: 6px;">{icon_html}{display_name}</div></td>'
                        html2 += f'<td style="padding: 7px 10px; text-align: right; font-family: monospace;">{row["total"]:,}</td>'
                        html2 += f'<td style="padding: 7px 10px; text-align: right; font-family: monospace; opacity: 0.7;">{row["count"]}</td>'
                        html2 += f'<td style="padding: 7px 10px; text-align: right; font-family: monospace; opacity: 0.7;">{row["avg"]}</td>'
                        html2 += "</tr>"

                    html2 += "</tbody></table>"

                put_html(html2, scope="commission_income")

                put_button(
                    t("Gui.Stat.Refresh"),
                    onclick=self._render_commission_income,
                    color="secondary",
                    small=True,
                    scope="commission_income",
                )

                html3 = '<div class="commission-income-recent" style="width: 100% !important; max-width: none !important; display: block !important; box-sizing: border-box;">'
                if recent:
                    html3 += f'<div style="height: 1px; background: rgba(128, 128, 128, 0.2); margin: 24px 0;"></div>'
                    html3 += f'<div style="font-size: 0.9rem; font-weight: 500; color: inherit; margin-bottom: 10px;">{t("Gui.Stat.CommissionIncomeRecentTitle")}</div>'
                    html3 += '<div style="font-size: 13px; width: 100%;">'
                    for entry in recent:
                        ts = entry.get("ts", "")
                        try:
                            dt = datetime.fromisoformat(ts)
                            time_str = dt.strftime("%m-%d %H:%M")
                        except Exception:
                            time_str = ts[:16] if ts else "--"
                        items = entry.get("items", {})
                        item_parts = []
                        for raw_name, amount in items.items():
                            if not amount or int(amount) <= 0:
                                continue
                            mapped_name = COMMISSION_ITEM_NAME_MAP.get(
                                raw_name, raw_name
                            )
                            if mapped_name not in COMMISSION_TRACKED_ITEMS:
                                continue
                            meta = COMMISSION_ITEM_META.get(
                                mapped_name, {"color": "#888"}
                            )
                            icon_path = item_icon_map.get(mapped_name, "")
                            display = item_name_map.get(mapped_name, mapped_name)

                            icon_html = (
                                (
                                    f'<div style="width: 22px; height: 22px; display: inline-flex; align-items: center; justify-content: center; background: {meta["color"]}1a; border-radius: 4px; margin-right: 6px; vertical-align: middle;">'
                                    f'<img src="{icon_path}" style="width: 16px; height: 16px; object-fit: contain; background: transparent;">'
                                    f"</div>"
                                )
                                if icon_path
                                else f'<span style="display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: {meta["color"]}; margin-right: 4px;"></span>'
                            )

                            item_parts.append(
                                f'<span style="display: inline-flex; align-items: center; margin-right: 12px; height: 24px;">'
                                f"{icon_html}"
                                f'<span style="color: inherit;">{display}</span>'
                                f'<span style="opacity: 0.65; margin-left: 2px;">x{int(amount)}</span>'
                                f"</span>"
                            )
                        items_str = (
                            "".join(item_parts)
                            if item_parts
                            else '<span style="opacity: 0.6;">--</span>'
                        )
                        html3 += (
                            f'<div class="commission-income-recent-row" style="display: flex; align-items: center; padding: 6px 0; border-bottom: 1px solid rgba(128, 128, 128, 0.1);">'
                            f'<span style="opacity: 0.65; min-width: 80px; font-size: 12px;">{time_str}</span>'
                            f'<span style="flex: 1;">{items_str}</span>'
                            f"</div>"
                        )
                    html3 += "</div>"

                html3 += f'<p style="font-size: 0.75rem; opacity: 0.5; margin-top: 10px;">{t("Gui.Stat.CommissionIncomeTotalCommissions", value=summary["total_commissions"])}</p>'
                html3 += "</div>"
                put_html(html3, scope="commission_income")

        except Exception as e:
            with use_scope("commission_income", clear=True):
                put_text(t("Gui.Stat.CommissionIncomeNoData"))
                logger.warning(f"Commission income render failed: {e}")
