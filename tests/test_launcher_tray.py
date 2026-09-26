"""Windows tray close policy without importing WinForms on other platforms."""

import sys
import threading
import types
from types import SimpleNamespace

import pytest

from ouroboros.launcher_tray import WindowsTray


def test_close_hides_only_with_live_tray_and_cancelable_event():
    hidden = []
    window = SimpleNamespace(hide=lambda: hidden.append(True))
    shutdown = threading.Event()
    tray = WindowsTray(lambda: window, lambda: None, shutdown)
    assert not tray.hide_on_close(window)
    tray.ready.set()
    assert tray.hide_on_close(window) and hidden == [True]
    tray.stop()
    assert not tray.hide_on_close(window)
    tray.ready.set()
    shutdown.set()
    assert not tray.hide_on_close(window)


def test_start_fails_open_without_winforms(monkeypatch):
    monkeypatch.setitem(sys.modules, "clr", None)
    tray = WindowsTray(lambda: None, lambda: None, threading.Event())
    assert tray.start() is False
    assert not tray.ready.is_set()


def test_sta_setup_failure_falls_back_to_ordinary_close(monkeypatch):
    clr = types.ModuleType("clr")
    clr.AddReference = lambda name: None
    system = types.ModuleType("System")
    system.__path__ = []
    drawing = types.ModuleType("System.Drawing")
    drawing.Icon = object
    drawing.SystemIcons = SimpleNamespace(Application=object())
    dotnet_threading = types.ModuleType("System.Threading")
    dotnet_threading.ApartmentState = SimpleNamespace(STA=object())
    dotnet_threading.ThreadStart = lambda fn: fn

    class BadThread:
        def __init__(self, fn):
            self.fn = fn

        def SetApartmentState(self, state):
            raise RuntimeError("STA unavailable")

    dotnet_threading.Thread = BadThread
    forms = types.ModuleType("System.Windows.Forms")
    for name in ("Application", "ApplicationContext", "ContextMenuStrip", "MouseButtons",
                 "NotifyIcon", "Timer", "ToolStripMenuItem"):
        setattr(forms, name, object())
    for name, value in (("clr", clr), ("System", system), ("System.Drawing", drawing),
                        ("System.Threading", dotnet_threading), ("System.Windows.Forms", forms)):
        monkeypatch.setitem(sys.modules, name, value)
    tray = WindowsTray(lambda: None, lambda: None, threading.Event())
    assert tray.start() is False
    assert not tray.ready.is_set()


def test_lost_pump_restores_a_window_hidden_by_x():
    seen = []
    window = SimpleNamespace(hide=lambda: seen.append("hide"), show=lambda: seen.append("show"))
    tray = WindowsTray(lambda: window, lambda: None, threading.Event())
    tray.ready.set()
    assert tray.hide_on_close(window)
    tray._pump_stopped()
    assert seen == ["hide", "show"]
    assert not tray.ready.is_set()
    tray._pump_stopped()
    assert seen == ["hide", "show"]


def test_intentional_stop_does_not_reopen_hidden_window():
    seen = []
    window = SimpleNamespace(hide=lambda: seen.append("hide"), show=lambda: seen.append("show"))
    tray = WindowsTray(lambda: window, lambda: None, threading.Event())
    tray.ready.set()
    assert tray.hide_on_close(window)
    tray.stop()
    tray._pump_stopped()
    assert seen == ["hide"]


def test_tray_exit_reuses_the_window_shutdown_owner(monkeypatch):
    import launcher
    calls = []
    monkeypatch.setattr(launcher, "_shutdown_event", threading.Event())
    monkeypatch.setattr(launcher, "stop_agent", lambda: calls.append("stop"))
    monkeypatch.setattr(launcher, "_kill_orphaned_children", lambda port, reason: calls.append((port, reason)))
    monkeypatch.setattr(launcher, "release_pid_lock", lambda: calls.append("release"))

    def exit_process(code):
        calls.append(("exit", code))
        raise SystemExit(code)

    monkeypatch.setattr(launcher.os, "_exit", exit_process)
    with pytest.raises(SystemExit):
        launcher._exit_desktop(8765)
    assert launcher._shutdown_event.is_set()
    assert calls == ["stop", (8765, "desktop_shutdown"), "release", ("exit", 0)]
