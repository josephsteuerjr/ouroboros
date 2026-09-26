"""Registry-backed Settings control in the real served UI (isolated backend)."""
import os

import pytest

pytest_plugins = ("tests.test_ui_smoke_playwright",)


@pytest.mark.serial
@pytest.mark.ui_browser
def test_windows_autostart_control_is_separate_from_settings_draft(direct_server_with_data):
    from playwright.sync_api import expect, sync_playwright

    state = {"enabled": False}
    changes = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})

            def owner_route(route):
                if route.request.method == "GET":
                    route.fulfill(json={"available": True, "enabled": state["enabled"]})
                else:
                    desired = route.request.post_data_json["enabled"]
                    changes.append(desired)
                    state["enabled"] = desired
                    route.fulfill(json={"available": True, "enabled": desired})

            page.route("**/api/owner/autostart", owner_route)
            page.goto(direct_server_with_data["url"], wait_until="domcontentloaded")
            page.click('[data-nav-page="settings"]')
            page.click('[data-settings-tab="advanced"]')
            checkbox = page.locator("#windows-autostart")
            expect(checkbox).to_be_visible(timeout=30000)
            expect(checkbox).not_to_be_checked()
            checkbox.click()
            expect(checkbox).to_be_disabled()
            page.evaluate("document.querySelector('#windows-autostart').dispatchEvent(new Event('change', {bubbles:true}))")
            assert changes == []
            page.get_by_role("button", name="Enable autostart").click()
            expect(checkbox).to_be_checked()
            assert changes == [True]
            expect(page.locator("#settings-unsaved-indicator")).not_to_have_class("is-visible")
            if os.environ.get("AUTOSTART_SCREENSHOT"):
                page.screenshot(path=os.environ["AUTOSTART_SCREENSHOT"], full_page=True)
            checkbox.click()
            expect(checkbox).to_be_disabled()
            state["enabled"] = False  # Windows Startup changed independently during confirmation.
            page.get_by_role("button", name="Cancel").click()
            expect(checkbox).not_to_be_checked()
            expect(checkbox).to_be_enabled()
            assert changes == [True]
        finally:
            browser.close()
