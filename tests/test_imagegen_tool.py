"""Tests for the generate_image tool and its Claudexor client family."""

from __future__ import annotations

import base64
import json

import pytest

import ouroboros.tools.imagegen as ig
from ouroboros.tools.imagegen import _generate_image, _sniff_mime
from ouroboros.gateways import claudexor_images
from ouroboros.gateways.claudexor_images import image_operation_supported


def _png_bytes(size=64):
    return b"\x89PNG\r\n\x1a\n" + b"0" * size


class _FakeGateway:
    def __init__(self, *, supported=True, result_body=None, fail_with=None):
        self.supported = supported
        self.result_body = result_body or {}
        self.fail_with = fail_with
        self.calls = []

    def operations(self):
        ops = []
        if self.supported:
            ops.append({"method": "POST", "path": claudexor_images.IMAGE_OPERATION_PATH, "parameters": []})
        return ops

    def _request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs.get("json_body"), kwargs.get("headers")))
        if self.fail_with is not None and method == "POST":
            raise self.fail_with
        if path.endswith("/result"):
            return self.result_body
        if method == "GET" and path.count("/") == 3:
            return {"state": "succeeded"}
        return {"operationId": "img-op-1"}

    # Compatibility with the module-level functions' gateway contract:
    # they call gateway._request directly, so _request above IS the seam.


class _Ctx:
    def __init__(self, tmp_path):
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
    monkeypatch.setattr(ig, "_gateway_for", lambda ctx: gateway)


def _patch_accounting(monkeypatch, captured):
    def fake_execute(request, send, **kwargs):
        captured.append({"request": request})
        return send()

    monkeypatch.setattr(ig, "execute_physical_attempt", fake_execute)


def _last_code(monkeypatch, ctx, call):
    captured_codes = []
    orig = ig._publish_tool_result

    def spy(c, result):
        captured_codes.append(result.code)
        return orig(c, result)

    monkeypatch.setattr(ig, "_publish_tool_result", spy)
    out = call()
    monkeypatch.setattr(ig, "_publish_tool_result", orig)
    return out, captured_codes[-1] if captured_codes else ""


class TestSniffMime:
    def test_png_jpeg_webp(self):
        assert _sniff_mime(_png_bytes()) == "image/png"
        assert _sniff_mime(b"\xff\xd8\xff" + b"0" * 8) == "image/jpeg"
        assert _sniff_mime(b"RIFF" + b"0" * 4 + b"WEBP") == "image/webp"
        assert _sniff_mime(b"nonsense") == ""


