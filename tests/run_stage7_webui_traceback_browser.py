from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests/fixtures/stage7_webui_traceback"
VIEWPORTS = ((1280, 720), (1366, 768), (1920, 1080))


def metrics(page: Page) -> dict[str, object]:
    return page.locator(".rich-traceback-container").evaluate(
        """element => {
            const pre = element.querySelector('pre');
            const style = getComputedStyle(pre);
            const root = document.documentElement;
            return {
                container_count: document.querySelectorAll('.rich-traceback-container').length,
                script_count: document.querySelectorAll('script').length,
                injected_id_count: document.querySelectorAll('#stage7-owned').length,
                page_client_width: root.clientWidth,
                page_scroll_width: root.scrollWidth,
                container_client_width: element.clientWidth,
                container_scroll_width: element.scrollWidth,
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
    assert data["white_space"] == "pre", data
    assert data["ligatures"] == "none", data
    text = str(data["text"])
    assert "Exception: quq" in text, data
    assert "private-token" not in text, data
    assert "do-not-leak" not in text, data
    if narrow:
        assert data["container_scroll_width"] > data["container_client_width"], data


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
        finally:
            browser.close()
    return results


if __name__ == "__main__":
    print(json.dumps({"status": "PASS", "cases": run()}, ensure_ascii=False, indent=2))
