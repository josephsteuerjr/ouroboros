"""gpt-image-2 generation through the Claudexor engine's image operations.

The tool is a thin client of a PR-CX-style ``/v2/image-operations`` family on
the owned Claudexor daemon: the engine owns subscription credentials, account
rotation and quota mapping, exactly as for model operations. The route family
is negotiated STRUCTURALLY through the engine's own ``GET /v2/operations``
catalog (``image_operation_supported``), so this tool stays dormant and
refuses typed until an engine that implements the routes serves it — no
version folklore.

Invariants carried from the working prototype (Praxis / praxis-relay):

1. ONE request = ONE attempt. A paid generation is never retried automatically:
   the idempotency key is minted per tool call, and an interrupted or unknown
   outcome surfaces as a typed ``image_outcome_unknown`` marker with the
   operation id, never as a silent second send.
2. Image quota is NOT the text quota. An upstream ``image_generation_limit_reached``
   (HTTP 429) is surfaced typed with ``resets_at`` when the engine provides it
   and does not park or deprioritise the account's text lane — that mapping is
   the engine's own quota logic, this client only reports it.
3. No base64 in model context. The result's ``b64_json`` is decoded straight
   into a content-addressed chat-media artifact; the tool returns only
   ``{path, sha256, mime, size, usage}``.
4. Generation is not delivery. Storing the artifact is the tool's commit
   point; showing it to the owner is a separate step (``send=True`` sends the
   photo through the existing owner-delivery path, bounded by its size cap).
"""

from __future__ import annotations

import base64
import logging
import uuid
from typing import Any, Dict, List, Optional

from ouroboros.artifacts import store_chat_media_bytes
from ouroboros.tools.owner_delivery import deliver_owner_event
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.tool_result import ToolResult, _publish_tool_result
from ouroboros.usage_accounting import AttemptRequest, execute_physical_attempt

log = logging.getLogger(__name__)

# The negotiated route family (mirrors PR-CX option 2). Localized to this
# module so an engine-side shape change is a one-line client fix.
IMAGE_OPERATION_PATH = "/v2/image-operations"

# Result envelope caps mirrored from the prototype's validation (relay images.rs):
# single image <= 32 MiB, whole response <= 64 MiB.
_MAX_RESULT_BYTES = 64 * 1024 * 1024
_MAX_IMAGE_BYTES = 32 * 1024 * 1024

# Prompt contract mirrored from the upstream API (relay validation): 1..32000.
_MAX_PROMPT_CHARS = 32000

# send_photo's inline delivery cap (10 MiB). Larger artifacts are delivered by
# send_file, not the photo path.
_PHOTO_INLINE_CAP = 10 * 1024 * 1024

# Magic-byte sniffing for the three formats the upstream returns.
def _sniff_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _refuse(ctx: Any, message: str, code: str) -> str:
    status = "unavailable" if code in ("CAPABILITY_UNAVAILABLE", "IMAGE_RATE_LIMITED") else "error"
    return _publish_tool_result(ctx, ToolResult(status=status, code=code, text=message))


def _gateway_for(ctx: Any):
    """Read the owned daemon gateway without starting it (a generation request
    must not wake a stopped daemon as a side effect of capability probing)."""
    from ouroboros.claudexor_daemon import read_owned_gateway

    gateway = read_owned_gateway()
    if gateway is None:
        raise ConnectionError("claudexor_daemon_absent")
    return gateway


def image_operation_supported(operations: list) -> bool:
    """Does the serving engine's own route catalog list POST /v2/image-operations?

    Same structural negotiation as ``run_message_supported``: presence of the
    route in ``GET /v2/operations`` is the capability; an engine that does not
    implement the family answers a 404 the tool must never reach.
    """
    return any(
        operation.get("method") == "POST" and operation.get("path") == IMAGE_OPERATION_PATH
        for operation in operations if isinstance(operation, dict)
    )