class TestImageOperationSupported:
    def test_present_and_absent(self):
        assert image_operation_supported([{"method": "POST", "path": claudexor_images.IMAGE_OPERATION_PATH}])
        assert not image_operation_supported([{"method": "GET", "path": "/v2/runs"}])
        assert not image_operation_supported([])


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
            raise ig._DaemonAbsent("daemon_not_discovered")

        monkeypatch.setattr(ig, "_gateway_for", _absent)
        ctx = _Ctx(tmp_path)
        out, code = _last_code(monkeypatch, ctx, lambda: _generate_image(ctx, "a castle"))
        assert code == "CAPABILITY_UNAVAILABLE"

    def test_argument_validation(self, monkeypatch, tmp_path):
        ctx = _Ctx(tmp_path)
        out, code = _last_code(monkeypatch, ctx, lambda: _generate_image(ctx, ""))
        assert code == "TOOL_ARG_ERROR"
        out, code = _last_code(monkeypatch, ctx, lambda: _generate_image(ctx, "x" * 32001))
        assert code == "TOOL_ARG_ERROR"
        out, code = _last_code(monkeypatch, ctx, lambda: _generate_image(ctx, "ok", n=0))
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
            ig, "deliver_owner_event", lambda ctx, ev: sent.append(ev) or "live"
        )
        ctx = _Ctx(tmp_path)
        out = _generate_image(ctx, "a castle", send=True)
        assert out.startswith("OK:") or "OK" in out
        creates = [c for c in gw.calls if c[0] == "POST" and c[1] == claudexor_images.IMAGE_OPERATION_PATH]
        assert len(creates) == 1  # ONE attempt, no retry
        summary = json.loads(out.split("\n", 1)[1])
        img = summary["images"][0]
        assert img["mime"] == "image/png" and img["size"] == len(png)
        assert set(img.keys()) == {"path", "sha256", "mime", "size"}
        assert "b64_json" not in json.dumps(summary)  # base64 never in tool text
        import pathlib
        assert pathlib.Path(img["path"]).exists()
        assert sent and sent[0]["type"] == "send_photo"
        # The delivery event legitimately carries the photo payload (transport
        # seam, not model context); the invariant is about the TOOL RESULT text.

    def test_only_first_image_sent(self, monkeypatch, tmp_path):
        png = _png_bytes(64)
        b64 = base64.b64encode(png).decode()
        body = {"data": [{"b64_json": b64}, {"b64_json": b64}], "usage": {}}
        gw = _FakeGateway(result_body=body)
        _patch_gateway(monkeypatch, gw)
        captured = []
        _patch_accounting(monkeypatch, captured)
        sent = []
        monkeypatch.setattr(
            ig, "deliver_owner_event", lambda ctx, ev: sent.append(ev) or "live"
        )
        ctx = _Ctx(tmp_path)
        out = _generate_image(ctx, "two castles", n=2, send=True)
        assert json.loads(out.split("\n", 1)[1])["generated"] == 2
        assert len(sent) == 1  # ONLY the first image is sent

    def test_image_429_typed_refusal(self, monkeypatch, tmp_path):
        class Err(Exception):
            code = "image_generation_limit_reached"
            reset_at = "2026-10-08T12:00:00Z"

        gw = _FakeGateway(fail_with=Err("limit"))
        _patch_gateway(monkeypatch, gw)
        captured = []
        _patch_accounting(monkeypatch, captured)
        ctx = _Ctx(tmp_path)
        out, code = _last_code(monkeypatch, ctx, lambda: _generate_image(ctx, "a castle"))
        assert code == "IMAGE_RATE_LIMITED"
        assert "NOT parked" in out
        assert "2026-10-08T12:00:00Z" in out
        assert len(captured) == 1  # the attempt was made and accounted

    def test_unknown_outcome_recorded_not_retried(self, monkeypatch, tmp_path):
        gw = _FakeGateway(fail_with=TimeoutError("image_operation_timeout:img-op-1"))
        _patch_gateway(monkeypatch, gw)
        captured = []
        _patch_accounting(monkeypatch, captured)
        ctx = _Ctx(tmp_path)
        out, code = _last_code(monkeypatch, ctx, lambda: _generate_image(ctx, "a castle"))
        assert code == "IMAGE_OUTCOME_UNKNOWN"
        creates = [c for c in gw.calls if c[0] == "POST"]
        assert len(creates) == 1  # never retried
        events = (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8")
        assert "image_outcome_unknown" in events


class TestClientFamily:
    """Client-family shape checks against the real module seam."""

    def test_create_image_operation_body_shape(self, monkeypatch):
        """B1 regression: the request body is the image request itself, NOT a
        model payload-ref; edit inputs carry their sniffed MIME (M3)."""
        seen = []

        class GW:
            def _request(self, method, path, **kwargs):
                seen.append((method, path, kwargs))
                return {"operationId": "op1"}

        out = claudexor_images.create_image_operation(
            GW(), {"model": "gpt-image-2", "prompt": "hi"},
            images=[(_png_bytes(16), "image/png")],
            idempotency_key="k-1",
        )
        method, path, kwargs = seen[0]
        assert method == "POST" and path == claudexor_images.IMAGE_OPERATION_PATH
        body = kwargs["json_body"]
        assert body["request"] == {"model": "gpt-image-2", "prompt": "hi"}  # plain request, no ref contract
        assert body["images"][0]["dataUrl"].startswith("data:image/png;base64,")
        assert kwargs["headers"]["Idempotency-Key"] == "k-1"

    def test_ack_carries_retained_digest(self, monkeypatch):
        seen = []

        class GW:
            def _request(self, method, path, **kwargs):
                seen.append((method, path, kwargs))
                return {}

        claudexor_images.acknowledge_image_result(GW(), "op1", "abc123")
        method, path, kwargs = seen[0]
        assert path.endswith("/op1/ack") and kwargs["json_body"] == {"sha256": "abc123"}

    def test_module_functions_reachable(self):
        for name in ("create_image_operation", "get_image_operation",
                     "get_image_result", "acknowledge_image_result",
                     "image_operation_supported"):
            assert callable(getattr(claudexor_images, name)), name
