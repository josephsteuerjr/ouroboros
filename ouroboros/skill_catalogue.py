"""Model-facing skill selection, distinct from the full UI/lifecycle summary.

Pages end between records and fit the existing tool-result cap. Each call is a
fresh observation; an inventory token detects changed page membership, NOT a
snapshot of mutable readiness. Oversized records use the existing actor-readable
source store, never a second catalogue cache or a larger generic cap.
"""
from __future__ import annotations

from dataclasses import fields
from hashlib import sha256
import json
import logging
from typing import Any

from ouroboros.artifacts import store_actor_source_bytes, task_id_for_artifacts
from ouroboros.skill_loader import discover_selected_skill_candidates
from ouroboros.tool_capabilities import tool_result_limit

log = logging.getLogger(__name__)

LIST_SKILLS_SCHEMA = {
    "name": "list_skills",
    "description": (
        "Choose a skill from compact whole-record pages, then request name + detail=true "
        "for its full manifest/instructions and tool metadata. Oversized records include "
        "an exact read_file source; unavailable sources are explicit. Follow next_offset "
        "with inventory from this response; changed membership asks you to restart at 0. "
        "Each call freshly reads readiness, not a state snapshot. available_for_execution "
        "is SCRIPT-only; extension desired_live/live_loaded/process/load_error are separate. "
        "No-arg calls now return the compact index, not the old full diagnostic summary."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Exact canonical skill name; empty lists the index."},
            "detail": {"type": "boolean", "description": "Full selected skill details; requires name. Default false."},
            "offset": {"type": "integer", "minimum": 0, "description": "Page offset; default 0."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Maximum records; default 100, further bounded by serialized size."},
            "inventory": {"type": "string", "description": "Copy the previous page's inventory token to detect membership drift; empty starts a new observation."},
        },
        "required": [],
    },
}

# Bounds are previews only; full values remain in named detail or its exact source.
_TEXT_FIELDS = {"description": 300, "when_to_use": 200, "load_error": 240,
                "live_reason": 160, "process": 120}
_FACT_FIELDS = ("name", "type", "version", "source", "content_hash", "enabled",
                "review_status", "review_stale", "executable_review",
                "available_for_execution", "desired_live", "live_loaded")


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _compact(row: dict) -> dict:
    out = {key: row.get(key) for key in _FACT_FIELDS}
    omitted = {}
    for key, bound in _TEXT_FIELDS.items():
        text = str(row.get(key) or "")
        out[key] = text[:bound]
        if len(text) > bound:
            omitted[key] = len(text) - bound
    readiness = row.get("readiness") or {}
    blockers = readiness.get("blockers") or []
    out["readiness_blockers"] = [str(value)[:160] for value in blockers[:3]]
    out["blockers_omitted"] = max(0, len(blockers) - 3)
    omitted["readiness_blockers"] = sum(max(0, len(str(value)) - 160) for value in blockers[:3])
    out["omitted_chars"] = omitted
    return out


def _detail(ctx: Any, row: dict, drive_root: Any) -> dict:
    """Use canonical selected discovery; never select a colliding location."""
    candidates = discover_selected_skill_candidates(drive_root, str(row["name"]))
    if len(candidates) != 1 or candidates[0].identity_collision:
        return {**_compact(row), "detail_error": "identity_collision_or_unavailable"}
    selected = candidates[0]
    if selected.content_hash != row.get("content_hash"):
        return {**_compact(row), "detail_error": "payload_changed_during_read"}
    # Manifest has the complete body, scripts, permissions and constraints; the
    # summary already owns readiness/liveness. Discovery does not enable anything.
    from ouroboros.extension_loader import _lock, _tools
    with _lock:
        schemas = [{"name": tool.get("name"), "description": tool.get("description"),
                    "schema": tool.get("schema")}
                   for tool in _tools.values() if tool.get("skill") == selected.name]
    manifest = {field.name: getattr(selected.manifest, field.name)
                for field in fields(selected.manifest)}
    try:
        _encode(manifest)
    except (TypeError, ValueError):
        # The tolerant parser accepts YAML dates, sets and aliases. Keep their
        # meaning in YAML rather than silently stringifying/dropping extras.
        import yaml
        manifest = {"body": selected.manifest.body, "representation": "yaml",
                    "yaml": yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False)}
    return {**row, "manifest": manifest, "tool_schemas": schemas}


