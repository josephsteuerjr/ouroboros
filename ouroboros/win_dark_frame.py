"""Dark DWM title bar for the pywebview 5.4 WinForms shell (#1417).

pywebview 5.4 (the pinned desktop shell) never sets
``DWMWA_USE_IMMERSIVE_DARK_MODE`` on its WinForms window, so on Windows the
system-drawn title bar stays light even when the OS apps theme is dark and
the web UI paints dark — a white frame around a dark app (measured on Win11
26300: the caption strip renders pure 255,255,255). pywebview upstream only
gained ``update_title_bar_theme`` in 6.2.1; until the pin moves, the launcher
applies the same DWM attribute itself.

The public seam is :func:`apply_dark_titlebar`: call it with the window
returned by ``webview.create_window`` before ``webview.start``. The attribute
is applied from the window's ``shown`` event so the native handle exists; the
handler resolves the HWND from ``window.native.Handle`` (set by the WinForms
backend at form construction) and calls ``DwmSetWindowAttribute`` directly.
DWM attribute calls are safe from a non-GUI thread (they change composition
metadata, not window state), and pywebview 5.4 dispatches ``shown`` handlers
on a worker thread — the same pattern its own fullscreen toggle uses from the
GUI side.

Darkness source: the window is painted dark when the OS *apps* theme is dark
(``AppsUseLightTheme == 0`` under HKCU\\...\\Themes\\Personalize), matching the
OS behaviour a native app gets. Windows whose body is unconditionally dark
(the launcher's already-running and startup-failure pages hard-code dark
HTML) pass ``force_dark=True`` so an OS-light machine still gets a dark frame
around a dark body. Everything is best-effort: a missing key, an older DWM
without the attribute, or a non-WinForms backend logs one warning and leaves
the frame as the OS drew it.
"""

from __future__ import annotations

import ctypes
import logging
import sys

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

# DWMWA_USE_IMMERSIVE_DARK_MODE. Attribute 20 on Windows 10 1809+ / Windows 11;
# 19 on the 1803–1809 builds that shipped the same flag under another id. A
# failed call (older DWM, non-composited session) is a warning, never a crash.
_DWMWA_IMMERSIVE_DARK_MODE_1803 = 19
_DWMWA_IMMERSIVE_DARK_MODE = 20

_PERSONALIZE_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
)
_APPS_USE_LIGHT_THEME = "AppsUseLightTheme"


def is_system_dark_apps_theme() -> bool | None:
    """Whether the Windows *apps* theme is dark; ``None`` when unknown.

    Mirrors the registry read pywebview 6.2.1 performs. Any failure (missing
    key, non-Windows, registry error) is ``None`` so callers can fall back to
    leaving the frame untouched rather than guessing.
    """
    if not _IS_WINDOWS:
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _PERSONALIZE_KEY, 0, winreg.KEY_READ
        ) as key:
            value, _ = winreg.QueryValueEx(key, _APPS_USE_LIGHT_THEME)
        return int(value) == 0
    except OSError:
        return None
    except Exception:  # malformed value types we cannot interpret
        logger.debug("Apps theme read failed", exc_info=True)
        return None


def _dwm_api():
    """The ``dwmapi`` windll entry; raises outside Windows (tests monkeypatch)."""
    return ctypes.windll.dwmapi


def _set_immersive_dark_mode(hwnd: int, dark: bool) -> bool:
    """Apply DWMWA_USE_IMMERSIVE_DARK_MODE to one window; True on success."""
    value = ctypes.c_int(1 if dark else 0)
    try:
        dwmapi = _dwm_api()
        for attribute in (
            _DWMWA_IMMERSIVE_DARK_MODE,
            _DWMWA_IMMERSIVE_DARK_MODE_1803,
        ):
            result = dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd),
                ctypes.c_uint(attribute),
                ctypes.byref(value),
                ctypes.c_size_t(ctypes.sizeof(value)),
            )
            if result == 0:
                return True
        logger.warning("DwmSetWindowAttribute dark mode refused for hwnd %s", hwnd)
        return False
    except Exception:
        # dwmapi unavailable or the call shape rejected: leave the frame alone.
        logger.warning("DWM dark-mode attribute not applied", exc_info=True)
        return False


def apply_dark_titlebar(window, force_dark: bool = False) -> None:
    """Paint the native title bar of ``window`` dark when its body is dark.

    No-op outside Windows and for backends without a ``native`` form object.
    Register before ``webview.start``: the attribute is applied once the
    window is shown. The theme is sampled when the window appears (not
    re-sampled live) — mid-session OS theme flips need a restart until the
    pywebview pin reaches 6.2.1, which re-renders natively.
    """
    if not _IS_WINDOWS or window is None:
        return

    def _on_shown(*_args) -> None:
        try:
            hwnd = int(window.native.Handle.ToInt32())
        except AttributeError:
            logger.debug("No native handle for dark title bar; backend differs?")
            return
        dark = bool(force_dark or is_system_dark_apps_theme())
        if dark:
            _set_immersive_dark_mode(hwnd, dark=True)

    try:
        window.events.shown += _on_shown
    except AttributeError:
        logger.debug("Window without events.shown; skipping dark title bar")
