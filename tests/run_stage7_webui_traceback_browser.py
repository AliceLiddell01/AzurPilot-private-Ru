from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests/fixtures/stage7_webui_traceback"
TRACEBACK_CSS = ROOT / "assets/gui/css/traceback-alas.css"
WEBUI_APP = ROOT / "module/webui/app.py"
VIEWPORTS = ((1280, 720), (1366, 768), (1920, 1080))
REFLOW_VIEWPORT = (874, 486)
MIN_TEXT_CONTRAST = 4.5


def apply_runtime_styles(page: Page) -> None:
    page.add_style_tag(path=str(TRACEBACK_CSS))


def metrics(page: Page) -> dict[str, object]:
    return page.locator(".rich-traceback-container").evaluate(
        """element => {
            const pre = element.querySelector('pre');
            const preStyle = getComputedStyle(pre);
            const containerStyle = getComputedStyle(element);
            const modal = element.closest('.modal');
            const dialog = modal ? modal.querySelector('.modal-dialog') : null;
            const content = modal ? modal.querySelector('.modal-content') : null;
            const modalStyle = modal ? getComputedStyle(modal) : null;
            const contentStyle = content ? getComputedStyle(content) : null;
            const root = document.scrollingElement || document.documentElement;
            const modalRect = modal ? modal.getBoundingClientRect() : null;
            const dialogRect = dialog ? dialog.getBoundingClientRect() : null;

            const parseRgb = value => {
                const match = value.match(
                    /rgba?\\(\\s*([0-9.]+)\\s*,\\s*([0-9.]+)\\s*,\\s*([0-9.]+)/
                );
                if (!match) return null;
                return [Number(match[1]), Number(match[2]), Number(match[3])];
            };
            const luminance = rgb => {
                const channels = rgb.map(channel => {
                    const normalized = channel / 255;
                    return normalized <= 0.04045
                        ? normalized / 12.92
                        : Math.pow((normalized + 0.055) / 1.055, 2.4);
                });
                return (
                    0.2126 * channels[0]
                    + 0.7152 * channels[1]
                    + 0.0722 * channels[2]
                );
            };
            const contrastRatio = (foreground, background) => {
                const foregroundLuminance = luminance(foreground);
                const backgroundLuminance = luminance(background);
                const lighter = Math.max(foregroundLuminance, backgroundLuminance);
                const darker = Math.min(foregroundLuminance, backgroundLuminance);
                return (lighter + 0.05) / (darker + 0.05);
            };

            const backgroundColor = getComputedStyle(document.body).backgroundColor;
            const backgroundRgb = parseRgb(backgroundColor);
            const contrastByColor = new Map();
            const textNodes = [pre, ...pre.querySelectorAll('span')];
            for (const node of textNodes) {
                const text = (node.textContent || '').trim();
                if (!text || !backgroundRgb) continue;
                const color = getComputedStyle(node).color;
                const foregroundRgb = parseRgb(color);
                if (!foregroundRgb) continue;
                const ratio = contrastRatio(foregroundRgb, backgroundRgb);
                const previous = contrastByColor.get(color);
                if (!previous || ratio < previous.ratio) {
                    contrastByColor.set(color, {
                        color,
                        ratio: Number(ratio.toFixed(3)),
                        sample: text.slice(0, 80),
                    });
                }
            }
            const contrastSamples = [...contrastByColor.values()].sort(
                (left, right) => left.ratio - right.ratio
            );
            const lowContrastSamples = contrastSamples.filter(
                sample => sample.ratio < 4.5
            );

            return {
                container_count: document.querySelectorAll('.rich-traceback-container').length,
                script_count: document.querySelectorAll('script').length,
                injected_id_count: document.querySelectorAll('#stage7-owned').length,
                page_client_width: root.clientWidth,
                page_scroll_width: root.scrollWidth,
                page_client_height: root.clientHeight,
                page_scroll_height: root.scrollHeight,
                page_scroll_top: root.scrollTop,
                container_client_width: element.clientWidth,
                container_scroll_width: element.scrollWidth,
                container_client_height: element.clientHeight,
                container_scroll_height: element.scrollHeight,
                container_scroll_top: element.scrollTop,
                container_overflow_x: containerStyle.overflowX,
                container_overflow_y: containerStyle.overflowY,
                modal_position: modalStyle ? modalStyle.position : '',
                modal_display: modalStyle ? modalStyle.display : '',
                modal_overflow_y: modalStyle ? modalStyle.overflowY : '',
                modal_scroll_top: modal ? modal.scrollTop : 0,
                modal_client_height: modal ? modal.clientHeight : 0,
                modal_scroll_height: modal ? modal.scrollHeight : 0,
                modal_top: modalRect ? modalRect.top : 0,
                dialog_top: dialogRect ? dialogRect.top : 0,
                content_background: contentStyle ? contentStyle.backgroundColor : '',
                content_border_radius: contentStyle ? contentStyle.borderRadius : '',
                content_box_shadow: contentStyle ? contentStyle.boxShadow : '',
                white_space: preStyle.whiteSpace,
                ligatures: preStyle.fontVariantLigatures,
                background_color: backgroundColor,
                min_text_contrast: contrastSamples.length
                    ? contrastSamples[0].ratio
                    : null,
                low_contrast_samples: lowContrastSamples,
                text: element.textContent,
            };
        }"""
    )


