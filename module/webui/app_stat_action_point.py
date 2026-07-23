"""WebUI 体力趋势图的数据装配和图表渲染。"""

from module.webui.app_dependencies import (
    alas_instance,
    current_time,
    datetime,
    json,
    put_button,
    put_html,
    put_text,
    run_js,
    t,
    use_scope,
)

from module.webui.app_helpers import (
    build_muted_notice,
    read_webapp_template,
)


from module.webui.app_types import WebUIMixinBase


class ActionPointStatisticsMixin(WebUIMixinBase):
    """WebUI 体力趋势图的数据装配和图表渲染。"""

    def _render_ap_chart(self):
        self.cleanup_client_resources("__apChartCleanups")
        try:
            from module.statistics.opsi_month import (
                get_ap_timeline,
                get_asset_timeline,
                get_coins_timeline,
            )

            instance_name = getattr(self, "alas_name", None)
            if not instance_name:
                from module.config.utils import alas_instance

                all_instances = alas_instance()
                instance_name = all_instances[0] if all_instances else None
            timeline = get_ap_timeline(instance_name=instance_name)
            coins_timeline = get_coins_timeline(instance_name=instance_name)
            asset_timeline = get_asset_timeline(instance_name=instance_name)
        except Exception as e:
            with use_scope("ap_chart", clear=True):
                put_text(t("Gui.Stat.LoadApDataFailed", e=e))
            return

        if not timeline:
            with use_scope("ap_chart", clear=True):
                put_html(build_muted_notice(t("Gui.Stat.NoApData")))
                put_button(
                    t("Gui.Stat.Refresh"), onclick=self._render_ap_chart, color="off"
                )
            return

        def _snapshot_float(point, key):
            value = point.get(key)
            if value is None:
                return None
            return float(value)

        raw_points = []
        for pt in timeline:
            ts_raw = pt.get("ts", "")
            try:
                dt = datetime.fromisoformat(ts_raw)
            except Exception:
                continue
            raw_points.append(
                {
                    "dt": dt,
                    "ap": int(pt.get("ap_total", pt.get("ap", 0))),
                    "source": pt.get("source", "-"),
                }
            )

        if not raw_points:
            with use_scope("ap_chart", clear=True):
                put_html(build_muted_notice(t("Gui.Stat.NoValidApData")))
            return

        raw_points.sort(key=lambda p: p["dt"])
        current_view = getattr(self, "_ap_chart_view", "line")

        labels = []
        opens = []
        highs = []
        lows = []
        closes = []
        counts = []
        ap_list = []
        ap_ts = []
        detail_sources = []
        chart_points = []
        is_detail_mode = False

        today = current_time().date()
        today_points = [p for p in raw_points if p["dt"].date() == today]
        if not today_points and raw_points:
            last_date = raw_points[-1]["dt"].date()
            today_points = [p for p in raw_points if p["dt"].date() == last_date]
            today = last_date

        if current_view == "detail":
            is_detail_mode = True
            if today_points:
                for p in today_points:
                    labels.append(p["dt"].strftime("%H:%M"))
                    ap_list.append(p["ap"])
                    ap_ts.append(int(p["dt"].timestamp() * 1000))
                    detail_sources.append(p.get("source", "-"))
                    chart_points.append(p)
                view_title = t("Gui.Stat.DetailChartTitle")
            else:
                for p in raw_points:
                    labels.append(p["dt"].strftime("%m-%d %H:%M"))
                    ap_list.append(p["ap"])
                    ap_ts.append(int(p["dt"].timestamp() * 1000))
                    chart_points.append(p)
                view_title = t("Gui.Stat.ViewTitleLine")
                is_detail_mode = False
                current_view = "line"
        elif current_view == "line":
            for p in raw_points:
                labels.append(p["dt"].strftime("%m-%d %H:%M"))
                ap_list.append(p["ap"])
                ap_ts.append(int(p["dt"].timestamp() * 1000))
                chart_points.append(p)
            view_title = t("Gui.Stat.ViewTitleLine")
        else:
            from collections import OrderedDict

            candles = OrderedDict()
            if current_view == "day":
                for p in today_points if today_points else raw_points[:24]:
                    hour_key = p["dt"].strftime("%H:00")
                    if hour_key not in candles:
                        candles[hour_key] = {
                            "open": p["ap"],
                            "high": p["ap"],
                            "low": p["ap"],
                            "close": p["ap"],
                            "count": 1,
                        }
                    else:
                        c = candles[hour_key]
                        c["high"] = max(c["high"], p["ap"])
                        c["low"] = min(c["low"], p["ap"])
                        c["close"] = p["ap"]
                        c["count"] += 1
                view_title = t("Gui.Stat.ViewTitleDay", day=today.strftime("%m-%d"))
            else:
                for p in raw_points:
                    day_key = p["dt"].strftime("%m-%d")
                    if day_key not in candles:
                        candles[day_key] = {
                            "open": p["ap"],
                            "high": p["ap"],
                            "low": p["ap"],
                            "close": p["ap"],
                            "count": 1,
                        }
                    else:
                        c = candles[day_key]
                        c["high"] = max(c["high"], p["ap"])
                        c["low"] = min(c["low"], p["ap"])
                        c["close"] = p["ap"]
                        c["count"] += 1
                view_title = t("Gui.Stat.ViewTitleMonth")

            if not candles:
                with use_scope("ap_chart", clear=True):
                    put_html(build_muted_notice(t("Gui.Stat.CannotAggregateKline")))
                    put_button(
                        t("Gui.Stat.ViewLineShort"),
                        onclick=lambda: (
                            setattr(self, "_ap_chart_view", "line"),
                            self._render_ap_chart(),
                        ),
                        color="off",
                    )
                return
            for k, v in candles.items():
                labels.append(k)
                opens.append(v["open"])
                highs.append(v["high"])
                lows.append(v["low"])
                closes.append(v["close"])
                counts.append(v["count"])

        all_ap = [p["ap"] for p in raw_points]
        ap_max = max(all_ap)
        ap_min = min(all_ap)
        ap_avg = int(sum(all_ap) / len(all_ap))
        ap_cur = all_ap[-1]
        if current_view in ("line", "detail"):
            ap_change = ap_list[-1] - ap_list[0] if len(ap_list) >= 2 else 0
            data_points_text = t("Gui.Stat.DataPointsCount", count=len(labels))
        else:
            ap_change = closes[-1] - opens[0] if len(closes) > 0 else 0
            data_points_text = t("Gui.Stat.CandlesCount", count=len(labels))
        change_color = "#ef5350" if ap_change >= 0 else "#26a69a"
        change_sign = "+" if ap_change >= 0 else ""

        yellow_coins_list = []
        purple_coins_list = []
        coins_sources_list = []

        distance_list = []

        asset_list = []
        asset_ts_list = []
        show_coins = False
        coins_stats_html = ""
        coins_legend_html = ""

        distance_raw_points = []
        if current_view in ("line", "detail"):
            for pt in timeline:
                distance_val = pt.get("distance")
                if distance_val is not None:
                    ts_raw = pt.get("ts", "")
                    try:
                        distance_dt = datetime.fromisoformat(ts_raw)
                        distance_raw_points.append(
                            {
                                "dt": distance_dt,
                                "distance": int(distance_val),
                            }
                        )
                    except Exception:
                        continue

        if coins_timeline and chart_points and current_view in ("line", "detail"):
            coins_raw_points = []
            for pt in coins_timeline:
                ts_raw = pt.get("ts", "")
                try:
                    dt = datetime.fromisoformat(ts_raw)
                except Exception:
                    continue
                coins_raw_points.append(
                    {
                        "dt": dt,
                        "yellow_coins": int(pt.get("yellow_coins", 0)),
                        "purple_coins": int(pt["purple_coins"])
                        if "purple_coins" in pt
                        else None,
                        "source": pt.get("source", "-"),
                    }
                )

            if coins_raw_points:
                coins_raw_points.sort(key=lambda p: p["dt"])
                coins_idx = 0
                coins_last = len(coins_raw_points) - 1
                for p in chart_points:
                    while coins_idx < coins_last:
                        cur_delta = abs(
                            (
                                coins_raw_points[coins_idx]["dt"] - p["dt"]
                            ).total_seconds()
                        )
                        next_delta = abs(
                            (
                                coins_raw_points[coins_idx + 1]["dt"] - p["dt"]
                            ).total_seconds()
                        )
                        if next_delta > cur_delta:
                            break
                        coins_idx += 1
                    coins_point = coins_raw_points[coins_idx]
                    yellow_coins_list.append(coins_point["yellow_coins"])
                    purple_coins_list.append(coins_point["purple_coins"])
                    coins_sources_list.append(coins_point.get("source", "-"))

                valid_yellow_coins = [v for v in yellow_coins_list if v is not None]
                valid_purple_coins = [
                    v for v in purple_coins_list if v is not None and v > 0
                ]
                show_coins = bool(valid_yellow_coins or valid_purple_coins)

                if valid_yellow_coins:
                    yc_cur = valid_yellow_coins[-1]
                    yc_change = (
                        valid_yellow_coins[-1] - valid_yellow_coins[0]
                        if len(valid_yellow_coins) >= 2
                        else 0
                    )
                    yc_change_color = "#ef5350" if yc_change >= 0 else "#26a69a"
                    yc_change_sign = "+" if yc_change >= 0 else ""
                    yc_max = max(valid_yellow_coins)
                    yc_min = min(valid_yellow_coins)

                    coins_stats_html += f'<div style="display:grid; grid-template-columns:150px 100px 90px 90px 90px; gap:8px; margin-bottom:2px; font-size:12px; color:#aaa;"><span>黄币: <b style="color:#ffd54f">{yc_cur}</b></span><span>变化: <b style="color:{yc_change_color}">{yc_change_sign}{yc_change}</b></span><span>最高: <b style="color:#ef5350">{yc_max}</b></span><span>最低: <b style="color:#26a69a">{yc_min}</b></span><span></span></div>'
                    coins_legend_html += '<span class="ap-legend-item" data-series="2" style="display:flex; align-items:center; gap:4px;cursor:pointer;opacity:1;"><span style="width:12px; height:2px; background:#ffd54f; border-radius:1px; border-top:1px dashed #ffd54f;"></span>黄币</span>'

                if valid_purple_coins:
                    pc_cur = valid_purple_coins[-1]
                    pc_change = (
                        valid_purple_coins[-1] - valid_purple_coins[0]
                        if len(valid_purple_coins) >= 2
                        else 0
                    )
                    pc_change_color = "#ef5350" if pc_change >= 0 else "#26a69a"
                    pc_change_sign = "+" if pc_change >= 0 else ""
                    pc_max = max(valid_purple_coins)
                    pc_min = min(valid_purple_coins)

                    coins_stats_html += f'<div style="display:grid; grid-template-columns:150px 100px 90px 90px 90px; gap:8px; margin-bottom:2px; font-size:12px; color:#aaa;"><span>紫币: <b style="color:#ce93d8">{pc_cur}</b></span><span>变化: <b style="color:{pc_change_color}">{pc_change_sign}{pc_change}</b></span><span>最高: <b style="color:#ef5350">{pc_max}</b></span><span>最低: <b style="color:#26a69a">{pc_min}</b></span><span></span></div>'
                    coins_legend_html += '<span class="ap-legend-item" data-series="1" style="display:flex; align-items:center; gap:4px;cursor:pointer;opacity:1;"><span style="width:12px; height:2px; background:#ce93d8; border-radius:1px; border-top:1px dashed #ce93d8;"></span>紫币</span>'

        # 处理海里数时间线（与金币等 chart_points 对齐）
        if distance_raw_points and chart_points and current_view in ("line", "detail"):
            distance_raw_points.sort(key=lambda p: p["dt"])
            distance_idx = 0
            distance_last = len(distance_raw_points) - 1
            for p in chart_points:
                while distance_idx < distance_last:
                    cur_delta = abs(
                        (
                            distance_raw_points[distance_idx]["dt"] - p["dt"]
                        ).total_seconds()
                    )
                    next_delta = abs(
                        (
                            distance_raw_points[distance_idx + 1]["dt"] - p["dt"]
                        ).total_seconds()
                    )
                    if next_delta > cur_delta:
                        break
                    distance_idx += 1
                distance_point = distance_raw_points[distance_idx]
                distance_list.append(distance_point["distance"])

            if distance_list:
                valid_distance = [v for v in distance_list if v is not None]
                if valid_distance:
                    d_cur = valid_distance[-1]
                    d_change = (
                        valid_distance[-1] - valid_distance[0]
                        if len(valid_distance) >= 2
                        else 0
                    )
                    d_change_color = "#ef5350" if d_change >= 0 else "#26a69a"
                    d_change_sign = "+" if d_change >= 0 else ""
                    d_max = max(valid_distance)
                    d_min = min(valid_distance)

                    coins_stats_html += f'<div style="display:grid; grid-template-columns:150px 100px 90px 90px 90px; gap:8px; margin-bottom:2px; font-size:12px; color:#aaa;"><span>海里数: <b style="color:#1565c0">{d_cur}</b></span><span>变化: <b style="color:{d_change_color}">{d_change_sign}{d_change}</b></span><span>最高: <b style="color:#ef5350">{d_max}</b></span><span>最低: <b style="color:#26a69a">{d_min}</b></span><span></span></div>'
                    coins_legend_html += '<span class="ap-legend-item" data-series="4" style="display:flex; align-items:center; gap:4px;cursor:pointer;opacity:1;"><span style="width:12px; height:2px; background:#1565c0; border-radius:1px;"></span>海里数</span>'

        # 处理资产时间线
        if asset_timeline and current_view in ("line", "detail"):
            for pt in asset_timeline:
                ts_raw = pt.get("ts", "")
                if ts_raw:
                    try:
                        va_dt = datetime.fromisoformat(ts_raw)
                        asset_value = _snapshot_float(pt, "asset")
                        if asset_value is None:
                            continue
                        asset_list.append(asset_value)
                        asset_ts_list.append(int(va_dt.timestamp() * 1000))
                    except TypeError, ValueError:
                        continue

        if asset_list:
            valid_asset = [v for v in asset_list if v is not None]
            if valid_asset:
                a_cur = valid_asset[-1]
                a_change = (
                    valid_asset[-1] - valid_asset[0] if len(valid_asset) >= 2 else 0
                )
                a_change_color = "#ef5350" if a_change >= 0 else "#26a69a"
                a_change_sign = "+" if a_change >= 0 else ""
                a_max = max(valid_asset)
                a_min = min(valid_asset)

                coins_stats_html += f'<div style="display:grid; grid-template-columns:150px 100px 90px 90px 90px; gap:8px; margin-bottom:2px; font-size:12px; color:#aaa;"><span>资产: <b style="color:#22d3ee">{a_cur:.1f}</b></span><span>变化: <b style="color:{a_change_color}">{a_change_sign}{a_change:.1f}</b></span><span>最高: <b style="color:#ef5350">{a_max:.1f}</b></span><span>最低: <b style="color:#26a69a">{a_min:.1f}</b></span><span></span></div>'
                coins_legend_html += '<span class="ap-legend-item" data-series="3" style="display:flex; align-items:center; gap:4px;cursor:pointer;opacity:1;"><span style="width:12px; height:2px; background:#22d3ee; border-radius:1px;"></span>资产</span>'

        # 确保 show_coins 在资产存在时也为 True，以启用右轴绘制
        if not show_coins and (
            asset_list or yellow_coins_list or purple_coins_list or distance_list
        ):
            show_coins = True

        chart_id = f"ap_cv_{id(self)}"
        detail_controls_display = (
            "display:flex;" if current_view in ("line", "detail") else "display:none;"
        )

        html_tpl = read_webapp_template("ap_chart_panel.html")
        html = html_tpl.format(
            chart_id=chart_id,
            view_title=view_title,
            ap_cur=ap_cur,
            change_color=change_color,
            change_sign=change_sign,
            ap_change=ap_change,
            ap_max=ap_max,
            ap_min=ap_min,
            ap_avg=ap_avg,
            data_points_text=data_points_text,
            detail_controls_display=detail_controls_display,
            coins_stats_html=coins_stats_html,
            coins_legend_html=coins_legend_html,
        )

        js_tpl = read_webapp_template("ap_chart.js")
        js_code = (
            js_tpl.replace("__CHART_TYPE__", "line" if is_detail_mode else current_view)
            .replace("__LABELS__", json.dumps(labels, ensure_ascii=False))
            .replace("__OPENS__", json.dumps(opens))
            .replace("__HIGHS__", json.dumps(highs))
            .replace("__LOWS__", json.dumps(lows))
            .replace("__CLOSES__", json.dumps(closes))
            .replace("__COUNTS__", json.dumps(counts))
            .replace("__AP__", json.dumps(ap_list))
            .replace("__AP_TS__", json.dumps(ap_ts))
            .replace("__AVG__", str(ap_avg))
            .replace("__CHART_ID__", chart_id)
            .replace("__IS_DETAIL_MODE__", "true" if is_detail_mode else "false")
            .replace(
                "__SOURCES__", json.dumps(detail_sources if is_detail_mode else [])
            )
            .replace("__YELLOW_COINS__", json.dumps(yellow_coins_list))
            .replace("__PURPLE_COINS__", json.dumps(purple_coins_list))
            .replace("__COINS_SOURCES__", json.dumps(coins_sources_list))
            .replace("__ASSET__", json.dumps(asset_list))
            .replace("__ASSET_TS__", json.dumps(asset_ts_list))
            .replace("__DISTANCE__", json.dumps(distance_list))
            .replace("__SHOW_COINS__", "true" if show_coins else "false")
        )
        from pywebio.session import run_js

        with use_scope("ap_chart", clear=True):
            put_html(html)
            run_js(js_code)
