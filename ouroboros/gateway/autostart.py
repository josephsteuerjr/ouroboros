"""Owner HTTP projection and mutation of the Windows Run value."""
from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros import windows_autostart
from ouroboros.gateway._helpers import run_sync_to_completion
from ouroboros.gateway.owner_settings import _owner_audit

log = logging.getLogger(__name__)


async def api_owner_autostart_get(request: Request) -> JSONResponse:
    try:
        return JSONResponse(windows_autostart.status())
    except OSError as exc:
        log.warning("Windows autostart status unavailable: %s", exc)
        return JSONResponse({"error": "Windows startup registry could not be read"}, status_code=500)


async def api_owner_autostart_post(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "Expected JSON object with enabled boolean"}, status_code=400)
    if not isinstance(body, dict) or set(body) != {"enabled"} or type(body["enabled"]) is not bool:
        return JSONResponse({"error": "Expected exactly enabled boolean"}, status_code=400)
    if windows_autostart.launcher_path() is None:
        return JSONResponse({"error": "Windows packaged desktop launcher is unavailable"}, status_code=409)
    try:
        result = await run_sync_to_completion(windows_autostart.set_enabled, body["enabled"])
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except OSError as exc:
        log.warning("Windows autostart write failed: %s", exc)
        return JSONResponse({"error": "Windows startup registry write failed"}, status_code=500)
    _owner_audit(request, "windows_autostart", {"enabled": result["enabled"]})
    return JSONResponse(result)