def assert_safe_layout(
    data: dict[str, object],
    *,
    theme: str,
    narrow: bool = False,
) -> None:
    assert data["container_count"] == 1, data
    assert data["script_count"] == 0, data
    assert data["injected_id_count"] == 0, data
    assert data["page_scroll_width"] <= data["page_client_width"], data
    assert data["container_scroll_height"] <= data["container_client_height"] + 1, data
    assert data["modal_scroll_height"] <= data["modal_client_height"] + 1, data
    assert data["container_overflow_x"] == "auto", data
    assert data["container_overflow_y"] in {"clip", "hidden"}, data
    assert data["modal_position"] == "absolute", data
    assert data["modal_display"] == "flex", data
    assert data["modal_overflow_y"] == "visible", data
    assert data["container_scroll_top"] == 0, data
    assert data["modal_scroll_top"] == 0, data
    assert data["dialog_top"] >= data["modal_top"], data
    assert data["content_background"] == "rgba(0, 0, 0, 0)", data
    assert data["content_border_radius"] == "0px", data
    assert data["content_box_shadow"] == "none", data
    assert data["white_space"] == "pre", data
    assert data["ligatures"] == "none", data
    text = str(data["text"])
    assert "Exception: quq" in text, data
    assert "private-token" not in text, data
    assert "do-not-leak" not in text, data
    if theme == "light":
        assert data["min_text_contrast"] is not None, data
        assert float(data["min_text_contrast"]) >= MIN_TEXT_CONTRAST, data
        assert data["low_contrast_samples"] == [], data
    if narrow:
        assert data["container_scroll_width"] > data["container_client_width"], data


def wheel_scroll_round_trip(page: Page) -> dict[str, object]:
    traceback = page.locator(".rich-traceback-container")
    page.evaluate(
        """() => {
            const root = document.scrollingElement || document.documentElement;
            const modal = document.querySelector('.modal');
            const traceback = document.querySelector('.rich-traceback-container');
            root.scrollTop = 0;
            if (modal) modal.scrollTop = 0;
            if (traceback) traceback.scrollTop = 0;
        }"""
    )
    traceback.hover()

    for _ in range(12):
        page.mouse.wheel(0, 1600)
    page.wait_for_timeout(100)
    bottom = metrics(page)

    for _ in range(12):
        page.mouse.wheel(0, -1600)
    page.wait_for_timeout(100)
    top = metrics(page)

    assert bottom["page_scroll_top"] > 0, bottom
    assert bottom["modal_scroll_top"] == 0, bottom
    assert bottom["container_scroll_top"] == 0, bottom
    assert top["page_scroll_top"] == 0, top
    assert top["modal_scroll_top"] == 0, top
    assert top["container_scroll_top"] == 0, top
    assert top["dialog_top"] >= top["modal_top"], top
    assert top["page_scroll_width"] <= top["page_client_width"], top
    return {"bottom": bottom, "top": top}


def verify_runtime_wiring() -> None:
    if not TRACEBACK_CSS.is_file():
        raise AssertionError(f"Отсутствует stylesheet: {TRACEBACK_CSS}")
    app_source = WEBUI_APP.read_text(encoding="utf-8")
    expected = 'add_css(filepath_css("traceback-alas"))'
    if expected not in app_source:
        raise AssertionError("WebUI не загружает traceback-alas.css после theme styles")


def run() -> list[dict[str, object]]:
    verify_runtime_wiring()
    results: list[dict[str, object]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for theme in ("light", "dark"):
                fixture = FIXTURE_DIR / f"fixture-{theme}.html"
                if not fixture.is_file():
                    raise AssertionError(f"Отсутствует fixture: {fixture}")

                for width, height in VIEWPORTS:
                    page = browser.new_page(
                        viewport={"width": width, "height": height},
                        device_scale_factor=1,
                        locale="ru-RU",
                    )
                    try:
                        page.goto(fixture.as_uri(), wait_until="load")
                        apply_runtime_styles(page)
                        page.wait_for_function(
                            "!document.fonts || document.fonts.status === 'loaded'"
                        )
                        initial = metrics(page)
                        assert_safe_layout(initial, theme=theme)

                        modal_html = page.locator(".modal").evaluate(
                            "element => element.outerHTML"
                        )
                        page.locator(".modal-dialog").evaluate(
                            "element => element.style.width = '520px'"
                        )
                        narrow = metrics(page)
                        assert_safe_layout(narrow, theme=theme, narrow=True)

                        page.locator(".modal").evaluate("element => element.remove()")
                        page.locator("body").evaluate(
                            "(body, html) => body.insertAdjacentHTML('beforeend', html)",
                            modal_html,
                        )
                        reopened = metrics(page)
                        assert_safe_layout(reopened, theme=theme)
                        results.append(
                            {
                                "theme": theme,
                                "viewport": [width, height],
                                "initial": initial,
                                "narrow": narrow,
                                "reopened": reopened,
                            }
                        )
                    finally:
                        page.close()

                width, height = REFLOW_VIEWPORT
                page = browser.new_page(
                    viewport={"width": width, "height": height},
                    device_scale_factor=1,
                    locale="ru-RU",
                )
                try:
                    page.goto(fixture.as_uri(), wait_until="load")
                    apply_runtime_styles(page)
                    page.wait_for_function(
                        "!document.fonts || document.fonts.status === 'loaded'"
                    )
                    reflow_initial = metrics(page)
                    assert_safe_layout(reflow_initial, theme=theme)
                    assert (
                        reflow_initial["page_scroll_height"]
                        > reflow_initial["page_client_height"]
                    ), reflow_initial
                    round_trip = wheel_scroll_round_trip(page)
                    results.append(
                        {
                            "theme": theme,
                            "viewport": [width, height],
                            "browser_zoom_equivalent": "200% of 1748x972",
                            "initial": reflow_initial,
                            "wheel_round_trip": round_trip,
                        }
                    )
                finally:
                    page.close()
        finally:
            browser.close()
    return results


if __name__ == "__main__":
    print(json.dumps({"status": "PASS", "cases": run()}, ensure_ascii=False, indent=2))
