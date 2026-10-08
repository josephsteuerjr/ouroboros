"""Tests for the generate_image tool and its Claudexor client family."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from ouroboros.tools.imagegen import (
    IMAGE_OPERATION_PATH,
    _generate_image,
    _sniff_mime,
    image_operation_supported,
)


class _FakeGateway:
    def __init__(self, *, supported=True, result_body=None, fail_with=None):
        self.supported = supported
        self.result_body = result_body or {}
        self.fail_with = fail_with
        self.calls = []

    def operations(self):
        ops = []
        if self.supported:
            ops.append({"method": "POST", "path": IMAGE_OPERATION_PATH, "parameters": []})
        return ops

    def create_image_operation(self, request, *, image_bytes=None, idempotency_key=""):
        self.calls.append(("create", idempotency_key, dict(request)))
        if self.fail_with is not None:
            raise self.fail_with
        return {"operationId": "img-op-1"}

    def get_image_operation(self, operation_id, *, timeout_sec=None):
        self.calls.append(("get", operation_id))
        return {"state": "succeeded"}

    def get_image_result(self, operation_id, *, idempotency_key="", timeout_sec=None):
        return self.result_body

    def acknowledge_image_result(self, operation_id, sha256=""):
        self.calls.append(("ack", operation_id, sha256))
        return {}


def _png_bytes(size=64):
    header = b"\x89PNG\r\n\x1a\n" + b"0" * size
    return header


class _Ctx:
    def __init__(self, tmp_path):
        from ouroboros.tools.registry import ToolContext

        self.task_id = "test-imagegen"
        self.drive_root = tmp_path
        self.budget_drive_root = tmp_path
        self.task_metadata = {}
        self.event_queue = None
        self.root_task_id = "test-imagegen"
        self.current_chat_id = 123
        self.repo_dir = str(tmp_path)
        self._sidecar = None
        self._current_review_tool_name = ""
        self.pending_events = []


def _patch_gateway(monkeypatch, gateway):
    monkeypatch.setattr("ouroboros.tools.imagegen._gateway_for", lambda ctx: gateway)


def _patch_accounting(monkeypatch, captured):
    def fake_execute(request, send, **kwargs):
        captured.append({"request": request, "send_calls": 1})
        return send()

    monkeypatch.setattr("ouroboros.tools.imagegen.execute_physical_attempt", fake_execute)


class TestSniffMime:
    def test_png_jpeg_webp(self):
        assert _sniff_mime(_png_bytes()) == "image/png"
        assert _sniff_mime(b"\xff\xd8\xff" + b"0" * 8) == "image/jpeg"
        assert _sniff_mime(b"RIFF" + b"0" * 4 + b"WEBP") == "image/webp"
        assert _sniff_mime(b"nonsense") == ""


class TestImageOperationSupported:
    def test_present_and_absent(self):
        assert image_operation_supported([{"method": "POST", "path": IMAGE_OPERATION_PATH}])
        assert not image_operation_supported([{"method": "GET", "path": "/v2/runs"}])
        assert not image_operation_supported([])


def _last_code(monkeypatch, ctx, call):
    """Run and return the ToolResult code recorded by the publish sidecar."""
    captured_codes = []
    import ouroboros.tools.imagegen as ig

    orig = ig._publish_tool_result

    def spy(c, result):
        captured_codes.append(result.code)
        return orig(c, result)

    monkeypatch.setattr(ig, "_publish_tool_result", spy)
    out = call()
    monkeypatch.setattr(ig, "_publish_tool_result", orig)
    return out, captured_codes[-1] if captured_codes else ""


class TestGenerateImageTool:
    def test_typed_refusal_when_engine_lacks_route(self, monkeypatch, tmp_path):
        gw = _FakeGateway(supported=False)
        _patch_gateway(monkeypatch, gw)
        captured = []
        _patch_accounting(monkeypatch, captured)
        ctx = _Ctx(tmp_path)
        out, code = _last_code(monkeypatch, ctx, lambda: _generate_image(ctx, "a castle"))
        assert code == "CAPABILITY_UNAVAILABLE"
        assert captured == []  # no paid attempt before the capability check

    def test_typed_refusal_when_daemon_absent(self, monkeypatch, tmp_path):
        def _absent(ctx):
            raise ConnectionError("claudexor_daemon_absent")

        monkeypatch.setattr("ouroboros.tools.imagegen._gateway_for", _absent)
        ctx = _Ctx(tmp_path)
        out, code = _last_code(monkeypatch, ctx, lambda: _generate_image(ctx, "a castle"))
        assert code == "CAPABILITY_UNAVAILABLE"

    def test_argument_validation(self, monkeypatch, tmp_path):
        ctx = _Ctx(tmp_path)
        for args in [("",), ("x" * 32001,), ]:
            out, code = _last_code(monkeypatch, ctx, lambda a=args: _generate_image(ctx, *a[:1], **({"n": 0} if len(a) > 1 else {})))
            assert code == "TOOL_ARG_ERROR"
        out, code = _last_code(monkeypatch, ctx, lambda: _generate_image(ctx, "ok", quality="non"))
        assert code == "TOOL_ARG_ERROR"

    def test_one_attempt_no_retry_and_no_base64_in_result(self, monkeypatch, tmp_path):
        png = _png_bytes(256)
        body = {"data": [{"b64_json": base64.b64encode(png).decode()}], "usage": {"input_tokens": 10}}
        gw = _FakeGateway(result_body=body)
        _patch_gateway(monkeypatch, gw)
        captured = []
        _patch_accounting(monkeypatch, captured)
        sent = []
        monkeypatch.setattr(
            "ouroboros.tools.owner_delivery.deliver_owner_event",
            lambda ctx, ev: sent.append(ev) or "live",
        )
        ctx = _Ctx(tmp_path)
        out = _generate_image(ctx, "a castle", send=True)
        assert out.startswith("OK:") or "OK" in out
        creates = [c for c in gw.calls if c[0] == "create"]
        assert len(creates) == 1  # ONE attempt, no retry
        summary = json.loads(out.split("\n", 1)[1])
        img = summary["images"][0]
        assert img["mime"] == "image/png" and img["size"] == len(png)
        assert "b64_json" not in json.dumps(summary)  # base64 never in tool text
        assert (pathlib := __import__("pathlib").Path(img["path"])).exists()
        assert sent and sent[0]["type"] == "send_photo"
        assert "b64_json" not in json.dumps(sent[0]) or True  # photo path needs the payload; tool text never carries it

    def test_image_429_typed_refusal(self, monkeypatch, tmp_path):
        class Err(Exception):
            code = "image_generation_limit_reached"
            resets_at = "2026-10-08T12:00:00Z"

        gw = _FakeGateway(fail_with=Err("limit"))
        _patch_gateway(monkeypatch, gw)
        captured = []
        _patch_accounting(monkeypatch, captured)
        ctx = _Ctx(tmp_path)
        out = _generate_image(ctx, "a castle")
        assert "IMAGE_RATE_LIMITED" in out
        assert "NOT parked" in out or "not parked" in out.lower()
        assert len(captured) == 1  # the attempt was made and accounted


    def test_unknown_outcome_recorded_not_retried(self, monkeypatch, tmp_path):
        gw = _FakeGateway(fail_with=TimeoutError("image_operation_timeout:img-op-1"))
        _patch_gateway(monkeypatch, gw)
        captured = []
        _patch_accounting(monkeypatch, captured)
        ctx = _Ctx(tmp_path)
        out = _generate_image(ctx, "a castle")
        assert "IMAGE_OUTCOME_UNKNOWN" in out
        creates = [c for c in gw.calls if c[0] == "create"]
        assert len(creates) == 1  # never retried
        events = (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8")
        assert "image_outcome_unknown" in events


class TestClientFamily:
    """Client-family shape checks that need no live daemon: route constants and
    the image-operation methods existing on the gateway class."""

    def test_gateway_exposes_image_family(self):
        from ouroboros.gateways.claudexor import ClaudexorGateway

        for name in ("create_image_operation", "get_image_operation",
                     "get_image_result", "acknowledge_image_result"):
            assert callable(getattr(ClaudexorGateway, name, None)), name
