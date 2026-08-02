from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests/fixtures/stage7_webui_traceback"
VIEWPORTS = ((1280, 720), (1366, 768), (1920, 1080))
REFLOW_VIEWPORT = (874, 486)


def metrics(page: Page) -> dict[str, object]:
    return page.locator(".rich-traceback-container").evaluate(
        """element => {
            const pre = element.querySelector('pre');
            const style = getComputedStyle(pre);
            const containerStyle = getComputedStyle(element);
            const modal = element.closest('.modal');
            const dialog = modal ? modal.querySelector('.modal-dialog') : null;
            const modalStyle = modal ? getComputedStyle(modal) : null;
            const root = document.documentElement;
            const modalRect = modal ? modal.getBoundingClientRect() : null;
            const dialogRect = dialog ? dialog.getBoundingClientRect() : null;
            return {
                container_count: document.querySelectorAll('.rich-traceback-container').length,
                script_count: document.querySelectorAll('script').length,
                injected_id_count: document.querySelectorAll('#stage7-owned').length,
                page_client_width: root.clientWidth,
                page_scroll_width: root.scrollWidth,
                container_client_width: element.clientWidth,
                container_scroll_width: element.scrollWidth,
                container_client_height: element.clientHeight,
                container_scroll_height: element.scrollHeight,
                container_scroll_top: element.scrollTop,
                container_overflow_y: containerStyle.overflowY,
                modal_display: modalStyle ? modalStyle.display : '',
                modal_overflow_y: modalStyle ? modalStyle.overflowY : '',
                modal_scroll_top: modal ? modal.scrollTop : 0,
                modal_client_height: modal ? modal.clientHeight : 0,
                modal_scroll_height: modal ? modal.scrollHeight : 0,
                modal_top: modalRect ? modalRect.top : 0,
                dialog_top: dialogRect ? dialogRect.top : 0,
                white_space: style.whiteSpace,
                ligatures: style.fontVariantLigatures,
                text: element.textContent,
            };
        }"""
    )


def assert_safe_layout(data: dict[str, object], *, narrow: bool = False) -> None:
    assert data["container_count"] == 1, data
    assert data["script_count"] == 0, data
    assert data["injected_id_count"] == 0, data
    assert data["page_scroll_width"] <= data["page_client_width"], data
    assert data["container_overflow_y"] == "visible", data
    assert data["modal_display"] == "block", data
    assert data["modal_overflow_y"] == "auto", data
    assert data["container_scroll_top"] == 0, data
    assert data["dialog_top"] >= data["modal_top"], data
    assert data["white_space"] == "pre", data
    assert data["ligatures"] == "none", data
    text = str(data["text"])
    assert "Exception: quq" in text, data
    assert "private-token" not in text, data
    assert "do-not-leak" not in text, data
    if narrow:
        assert data["container_scroll_width"] > data["container_client_width"], data


def wheel_scroll_round_trip(page: Page) -> dict[str, object]:
    modal = page.locator(".modal")
    traceback = page.locator(".rich-traceback-container")
    modal.evaluate("element => { element.scrollTop = 0; }")
    traceback.hover()

    for _ in range(12):
        page.mouse.wheel(0, 1600)
    page.wait_for_timeout(100)
    bottom = metrics(page)

    for _ in range(12):
        page.mouse.wheel(0, -1600)
    page.wait_for_timeout(100)
    top = metrics(page)

    assert bottom["modal_scroll_top"] > 0, bottom
    assert top["modal_scroll_top"] == 0, top
    assert top["container_scroll_top"] == 0, top
    assert top["dialog_top"] >= top["modal_top"], top
    assert top["page_scroll_width"] <= top["page_client_width"], top
    return {"bottom": bottom, "top": top}


def run() -> list[dict[str, object]]:
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
                        page.wait_for_function(
                            "!document.fonts || document.fonts.status === 'loaded'"
                        )
                        initial = metrics(page)
                        assert_safe_layout(initial)

                        modal_html = page.locator(".modal").evaluate(
                            "element => element.outerHTML"
                        )
                        page.locator(".modal-dialog").evaluate(
                            "element => element.style.width = '520px'"
                        )
                        narrow = metrics(page)
                        assert_safe_layout(narrow, narrow=True)

                        page.locator(".modal").evaluate("element => element.remove()")
                        page.locator("body").evaluate(
                            "(body, html) => body.insertAdjacentHTML('beforeend', html)",
                            modal_html,
                        )
                        reopened = metrics(page)
                        assert_safe_layout(reopened)
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
                    page.wait_for_function(
                        "!document.fonts || document.fonts.status === 'loaded'"
                    )
                    reflow_initial = metrics(page)
                    assert_safe_layout(reflow_initial)
                    assert reflow_initial["modal_scroll_height"] > reflow_initial["modal_client_height"], reflow_initial
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