def _source_record(ctx: Any, row: dict) -> dict:
    """An over-limit row keeps a small identity plus an exact source or a gap."""
    identity = {key: row.get(key) for key in ("name", "type", "source", "content_hash")}
    data = _encode(row).encode("utf-8")
    out = {**identity, "oversized": True, "complete_chars": len(data.decode("utf-8")),
           "source_status": "unavailable"}
    try:
        ref = store_actor_source_bytes(
            ctx.drive_root, task_id_for_artifacts(ctx), category="tool_results",
            source_id="skill-detail", data=data, extension="json",
        )
        out.update(source_status="available", source_ref=ref)
    except Exception:
        log.warning("Skill catalogue source persistence unavailable", exc_info=True)
    return out


def render_skill_catalogue(ctx: Any, summary: dict, drive_root: Any, *,
                          name: str = "", detail: bool = False, offset: int = 0,
                          limit: int = 100, inventory: str = "") -> str:
    """Project one fresh full summary without changing its other consumers."""
    if not isinstance(name, str) or not isinstance(inventory, str) or not isinstance(detail, bool):
        raise ValueError("name/inventory must be strings and detail must be boolean")
    if detail and not name:
        raise ValueError("detail=true requires an exact name")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("offset must be a nonnegative integer")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer in 1..100")
    rows = sorted(summary.get("skills") or [], key=lambda r: (
        str(r.get("name") or ""), str(r.get("source") or ""), str(r.get("content_hash") or "")))
    token = sha256(_encode([(r.get("name"), r.get("source"), r.get("content_hash"))
                            for r in rows]).encode("utf-8")).hexdigest()
    selected = [r for r in rows if r.get("name") == name] if name else rows
    total = len(selected)
    page_offset = min(offset, total)
    # Shared count fields survive. No arbitrary summary strings can inflate the
    # envelope; new fields need deliberate projection, not an unrestricted splat.
    out = {key: summary.get(key) for key in ("count", "available", "blocked_by_grants",
           "pending_review", "blocker_review", "warning_review", "broken")}
    out.update(ok=True, format="detail" if detail else "compact", inventory=token,
               total=total, offset=page_offset, returned=0, next_offset=None,
               skills=[], observation="fresh_read; inventory binds membership, not readiness")
    budget = tool_result_limit("list_skills") - 1000  # room for existing host annotations
    if inventory and inventory != token:
        out.update(ok=False, error="inventory_changed", restart_offset=0)
        return _encode(out)
    if name and not selected:
        out.update(ok=False, error="skill_not_found")
    for row in selected[page_offset:page_offset + limit]:
        projected = _detail(ctx, row, drive_root) if detail else _compact(row)
        index = page_offset + len(out["skills"])
        next_offset = index + 1 if index + 1 < total else None
        candidate = {**out, "skills": out["skills"] + [projected],
                     "returned": len(out["skills"]) + 1, "next_offset": next_offset}
        if len(_encode(candidate)) > budget:
            if out["skills"]:
                break
            projected = _source_record(ctx, projected if detail else row)
            candidate.update(skills=[projected], returned=1, next_offset=next_offset)
            if len(_encode(candidate)) > budget:
                # Even a hostile identity can exceed the cap. Preserve it only
                # in the exact source; the ordinal still advances the page.
                candidate["skills"] = [{k: v for k, v in projected.items()
                                        if k not in {"name", "type", "source", "content_hash"}}]
        if projected.get("detail_error") or projected.get("source_status") == "unavailable":
            candidate["ok"] = False
        out = candidate
    if out["skills"]:
        end = page_offset + len(out["skills"])
        out["next_offset"] = end if end < total else None
    encoded = _encode(out)
    if len(encoded) > budget:
        raise ValueError("catalogue envelope exceeds the tool result budget")
    return encoded
