"""Both emitted desktop APIs share one bounded browser handoff, never a save."""

import pytest

from ouroboros.launcher_bridge import create_main_api
from tests.test_onboarding_host import _install_fake_webview


@pytest.mark.parametrize("settled", [True, False, RuntimeError("no browser"), None])
def test_setup_and_main_bridge_share_the_bounded_external_opener(monkeypatch, settled):
    import launcher
    from ouroboros import launcher_onboarding

    opened, joins = [], []

    class OpenThread:
        def join(self, timeout):
            joins.append(timeout)

    def open_browser(url, outcome):
        opened.append(url)
        if settled is not None:
            outcome.append(settled)
        return OpenThread()

    monkeypatch.setattr(launcher, "_open_browser_detached", open_browser)
    created, _ = _install_fake_webview(monkeypatch)
    result = launcher_onboarding.present_first_run_onboarding(
        {}, 8765, open_external_url=launcher._open_external_url,
    )
    api = create_main_api(
        actual_port=8765, get_window=lambda: None, load_settings=lambda: {},
        normalize_runtime_mode=lambda value: value,
        request_runtime_mode_change=lambda *a: {},
        request_auto_grant_reviewed_skills_change=lambda *a: {},
        request_skill_key_grant=lambda *a: {},
        open_external_url=launcher._open_external_url,
        request_native_attention=lambda *a, **kw: {},
        open_path_external=lambda path: None,
    )
    for bridge in (created["js_api"], api):
        for url in ("https://example.test/signin", "http://example.test/", "mailto:owner@example.test"):
            answer = bridge.open_external_url(url)
            assert answer["ok"] is (settled is True or settled is None)
            if not answer["ok"]:
                assert "default browser could not be opened" in answer["error"]
        before = len(opened)
        for url in ("javascript:alert(1)", "file:///tmp/signin", "/relative", ""):
            assert bridge.open_external_url(url)["ok"] is False
        assert len(opened) == before
    assert len(opened) == 6 and joins == [3.0] * 6
    assert result == {"saved": False, "restart_required": False}
    assert not hasattr(created["js_api"], "save_wizard")


def test_external_opener_returns_thread_start_failure(monkeypatch):
    import launcher

    def fail(*_):
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(launcher, "_open_browser_detached", fail)
    assert launcher._open_external_url("https://example.test/") == {
        "ok": False, "error": "thread unavailable",
    }


def test_main_bridge_request_attention_delegates_window_and_sound(monkeypatch):
    import launcher

    shown = []
    window = type("Window", (), {"show": lambda self: shown.append(True)})()
    api = create_main_api(
        actual_port=8765, get_window=lambda: window, load_settings=lambda: {},
        normalize_runtime_mode=lambda value: value,
        request_runtime_mode_change=lambda *a: {},
        request_auto_grant_reviewed_skills_change=lambda *a: {},
        request_skill_key_grant=lambda *a: {},
        open_external_url=launcher._open_external_url,
        request_native_attention=lambda show, sound=True: (show(), {"ok": True, "sound": sound})[1],
        open_path_external=lambda path: None,
    )
    result = api.request_attention(False)
    assert result == {"ok": True, "sound": False}
    assert shown == [True]
