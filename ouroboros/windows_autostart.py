"""Windows desktop logon autostart; HKCU Run is the only persisted state.

No service or settings.json shadow: Windows' own Startup controls may change this
value between reads. This module is imported on other platforms without winreg.
"""
from __future__ import annotations

import os
import ntpath
import pathlib
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Ouroboros"


def _is_our_command(value: object, kind: int, path: pathlib.Path) -> bool:
    """Windows path spelling ignores case; arguments or foreign commands do not."""
    import winreg
    return (kind == winreg.REG_SZ and isinstance(value, str) and len(value) >= 3
            and value[0] == value[-1] == '"'
            and ntpath.normcase(value[1:-1]) == ntpath.normcase(str(path)))


def launcher_path() -> pathlib.Path | None:
    """Only the packaged Windows launcher can nominate an autostart target."""
    if sys.platform != "win32" or os.environ.get("OUROBOROS_PRESENTATION") != "desktop_window":
        return None
    raw = os.environ.get("OUROBOROS_LAUNCHER_EXE", "")
    if not raw or not pathlib.Path(raw).is_absolute() or pathlib.Path(raw).suffix.lower() != ".exe":
        return None
    path = pathlib.Path(raw)
    return path if path.is_file() else None


def status() -> dict:
    path = launcher_path()
    if path is None:
        return {"available": False, "enabled": False}
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, kind = winreg.QueryValueEx(key, VALUE_NAME)
    except FileNotFoundError:
        return {"available": True, "enabled": False}
    # An unrelated owner edit to this value must not be presented as OUR launcher.
    return {"available": True, "enabled": _is_our_command(value, kind, path)}


def set_enabled(enabled: bool) -> dict:
    path = launcher_path()
    if path is None:
        raise ValueError("Windows packaged desktop launcher is unavailable")
    import winreg
    if enabled:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
            try:
                value, kind = winreg.QueryValueEx(key, VALUE_NAME)
            except FileNotFoundError:
                pass
            else:
                if not _is_our_command(value, kind, path):
                    raise ValueError("Startup entry differs from this launcher; refusing to replace it")
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, f'"{path}"')
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
                try:
                    value, kind = winreg.QueryValueEx(key, VALUE_NAME)
                except FileNotFoundError:
                    return status()
                if not _is_our_command(value, kind, path):
                    raise ValueError("Startup entry differs from this launcher; refusing to delete it")
                winreg.DeleteValue(key, VALUE_NAME)
        except FileNotFoundError:
            pass
    return status()
