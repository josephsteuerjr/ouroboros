"""Dark DWM title bar helper (#1417): unit tests.

The DWM call, the registry read and the native window handle are all mocked
so the suite runs on every OS the repository CI covers; the Windows-only
behaviour is exercised through the same seams the launcher uses.
"""

from __future__ import annotations

import ctypes
from types import SimpleNamespace

import pytest

import ouroboros.win_dark_frame as wdf


class _FakeDwm:
    """Records DwmSetWindowAttribute calls; scriptable return codes."""

    def __init__(self, results=(0,)):
        self.calls: list[tuple[int, int, int, int]] = []
        self.results = list(results)

    def DwmSetWindowAttribute(self, hwnd, attr, value, size):
        unwrap = lambda x: getattr(x, "value", x)  # noqa: E731
        self.calls.append((unwrap(hwnd), unwrap(attr), ctypes.cast(value, ctypes.POINTER(ctypes.c_int)).contents.value, unwrap(size)))
        return self.results.pop(0) if self.results else 0


class _FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, item):
        self.handlers.append(item)
        return self


class _FakeWindow:
    """Mirrors the pywebview 5.4 window surface the helper touches."""

    def __init__(self, with_native=True):
        self.events = SimpleNamespace(shown=_FakeEvent())
        self.native = SimpleNamespace(Handle=SimpleNamespace(ToInt32=lambda: 4242)) if with_native else None


def _run_shown(window) -> None:
    for handler in window.events.shown.handlers:
        handler()


def test_force_dark_applies_attribute(monkeypatch):
    monkeypatch.setattr(wdf, "_IS_WINDOWS", True)
    dwm = _FakeDwm()
    monkeypatch.setattr(wdf, "_dwm_api", lambda: dwm)
    window = _FakeWindow()
    wdf.apply_dark_titlebar(window, force_dark=True)
    _run_shown(window)
    assert dwm.calls == [(4242, wdf._DWMWA_IMMERSIVE_DARK_MODE, 1, 4)]


def test_light_system_theme_leaves_frame_untouched(monkeypatch):
    monkeypatch.setattr(wdf, "_IS_WINDOWS", True)
    dwm = _FakeDwm()
    monkeypatch.setattr(wdf, "_dwm_api", lambda: dwm)
    monkeypatch.setattr(wdf, "is_system_dark_apps_theme", lambda: False)
    window = _FakeWindow()
    wdf.apply_dark_titlebar(window)
    _run_shown(window)
    assert dwm.calls == []


def test_dark_system_theme_applies_attribute(monkeypatch):
    monkeypatch.setattr(wdf, "_IS_WINDOWS", True)
    dwm = _FakeDwm()
    monkeypatch.setattr(wdf, "_dwm_api", lambda: dwm)
    monkeypatch.setattr(wdf, "is_system_dark_apps_theme", lambda: True)
    window = _FakeWindow()
    wdf.apply_dark_titlebar(window)
    _run_shown(window)
    assert dwm.calls == [(4242, wdf._DWMWA_IMMERSIVE_DARK_MODE, 1, 4)]


def test_unknown_theme_leaves_frame_untouched(monkeypatch):
    monkeypatch.setattr(wdf, "_IS_WINDOWS", True)
    dwm = _FakeDwm()
    monkeypatch.setattr(wdf, "_dwm_api", lambda: dwm)
    monkeypatch.setattr(wdf, "is_system_dark_apps_theme", lambda: None)
    window = _FakeWindow()
    wdf.apply_dark_titlebar(window)
    _run_shown(window)
    assert dwm.calls == []


def test_missing_native_handle_is_silent(monkeypatch):
    monkeypatch.setattr(wdf, "_IS_WINDOWS", True)
    dwm = _FakeDwm()
    monkeypatch.setattr(wdf, "_dwm_api", lambda: dwm)
    window = _FakeWindow(with_native=False)
    wdf.apply_dark_titlebar(window, force_dark=True)
    _run_shown(window)  # no native handle: warn-and-skip, never raise
    assert dwm.calls == []


def test_modern_attribute_falls_back_to_1803_id(monkeypatch):
    monkeypatch.setattr(wdf, "_IS_WINDOWS", True)
    # First (modern) attribute refused -> the 1803 id is retried.
    dwm = _FakeDwm(results=(0x80070057, 0))
    monkeypatch.setattr(wdf, "_dwm_api", lambda: dwm)
    window = _FakeWindow()
    wdf.apply_dark_titlebar(window, force_dark=True)
    _run_shown(window)
    assert dwm.calls == [
        (4242, wdf._DWMWA_IMMERSIVE_DARK_MODE, 1, 4),
        (4242, wdf._DWMWA_IMMERSIVE_DARK_MODE_1803, 1, 4),
    ]


def test_all_refused_is_warning_not_error(monkeypatch):
    monkeypatch.setattr(wdf, "_IS_WINDOWS", True)
    dwm = _FakeDwm(results=(1, 1))
    monkeypatch.setattr(wdf, "_dwm_api", lambda: dwm)
    window = _FakeWindow()
    wdf.apply_dark_titlebar(window, force_dark=True)
    _run_shown(window)
    assert len(dwm.calls) == 2


def test_dwm_api_raising_never_propagates(monkeypatch):
    monkeypatch.setattr(wdf, "_IS_WINDOWS", True)

    def boom():
        raise OSError("no dwmapi")

    monkeypatch.setattr(wdf, "_dwm_api", boom)
    window = _FakeWindow()
    wdf.apply_dark_titlebar(window, force_dark=True)
    _run_shown(window)


def test_none_window_is_a_noop():
    wdf.apply_dark_titlebar(None)  # must not raise


def test_window_without_events_is_a_noop():
    window = SimpleNamespace(native=None)  # no .events at all (foreign backend)
    wdf.apply_dark_titlebar(window, force_dark=True)  # must not raise


def test_non_windows_platform_is_a_noop(monkeypatch):
    monkeypatch.setattr(wdf, "_IS_WINDOWS", False)
    window = _FakeWindow()
    wdf.apply_dark_titlebar(window, force_dark=True)
    assert window.events.shown.handlers == []


def test_is_system_dark_apps_theme_parses_registry(monkeypatch):
    winreg = pytest.importorskip("winreg")

    class _Key:
        def __init__(self, value):
            self.value = value

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(wdf, "_IS_WINDOWS", True)
    monkeypatch.setattr(winreg, "OpenKey", lambda root, path, a, b: _Key(0))
    monkeypatch.setattr(winreg, "QueryValueEx", lambda key, name: (key.value, "REG_DWORD"))
    assert wdf.is_system_dark_apps_theme() is True

    monkeypatch.setattr(winreg, "OpenKey", lambda root, path, a, b: _Key(1))
    assert wdf.is_system_dark_apps_theme() is False

    def boom(*args, **kwargs):
        raise OSError("missing")

    monkeypatch.setattr(winreg, "OpenKey", boom)
    assert wdf.is_system_dark_apps_theme() is None