def _image_429_refusal(reset_at: str) -> str:
    line = ("⚠️ IMAGE_RATE_LIMITED: image generation quota exhausted for this account. "
            "The text lane is NOT parked; try again after the image window resets.")
    if reset_at:
        line += f" Window resets at: {reset_at}."
    return line


def _generate_image(ctx: ToolContext, prompt: str, n: int = 1, quality: str = "auto",
                    size: str = "auto", background: str = "auto",
                    image_paths: Optional[List[str]] = None, caption: str = "",
                    send: bool = True) -> str:
    """Generate image(s) through the engine's image operations; one attempt, no retry."""
    if not prompt or not prompt.strip():
        return _refuse(ctx, "⚠️ Prompt is required (1–32000 chars).", "TOOL_ARG_ERROR")
    if len(prompt) > _MAX_PROMPT_CHARS:
        return _refuse(ctx, f"⚠️ Prompt exceeds {_MAX_PROMPT_CHARS} characters.", "TOOL_ARG_ERROR")
    n = int(n or 1)
    if n < 1 or n > 10:
        return _refuse(ctx, "⚠️ n must be 1–10.", "TOOL_ARG_ERROR")
    if quality not in ("auto", "low", "medium", "high"):
        return _refuse(ctx, "⚠️ quality must be auto|low|medium|high.", "TOOL_ARG_ERROR")
    if background not in ("auto", "opaque", "transparent"):
        return _refuse(ctx, "⚠️ background must be auto|opaque|transparent.", "TOOL_ARG_ERROR")

    model = ""
    from ouroboros.config import runtime_setting
    try:
        model = str(runtime_setting("OUROBOROS_MODEL_IMAGE") or "")
    except Exception:
        model = ""

    try:
        gateway = _gateway_for(ctx)
    except ConnectionError as exc:
        if "claudexor_daemon_absent" in str(exc):
            return _refuse(
                ctx,
                "⚠️ CAPABILITY_UNAVAILABLE: the Claudexor daemon is not running; "
                "image generation needs the owned engine. Start it from Settings → Accounts.",
                "CAPABILITY_UNAVAILABLE",
            )
        raise

    # Capability negotiation — typed refusal BEFORE any paid request.
    try:
        operations = gateway.operations()
    except Exception as exc:
        return _refuse(ctx, f"⚠️ CAPABILITY_UNAVAILABLE: engine catalog unreadable ({type(exc).__name__}).", "CAPABILITY_UNAVAILABLE")
    if not image_operation_supported(operations):
        return _refuse(
            ctx,
            "⚠️ CAPABILITY_UNAVAILABLE: this Claudexor engine does not implement image "
            "operations (POST " + IMAGE_OPERATION_PATH + " not in /v2/operations). "
            "The tool activates structurally once the engine ships the route family.",
            "CAPABILITY_UNAVAILABLE",
        )

    request: Dict[str, Any] = {
        "model": model or "gpt-image-2",
        "prompt": prompt,
        "n": n,
        "quality": quality,
        "size": size,
        "background": background,
    }
    image_bytes_list: List[bytes] = []
    if image_paths:
        if len(image_paths) > 5:
            return _refuse(ctx, "⚠️ image_paths accepts at most 5 edit inputs.", "TOOL_ARG_ERROR")
        for raw in image_paths:
            source = pathlib.Path(str(raw)).expanduser()
            if not source.is_file():
                return _refuse(ctx, f"⚠️ Edit input not found: {raw}", "TOOL_ARG_ERROR")
            data = source.read_bytes()
            mime = _sniff_mime(data)
            if not mime:
                return _refuse(ctx, f"⚠️ Edit input is not a PNG/JPEG/WebP image: {raw}", "TOOL_ARG_ERROR")
            image_bytes_list.append(data)

    idempotency_key = f"image-{uuid.uuid4().hex}"
    op_id = ""
    artifact_rows: List[Dict[str, Any]] = []

    def _send() -> Dict[str, Any]:
        nonlocal op_id
        nonlocal op_id_ref
        op = gateway.create_image_operation(request, image_bytes=image_bytes_list, idempotency_key=idempotency_key)
        op_id = str(op.get("operationId") or op.get("id") or "")
        op_id_ref[0] = op_id
        return _wait_for_image_result(gateway, op_id, idempotency_key)

    op_id_ref = [""]

    # Accounting: one physical attempt covering create+poll+result — the
    # budget fence of the task tree is inherited by execute_physical_attempt.
    attempt_request = AttemptRequest(
        model=request["model"],
        provider="claudexor",
        prompt_tokens_estimate=max(1, len(prompt) // 4),
        max_completion_tokens=0,
        force_unknown_reservation=True,
        drive_root=getattr(ctx, "budget_drive_root", None) or getattr(ctx, "drive_root", None),
        task_id=str(getattr(ctx, "task_id", "") or ""),
        root_task_id=str(getattr(ctx, "root_task_id", "") or getattr(ctx, "task_id", "") or ""),
        category="image_generation",
        source="imagegen",
    )
    try:
        result_body = execute_physical_attempt(attempt_request, _send)
    except Exception as exc:
        code = getattr(exc, "code", "") or ""
        status = getattr(exc, "status_code", None)
        message = str(exc)
        if code == "image_generation_limit_reached" or status == 429:
            reset_at = str(getattr(exc, "resets_at", "") or "")
            return _refuse(ctx, _image_429_refusal(reset_at), "IMAGE_RATE_LIMITED")
        # Interrupted/unknown generation — NEVER retried here.
        append_jsonl_safe(
            getattr(ctx, "budget_drive_root", None) or getattr(ctx, "drive_root", None),
            {
                "type": "image_outcome_unknown",
                "operation_id": op_id_ref[0] or "",
                "idempotency_key": idempotency_key,
                "error": f"{type(exc).__name__}: {message}"[:500],
            },
        )
        return _refuse(
            ctx,
            f"⚠️ IMAGE_OUTCOME_UNKNOWN: the generation attempt ended without a settled "
            f"result (operation {op_id_ref[0] or 'unknown'}). It may have been billed — "
            f"it is NOT retried automatically. Re-read operation {op_id_ref[0]} via the "
            f"engine, or start a NEW request deliberately.",
            "IMAGE_OUTCOME_UNKNOWN",
        )

    # Result custody: decode each b64 payload straight to a chat-media artifact.
    data_rows = result_body.get("data") if isinstance(result_body, dict) else None
    if not isinstance(data_rows, list) or not data_rows:
        if op_id_ref[0]:
            append_jsonl_safe(getattr(ctx, "drive_root", None), {
                "type": "image_outcome_unknown", "operation_id": op_id_ref[0],
                "idempotency_key": idempotency_key, "error": "empty_data_rows"})
        return _refuse(ctx, "⚠️ IMAGE_OUTCOME_UNKNOWN: engine returned no image data.", "IMAGE_OUTCOME_UNKNOWN")
    from ouroboros.artifacts import store_chat_media_bytes
    from ouroboros.tools.owner_delivery import deliver_owner_event

    usage = result_body.get("usage") if isinstance(result_body.get("usage"), dict) else {}
    for row in data_rows:
        if not isinstance(row, dict):
            continue
        b64 = row.get("b64_json")
        if not b64 or not isinstance(b64, str):
            continue
        try:
            raw = base64.b64decode(b64, validate=True)
        except Exception:
            continue
        if len(raw) > _MAX_IMAGE_BYTES or not _sniff_mime(raw):
            continue
        stored = store_chat_media_bytes(
            getattr(ctx, "budget_drive_root", None) or getattr(ctx, "drive_root", None),
            str(getattr(ctx, "task_id", "") or ""),
            raw, _sniff_mime(raw),
        )
        if stored:
            artifact_rows.append(stored)
            if send and len(raw) <= _PHOTO_INLINE_CAP:
                deliver_owner_event(ctx, {
                    "type": "send_photo", "image_base64": b64, "mime": stored["mime"],
                    "caption": caption or "",
                })

    if not artifact_rows:
        return _refuse(ctx, "⚠️ IMAGE_ERROR: engine response contained no decodable image payload.", "IMAGE_ERROR")

    summary = {
        "operation_id": op_id_ref[0],
        "generated": len(artifact_rows),
        "images": [{"path": row["path"], "sha256": row["sha256"], "mime": row["mime"],
                    "size": row["size"]} for row in artifact_rows],
        "usage": usage,
    }
    return _publish_tool_result(ctx, ToolResult(
        status="ok", code="OK",
        text="OK: generated " + str(len(artifact_rows)) + " image(s).\n" + json.dumps(summary, ensure_ascii=False, indent=2),
    ))


def _wait_for_image_result(gateway: Any, op_id: str, idempotency_key: str) -> Dict[str, Any]:
    """Poll the image operation to a terminal state, then read+ACK the result.

    Single send per generation: the poll loop reads state; the result read is
    the data transfer. Both reuse the existing gateway request machinery.
    """
    import time

    deadline = time.monotonic() + 240.0
    while time.monotonic() < deadline:
        op = gateway.get_image_operation(op_id)
        state = str(op.get("state") or op.get("status") or "")
        if state in ("succeeded", "ready", "complete", "completed"):
            result = gateway.get_image_result(op_id, idempotency_key=idempotency_key)
            gateway.acknowledge_image_result(op_id, result.get("sha256", ""))
            return result
        if state in ("failed", "error", "cancelled"):
            raise RuntimeError(f"image_operation_failed:{state}:{op.get('error') or op.get('reason') or ''}")
        time.sleep(2.0)
    raise TimeoutError(f"image_operation_timeout:{op_id}")


def append_jsonl_safe(drive_root: Any, event: Dict[str, Any]) -> None:
    try:
        from ouroboros.utils import append_jsonl, utc_now_iso

        path = pathlib.Path(str(drive_root)) / "logs" / "events.jsonl"
        append_jsonl(path, {**event, "ts": utc_now_iso()})
    except Exception:
        log.exception("imagegen: failed to append event")


import json  # noqa: E402  (kept at bottom so the module docstring stays first)
import pathlib  # noqa: E402


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="generate_image",
            schema={
                "name": "generate_image",
                "description": (
                    "Generate image(s) with gpt-image-2 through the Claudexor engine's "
                    "image operations (subscription billing, Plus and up). ONE request = "
                    "ONE attempt: an interrupted generation is reported as "
                    "image_outcome_unknown and is never retried automatically. The result "
                    "is stored as a content-addressed artifact; base64 never enters the "
                    "conversation. Image quota is a separate bucket from the text quota — "
                    "an image 429 does not park the account's text lane. send=true also "
                    "delivers the first image to the owner chat as a photo (<=10 MiB; "
                    "larger stay artifact-only). Requires an engine that implements "
                    "POST /v2/image-operations; older engines get a typed refusal."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string", "description": "1–32000 chars."},
                        "n": {"type": "integer", "description": "How many images (1–10)."},
                        "quality": {"type": "string", "enum": ["auto", "low", "medium", "high"]},
                        "size": {"type": "string", "description": "auto or WxH."},
                        "background": {"type": "string", "enum": ["auto", "opaque", "transparent"]},
                        "image_paths": {
                            "type": "array", "items": {"type": "string"}, "maxItems": 5,
                            "description": "Edit mode: 1–5 input images.",
                        },
                        "caption": {"type": "string", "description": "Photo caption when send=true."},
                        "send": {"type": "boolean"},
                    },
                    "required": ["prompt"],
                },
            },
            handler=_generate_image,
            timeout_sec=300,
        ),
    ]
