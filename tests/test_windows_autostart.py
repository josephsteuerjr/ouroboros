"""Windows Run-key contract without modifying the host's actual registry."""
import sys
import types

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ouroboros import windows_autostart as autostart
from ouroboros.gateway.autostart import api_owner_autostart_get, api_owner_autostart_post


@pytest.fixture
def registry(monkeypatch, tmp_path):
    exe = tmp_path / "Ouroboros.exe"
    exe.write_bytes(b"launcher")
    monkeypatch.setattr(autostart.sys, "platform", "win32")
    monkeypatch.setenv("OUROBOROS_PRESENTATION", "desktop_window")
    monkeypatch.setenv("OUROBOROS_LAUNCHER_EXE", str(exe))
    values = {}

    class Key:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass

    def query(key, name):
        if name not in values:
            raise FileNotFoundError(name)
        return values[name]

    def delete(key, name):
        if name not in values:
            raise FileNotFoundError(name)
        del values[name]

    def open_key(root, subkey, reserved, access):
        if values and access & 2 and not access & 4:
            raise PermissionError("QueryValueEx needs KEY_QUERY_VALUE")
        return Key()

    fake = types.SimpleNamespace(
        HKEY_CURRENT_USER=1, KEY_READ=1, KEY_SET_VALUE=2, KEY_QUERY_VALUE=4, REG_SZ=1,
        OpenKey=open_key, CreateKeyEx=lambda *args: Key(),
        QueryValueEx=query, DeleteValue=delete,
        SetValueEx=lambda key, name, reserved, kind, value: values.__setitem__(name, (value, kind)),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)
    return exe, values


def test_run_key_roundtrip_and_foreign_edit_preserved(registry):
    exe, values = registry
    assert autostart.status() == {"available": True, "enabled": False}
    assert autostart.set_enabled(True)["enabled"] is True
    assert values["Ouroboros"] == (f'"{exe}"', 1)
    values["Ouroboros"] = (f'"{str(exe).swapcase()}"', 1)
    assert autostart.status()["enabled"] is True
    assert autostart.set_enabled(False)["enabled"] is False
    autostart.set_enabled(True)
    values["Ouroboros"] = (f'"{exe}" --other', 1)
    with pytest.raises(ValueError, match="differs"):
        autostart.set_enabled(False)
    values["Ouroboros"] = (f'"{exe}"', 1)
    assert autostart.set_enabled(False)["enabled"] is False
    assert autostart.set_enabled(False)["enabled"] is False
    autostart.set_enabled(True)
    values["Ouroboros"] = ('"other.exe"', 1)
    assert autostart.status()["enabled"] is False
    with pytest.raises(ValueError, match="differs"):
        autostart.set_enabled(False)
    with pytest.raises(ValueError, match="differs"):
        autostart.set_enabled(True)
    assert values["Ouroboros"] == ('"other.exe"', 1)


def test_unavailable_source_mode_never_writes(registry, monkeypatch):
    _, values = registry
    monkeypatch.setenv("OUROBOROS_PRESENTATION", "web")
    assert autostart.status() == {"available": False, "enabled": False}
    with pytest.raises(ValueError, match="unavailable"):
        autostart.set_enabled(True)
    assert values == {}


def test_http_validation_and_registry_truth(registry, monkeypatch):
    from ouroboros.gateway import autostart as gateway
    monkeypatch.setattr(gateway, "_owner_audit", lambda *args: None)
    client = TestClient(Starlette(routes=[
        Route("/api/owner/autostart", api_owner_autostart_get, methods=["GET"]),
        Route("/api/owner/autostart", api_owner_autostart_post, methods=["POST"]),
    ]))
    assert client.get("/api/owner/autostart").json()["enabled"] is False
    for body in ({"enabled": 1}, {"enabled": True, "extra": False}, {}):
        assert client.post("/api/owner/autostart", json=body).status_code == 400
    assert client.post("/api/owner/autostart", json={"enabled": True}).json()["enabled"] is True
    assert client.get("/api/owner/autostart").json()["enabled"] is True
