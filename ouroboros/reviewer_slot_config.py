"""The review pool: the marked rows of the subagent catalog, projected as reviewer rows.

PR-3 (one transition): a reviewer is a row of ``OUROBOROS_SUBAGENTS`` whose owner
marked it ``review_eligible`` and left it enabled; the former
``OUROBOROS_REVIEWER_SLOTS`` lanes are gone as a configuration surface, and every
review surface (commit gate, plan review, skill review, task acceptance, the
author's ``review_change`` wave) reads ONE builder, ``review_pool_slots``. A seat's
identity IS the row's stored id (``slot_id == subagent_id``); model, route, pin,
effort, processing and delivery are the row's own facts, read at wave time.

``delivery`` of an ``api_model`` row (``native``: bounded native tool rounds on the
row's route; ``packet``: the assembled pack) is a catalog field, native by default;
a session row always retrieves. ``ReviewSlot.native_retrieval`` derives from that
field alone — never from the presence of a subagent id.

The pool ignores the catalog's global switch (``payload.enabled`` governs
delegation, not review) and reads the catalog WITHOUT the delegation resolver; a
row with ``enabled: false`` is not in the pool. An empty pool is a configured fact
(``review_pool_state`` → ``empty``): a save that leaves rows but no marks is refused
unless the owner says ``allow_empty_review_pool`` (``review_pool_save_error``).

The lane readers still below (``parse_reviewer_slots`` and helpers,
``_default_config``, ``load_reviewer_slot_config``, the lane projections) are
marked ``removed by package A after package C freezes the lane readers``: package
C copies the document-reading versions into ``review_pool_migration.py`` first,
then the integrator deletes them here. No review surface configures from them.

Malformed configuration RAISES: a typo mapped to ``api_chat`` would silently spend
the API money the owner moved the row off of; mapped to ``agent_session`` it would
silently delegate a row the owner never delegated.
"""

from __future__ import annotations

import contextlib as _contextlib
import json
import os
from ouroboros.settings_integrity import runtime_environ, runtime_setting
from ouroboros.model_slots import normalize_processing_preference, resolve_processing_preference
import pathlib
import threading
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

if TYPE_CHECKING:  # annotation-only; review_records imports this module's leaves at call time
    from ouroboros.review_records import ReviewSlot

from ouroboros.route_spec import (
    ROUTE_KIND_AGENT_SESSION as SHARED_ROUTE_KIND_SESSION,
    ROUTE_KIND_API_MODEL as SHARED_ROUTE_KIND_API,
    RouteSpec,
    compound_session_effort,
    parse_route_spec,
    validate_compound_session_effort,
)

REVIEWER_SLOTS_ENV = "OUROBOROS_REVIEWER_SLOTS"

ROUTE_KIND_API = "api_chat"
ROUTE_KIND_SESSION = "agent_session"
# The one role hint every scope row carries (structured or default panel); the
# commit gate's wave admission renders a native scope seat's work-order with it.
SCOPE_ROLE_HINT = "scope reviewer"

# removed by package A after package C freezes the lane readers: the per-lane
# ceilings. The pool's one ceiling is the catalog's ``MAX_CONFIGURED_SUBAGENTS``
# (which ``tools.review_multi_model.MAX_MODELS`` now IS); these two survive only
# for the lane parser and ``review_change.compose_panel`` until packages C/D land.
TRIAD_SLOT_LIMIT = 10
SCOPE_SLOT_LIMIT = 4

_SLOT_ID_MAX_CHARS = 64

# The saved delivery of an api row (``ConfiguredReviewerSlot.delivery``): the
# catalog's vocabulary (``configured_subagents.REVIEW_DELIVERIES``).
DELIVERY_NATIVE = "native"
DELIVERY_PACKET = "packet"
# removed by package A after package C freezes the lane readers (the shipped
# default panel's triad delivery; the pool has no default panel).
DEFAULT_TRIAD_DELIVERY = DELIVERY_NATIVE

# The deep self-review row is a singleton like the advisory: its identity is
# fixed (the UI reads «Выполняется как» under this id), never owner-minted.
DEEP_REVIEW_SLOT_ID = "deep_review_slot_1"


@dataclass(frozen=True)
class ConfiguredReviewerSlot:
    """One configured reviewer row: identity, delivery, strength."""

    slot_id: str
    kind: str  # api_chat | agent_session
    target_id: str  # API model id, or opaque ``harness[=model]`` session spec
    # Empty means a compound Cursor/Agy route's encoded effort when present,
    # otherwise the surface's established default.
    effort: str = ""
    # The opaque per-row session spec. Structured agent_session rows carry
    # their target here; api rows carry ''. Legacy session rows resolve the
    # same shared route once into this row so delivery/fingerprint see one fact.
    session_target: str = ""
    # Optional managed account pin for session or raw-model delivery; '' = Auto.
    profile_id: str = ""
    # Optional configured-subagent reference (OUROBOROS_SUBAGENTS row id).
    # Mutually exclusive with an inline route in the STORED form; when set, the
    # execution fields above were resolved from the frozen roster row at load
    # time and the roster stays their SSOT. '' = ordinary direct row.
    subagent_id: str = ""
    use_local: Optional[bool] = None  # Runtime task override only; never a second settings policy.
    processing_preference: str = ""  # Effective preference captured when the row is loaded.
    # An api row's saved delivery (the catalog field): "native" reads the
    # subject in bounded native tool rounds, "packet" receives the assembled
    # pack (the fallback for a model without tool calling). '' means native —
    # the catalog's default; the old "empty = packet" reading of a lane row
    # lives only inside the lane reader (``_parse_slot`` writes it explicitly).
    delivery: str = ""

    @property
    def is_session(self) -> bool:
        return self.kind == ROUTE_KIND_SESSION

    @property
    def native_retrieval(self) -> bool:
        """An ``api_chat`` row that reads the subject itself in bounded native
        tool rounds: every api row whose delivery is not ``packet``. Derived
        from the delivery field ALONE — a catalog row's id says nothing about
        how it delivers (F8).

        Kept OFF the closed public route vocabulary (``api_chat`` stays the
        wire kind); executor selection and admission read this derived fact.
        """
        return self.kind == ROUTE_KIND_API and self.delivery != DELIVERY_PACKET

    @property
    def retrieves(self) -> bool:
        """Delivery class: the reviewer reads the subject with its own tools.

        THE predicate admission/fit/authority callers must use instead of
        route-name comparisons — a session row and a native-retrieval api row
        are one class here, and neither receives an assembled packet.
        """
        return self.is_session or self.native_retrieval


# removed by package A after package C freezes the lane readers.
@dataclass(frozen=True)
class AdvisorySlotConfig:
    """The ONE optional advisory reviewer (D14) — on the shared row vocabulary.

    ``enabled=False`` is a standing owner decision with a constitutional
    consequence the UI must state: every reviewed commit then records an
    AUDITED BYPASS instead of an advisory verdict (never a silent skip).

    Delivery follows the shared closed kinds: an ``api_chat`` advisory row is
    a routed catalog model that runs the bounded NATIVE inspection episode
    (advisory is an inspection critic by definition — it never receives an
    assembled packet), ``agent_session`` is a delegated Claudexor run, and a
    ``subagent_id`` reference resolves the configured roster row. The retired
    legacy ``api`` kind (Claude-Agent-SDK spellings) is migrated at parse:
    a translatable target becomes its routed id; an untranslatable one keeps
    the row DISABLED with a loud typed reason, never a silently swapped model.
    """

    enabled: bool = True
    kind: str = ROUTE_KIND_API  # api_chat | agent_session
    # agent_session: harness[=model] spec ('' = shared route). api_chat: a
    # routed catalog model id ('' = the shipped advisory default).
    target_id: str = ""
    # api_chat keeps the historical low default. Session ``""`` means the
    # route's own default; an explicit/compound route effort is materialized on
    # legacy migration so Settings round-trips one authority.
    effort: str = "low"
    profile_id: str = ""  # optional manual credential pin (Q2-в); '' = rotation
    # Configured-subagent reference ('' = direct row); resolved at parse into
    # the execution fields above, exactly like triad/scope actor rows.
    subagent_id: str = ""
    # Non-empty ⇒ the row was force-disabled at parse with this typed reason
    # (currently only the unmapped legacy Claude-SDK target migration).
    disabled_reason: str = ""
    use_local: Optional[bool] = None  # Runtime task override, not serialized configuration.
    processing_preference: str = ""  # Effective preference, including a referenced actor's choice.

    @property
    def slot_id(self) -> str:
        return "advisory_slot_1"  # The existing single advisory actor identity.


# removed by package A after package C freezes the lane readers.
@dataclass(frozen=True)
class ReviewerSlotConfig:
    triad: Tuple[ConfiguredReviewerSlot, ...]
    scope: Tuple[ConfiguredReviewerSlot, ...]
    advisory: AdvisorySlotConfig
    source: str  # "structured" | "default" (ABI 7.0: the legacy read is gone)
    # The optional deep self-review row on the shared vocabulary (no
    # ``enabled``: a deep review is owner-triggered, never a standing gate).
    # None = not configured; ``deep_review_slot`` then synthesizes the api
    # row from the deep-review model key. Every deep-review api row (bare or
    # subagent-bound) runs the native inspection episode and an agent_session
    # row a delegated session: the surface declares retrieval for all its rows.
    deep_review: Optional[ConfiguredReviewerSlot] = None


# removed by package A after package C freezes the lane readers.
def structured_reviewer_slots_raw() -> str:
    return str(runtime_setting(REVIEWER_SLOTS_ENV, "") or "").strip()


# removed by package A after package C freezes the lane readers.
def structured_reviewer_slots_present() -> bool:
    return bool(structured_reviewer_slots_raw())


def _valid_effort(value: Any, where: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(
            f"{REVIEWER_SLOTS_ENV}: {where} effort must be a string"
        )
    effort = value.strip().lower()
    if not effort:
        return ""
    from ouroboros.config import EFFORT_SCALE

    if effort not in EFFORT_SCALE:
        raise ValueError(
            f"{REVIEWER_SLOTS_ENV}: {where} names an unknown effort {effort!r}; "
            f"valid: {', '.join(EFFORT_SCALE)}"
        )
    return effort


def _validate_concrete_session_target(route: RouteSpec, where: str) -> None:
    """A structured session row names one concrete delegated route.

    ``parse_route_spec`` owns the shared JSON shape and deliberately accepts an
    opaque target.  Reviewer rows additionally promise exact delivery, so a
    non-empty sentinel/malformed target that the canonical delegated-route
    parser resolves to ``None`` must be refused here instead of reaching a
    consumer that may interpret ``None`` as permission to use a shared route.
    """
    if not route.is_session or not route.target_id:
        return
    from ouroboros.subagents import parse_subagent_harness

    if parse_subagent_harness(route.target_id) is None:
        raise ValueError(
            f"{REVIEWER_SLOTS_ENV}: {where} session target "
            f"{route.target_id!r} does not name a concrete harness route"
        )


import contextvars as _contextvars
_ROSTER_ENV_OVERRIDE: "_contextvars.ContextVar[Optional[dict]]" = _contextvars.ContextVar(
    "reviewer_roster_env_override", default=None)


def _row_processing(raw: Any = None, *, role: str = "") -> str:
    """Capture authored row/role/global precedence on the same settings plane."""
    return resolve_processing_preference(
        role, override=normalize_processing_preference(raw) or None,
        settings=_ROSTER_ENV_OVERRIDE.get(),
    )


def _catalog_row_slot(row: Any, settings: Any, *, where: str = "") -> ConfiguredReviewerSlot:
    """ONE catalog row as the frozen reviewer row every review surface runs.

    ``slot_id`` IS the row's stored id; model, route, pin, effort, processing and
    delivery are the row's own facts under ``settings`` (the same effective
    identity ``configured_subagents.engine_identity`` names). A session row
    additionally promises one concrete harness route, as a lane row did.
    """
    where = where or f"row {row.subagent_id!r}"
    route = row.route
    processing = resolve_processing_preference(
        override=row.processing_preference or None, settings=dict(settings or {}))
    if route.is_session:
        _validate_concrete_session_target(route, where)
        return ConfiguredReviewerSlot(
            slot_id=row.subagent_id, kind=ROUTE_KIND_SESSION, target_id=route.target_id,
            effort=row.effort, session_target=route.target_id, profile_id=route.credential_profile_id,
            subagent_id=row.subagent_id, processing_preference=processing,
        )
    return ConfiguredReviewerSlot(
        slot_id=row.subagent_id, kind=ROUTE_KIND_API, target_id=route.target_id,
        effort=row.effort, profile_id=route.credential_profile_id, subagent_id=row.subagent_id,
        processing_preference=processing, delivery=row.delivery or DELIVERY_NATIVE,
    )


def _catalog_settings(snapshot: Any = None) -> Any:
    """The settings plane the pool reads: an explicit task snapshot, else the
    save-time roster override, else the applied runtime environment."""
    if snapshot is not None:
        return snapshot
    override = _ROSTER_ENV_OVERRIDE.get()
    return override if override is not None else runtime_environ()


def _parse_catalog(settings: Any) -> Any:
    """The catalog document itself (``parse_configured_subagents``), WITHOUT the
    delegation resolver: the pool must read a switched-off catalog's marked rows
    (review is not switched off by a transfer, F6). ``None`` when the key is
    absent or blank; a malformed document raises its typed error."""
    from ouroboros.configured_subagents import SUBAGENTS_SETTING, parse_configured_subagents

    raw = settings.get(SUBAGENTS_SETTING) if hasattr(settings, "get") else None
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    return parse_configured_subagents(raw)


def _pool_rows(settings: Any) -> List[ConfiguredReviewerSlot]:
    """The pool under ``settings``: enabled rows the owner marked review-eligible,
    in catalog order. The catalog's global switch is deliberately not consulted."""
    config = _parse_catalog(settings)
    if config is None:
        return []
    return [
        _catalog_row_slot(row, settings, where=f"items[{index}]")
        for index, row in enumerate(config.items)
        if row.enabled and row.review_eligible
    ]


def review_pool_rows(snapshot: Any = None) -> List[ConfiguredReviewerSlot]:
    """The pool as frozen reviewer rows (not yet delivery slots): catalog order,
    ``slot_id == subagent_id``. ``snapshot`` as in :func:`review_pool_slots`."""
    return _pool_rows(_catalog_settings(snapshot))


def catalog_review_row(snapshot: Any, selector: str) -> ConfiguredReviewerSlot:
    """ONE enabled catalog row named by its handle or stored id, as a reviewer row.

    The author's ``review_change`` names any enabled row — marked or not — as a
    seat of its own wave; the catalog's global switch is not consulted (it
    governs delegation). An unknown or ambiguous selector, a disabled row, an
    absent or malformed catalog are the parser's typed ``ValueError``.
    """
    from ouroboros.configured_subagents import SUBAGENTS_SETTING, resolve_roster_selector

    settings = _catalog_settings(snapshot)
    config = _parse_catalog(settings)
    if config is None:
        raise ValueError(f"{SUBAGENTS_SETTING} is not configured; no row is named {selector!r}")
    row, code, detail = resolve_roster_selector(config, str(selector or ""), settings)
    if row is None:
        raise ValueError(f"{SUBAGENTS_SETTING}: {code}: {detail}")
    if not row.enabled:
        raise ValueError(f"{SUBAGENTS_SETTING}: row {selector!r} is switched off (enabled: false)")
    return _catalog_row_slot(row, settings)


def _stored_id_for_selector(selector: str) -> str:
    """The stored id a catalog handle names, or '' when nothing resolves."""
    from ouroboros.configured_subagents import resolve_roster_selector

    try:
        settings = _catalog_settings()
        config = _parse_catalog(settings)
        row = resolve_roster_selector(config, selector, settings)[0] if config is not None else None
    except ValueError:
        return ""
    return row.subagent_id if row is not None else ""


def review_pool_state(raw: Any) -> Dict[str, str]:
    """``{"state": structured|empty|error, "error": str}`` of ONE catalog document.

    ``structured``: at least one enabled, marked row; ``empty``: a readable
    catalog (or none) with no such row — a configured fact, never an absence;
    ``error``: a malformed catalog, with its typed text.
    """
    try:
        rows = _pool_rows({"OUROBOROS_SUBAGENTS": raw})
    except ValueError as exc:
        return {"state": "error", "error": str(exc)}
    return {"state": "structured" if rows else "empty", "error": ""}


def review_pool_save_error(raw: Any, *, allow_empty: bool) -> str:
    """The SAVE-path judge of an empty pool; ``""`` means acceptable.

    A catalog that has rows but no review mark would silently leave every review
    surface without a reviewer, so the save is refused (400) unless the owner
    says ``allow_empty_review_pool`` — the one place "empty" is confirmed rather
    than inferred. An absent catalog, a catalog without rows, or a marked row
    pass; a malformed catalog returns its typed text.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ""
    try:
        config = _parse_catalog({"OUROBOROS_SUBAGENTS": raw})
    except ValueError as exc:
        return str(exc)
    if config is None or not config.items or allow_empty:
        return ""
    if any(row.enabled and row.review_eligible for row in config.items):
        return ""
    return "no reviewers marked; mark at least one row or save with `allow_empty_review_pool`"


# removed by package A after package C freezes the lane readers (the actor
# reference of a lane row; the pool reads the catalog row itself).
def _resolve_actor_slot(
    slot_id: str, subagent_id: str, effort: str, where: str,
) -> ConfiguredReviewerSlot:
    """A lane row's configured-subagent reference as one frozen reviewer row
    (resolved at load time; an unknown/disabled/invalid reference is the parser's
    typed ValueError, never a silent fallback)."""
    from ouroboros.subagent_runtime import SubagentSelectionError, select_subagent_snapshot

    try:
        # The applied env, or the save handler's incoming roster via the
        # context-local override (never by mutating the process env).
        _override = _ROSTER_ENV_OVERRIDE.get()
        snapshot, _legacy = select_subagent_snapshot(
            _override if _override is not None else runtime_environ(),
            subagent_id=subagent_id,
        )
    except SubagentSelectionError as exc:
        raise ValueError(
            f"{REVIEWER_SLOTS_ENV}: {where} subagent_id {subagent_id!r} does "
            f"not resolve: {exc.code}: {exc.detail}"
        ) from exc
    route = dict(snapshot.get("route") or {})
    target = str(route.get("target_id") or "")
    pin = str(route.get("credential_profile_id") or "")
    # Explicit row effort wins; otherwise the roster row's own effort; an empty
    # result falls through to the surface default via row_effort, as always.
    chosen_effort = effort or _valid_effort(snapshot.get("effort"), where)
    if str(route.get("kind") or "") == SHARED_ROUTE_KIND_SESSION:
        shared = RouteSpec(
            kind=SHARED_ROUTE_KIND_SESSION, target_id=target,
            credential_profile_id=pin,
        )
        _validate_concrete_session_target(shared, where)
        validate_compound_session_effort(
            shared, chosen_effort, setting=REVIEWER_SLOTS_ENV, where=where,
        )
        return ConfiguredReviewerSlot(
            slot_id=slot_id, kind=ROUTE_KIND_SESSION, target_id=target,
            effort=chosen_effort, session_target=target, profile_id=pin,
            subagent_id=subagent_id,
            processing_preference=str(snapshot.get("processing_preference") or ""),
        )
    return ConfiguredReviewerSlot(
        slot_id=slot_id, kind=ROUTE_KIND_API, target_id=target,
        effort=chosen_effort, subagent_id=subagent_id, profile_id=pin,
        processing_preference=str(snapshot.get("processing_preference") or ""),
    )


def _parse_delivery(row: Dict[str, Any], where: str, *, allowed: bool) -> str:
    """A direct api_chat triad row's saved delivery; refused wherever it means nothing."""
    if "delivery" not in row:
        return ""
    if not allowed:
        raise ValueError(
            f"{REVIEWER_SLOTS_ENV}: {where} delivery applies only to a direct api_chat triad row "
            "(sessions, subagent references, scope, advisory and deep review rows always read)")
    value = row["delivery"]
    if value not in (DELIVERY_NATIVE, DELIVERY_PACKET):
        raise ValueError(
            f"{REVIEWER_SLOTS_ENV}: {where} delivery must be {DELIVERY_NATIVE!r} or {DELIVERY_PACKET!r}")
    return value


# removed by package A after package C freezes the lane readers.
def _parse_slot(row: Any, where: str, seen_ids: set, *, delivery_allowed: bool = False) -> ConfiguredReviewerSlot:
    if not isinstance(row, dict):
        raise ValueError(f"{REVIEWER_SLOTS_ENV}: {where} is not an object")
    unknown = sorted(set(row) - {"slot_id", "route", "subagent_id", "effort", "processing_preference", "delivery"})
    if unknown:
        raise ValueError(
            f"{REVIEWER_SLOTS_ENV}: {where} has unknown keys: {unknown}"
        )
    raw_slot_id = row.get("slot_id")
    if not isinstance(raw_slot_id, str):
        raise ValueError(
            f"{REVIEWER_SLOTS_ENV}: {where} slot_id must be a string"
        )
    slot_id = raw_slot_id.strip()
    if not slot_id or len(slot_id) > _SLOT_ID_MAX_CHARS:
        raise ValueError(
            f"{REVIEWER_SLOTS_ENV}: {where} needs a stable non-empty slot_id "
            f"(≤{_SLOT_ID_MAX_CHARS} chars) — identity is never an array index"
        )
    if slot_id in seen_ids:
        raise ValueError(
            f"{REVIEWER_SLOTS_ENV}: slot_id {slot_id!r} appears twice; a row's "
            "receipts can only line up with ONE history"
        )
    seen_ids.add(slot_id)
    raw_ref = row.get("subagent_id")
    if raw_ref is not None and not isinstance(raw_ref, str):
        raise ValueError(
            f"{REVIEWER_SLOTS_ENV}: {where} subagent_id must be a string"
        )
    actor_ref = str(raw_ref or "").strip()
    if raw_ref is not None and not actor_ref:
        raise ValueError(
            f"{REVIEWER_SLOTS_ENV}: {where} subagent_id must not be empty"
        )
    if actor_ref and row.get("route") is not None:
        raise ValueError(
            f"{REVIEWER_SLOTS_ENV}: {where} must use either route or "
            "subagent_id, not both — the roster row is the route's SSOT"
        )
    if actor_ref:
        if "processing_preference" in row:
            raise ValueError(f"{REVIEWER_SLOTS_ENV}: {where} inherits Processing from its subagent")
        _parse_delivery(row, where, allowed=False)
        return _resolve_actor_slot(
            slot_id, actor_ref, _valid_effort(row.get("effort"), where), where,
        )
    route = parse_route_spec(
        row.get("route"), setting=REVIEWER_SLOTS_ENV, where=where,
        kind_aliases={
            ROUTE_KIND_API: SHARED_ROUTE_KIND_API,
            ROUTE_KIND_SESSION: SHARED_ROUTE_KIND_SESSION,
        },
        pin_key="profile_id",
        reject_unknown=True,
        strict_strings=True,
        reject_api_pin=True,
    )
    kind = ROUTE_KIND_SESSION if route.is_session else ROUTE_KIND_API
    _validate_concrete_session_target(route, where)
    effort = _valid_effort(row.get("effort"), where)
    validate_compound_session_effort(
        route, effort, setting=REVIEWER_SLOTS_ENV, where=where,
    )
    direct_api_triad = delivery_allowed and kind == ROUTE_KIND_API
    # The lane reader's own reading of an absent delivery: a direct api triad row
    # saved before the field existed received the packet. Written explicitly so
    # the slot's derived facts never depend on this legacy meaning.
    delivery = _parse_delivery(row, where, allowed=direct_api_triad) or (DELIVERY_PACKET if direct_api_triad else "")
    return ConfiguredReviewerSlot(
        slot_id=slot_id, kind=kind, target_id=route.target_id,
        effort=effort,
        session_target=route.target_id if kind == ROUTE_KIND_SESSION else "",
        profile_id=route.credential_profile_id,
        processing_preference=_row_processing(row.get("processing_preference")),
        delivery=delivery,
    )


def _migrate_sdk_advisory_target(raw_kind: str, target: str) -> tuple[str, str]:
    """Translate a retired Claude-SDK ``api``-kind target to ``(routed, reason)``.

    The Claude-Agent-SDK advisory transport is retired (owner decision,
    2026-08-29): its rows migrate to the routed catalog. Only translations
    that keep the SAME model are performed; anything else keeps the row
    DISABLED with a typed reason — a silently swapped reviewer model is the
    exact class this parser exists to refuse.
    """
    if raw_kind != "api":
        return target, ""
    base = target.replace("[1m]", "").strip()
    if not base or base in {"sonnet", "claude-sonnet-5"}:
        # '' and the shipped default spelling both meant claude-sonnet-5.
        return "", ""
    if "/" in base or "::" in base:
        return target, ""  # already a routed/provider-tagged id
    if base.startswith("claude-"):
        return f"anthropic/{base}", ""
    return target, "legacy_claude_sdk_target_unmapped"


def _resolve_advisory_actor(subagent_id: str, effort: str, enabled: bool) -> AdvisorySlotConfig:
    row = _resolve_actor_slot("advisory_slot_1", subagent_id, effort, "advisory")
    return AdvisorySlotConfig(
        enabled=enabled, kind=row.kind, target_id=row.target_id,
        effort=row.effort or ("low" if not row.is_session else ""),
        profile_id=row.profile_id, subagent_id=subagent_id,
        processing_preference=row.processing_preference,
    )


# removed by package A after package C freezes the lane readers.
def _parse_advisory(raw: Any) -> AdvisorySlotConfig:
    if raw is None:
        return AdvisorySlotConfig(processing_preference=_row_processing())
    if not isinstance(raw, dict):
        raise ValueError(f"{REVIEWER_SLOTS_ENV}: advisory must be an object")
    unknown = sorted(set(raw) - {"enabled", "route", "kind", "target_id", "effort", "subagent_id", "processing_preference"})
    if unknown:
        raise ValueError(
            f"{REVIEWER_SLOTS_ENV}: advisory has unknown keys: {unknown}"
        )
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"{REVIEWER_SLOTS_ENV}: advisory enabled must be a boolean")
    for key in ("kind", "target_id", "subagent_id"):
        if key in raw and not isinstance(raw[key], str):
            raise ValueError(
                f"{REVIEWER_SLOTS_ENV}: advisory {key} must be a string"
            )
    actor_ref = str(raw.get("subagent_id") or "").strip()
    if "subagent_id" in raw and not actor_ref:
        raise ValueError(f"{REVIEWER_SLOTS_ENV}: advisory subagent_id must not be empty")
    route = raw.get("route")
    if route is not None and not isinstance(route, dict):
        # The same typed refusal _parse_slot gives; an AttributeError would
        # escape every ``except ValueError`` that treats this parser as authority.
        raise ValueError(f"{REVIEWER_SLOTS_ENV}: advisory route must be an object "
                         "{kind, target_id}")
    if actor_ref and (route is not None or ({"kind", "target_id"} & set(raw))):
        raise ValueError(
            f"{REVIEWER_SLOTS_ENV}: advisory must use either subagent_id or a "
            "route, not both — the roster row is the route's SSOT"
        )
    if actor_ref:
        if "processing_preference" in raw:
            raise ValueError(f"{REVIEWER_SLOTS_ENV}: advisory inherits Processing from its subagent")
        return _resolve_advisory_actor(
            actor_ref, _valid_effort(raw.get("effort"), "advisory"), enabled,
        )
    if route is not None and ({"kind", "target_id"} & set(raw)):
        raise ValueError(
            f"{REVIEWER_SLOTS_ENV}: advisory must use either route or legacy "
            "kind/target_id, not both"
        )
    route_payload = dict(route or {})
    if "kind" not in route_payload:
        route_payload["kind"] = raw.get("kind") or ROUTE_KIND_API
    if "target_id" not in route_payload:
        route_payload["target_id"] = raw.get("target_id") or ""
    raw_kind = str(route_payload.get("kind") or "").strip().lower()
    shared_route = parse_route_spec(
        route_payload,
        setting=REVIEWER_SLOTS_ENV,
        where="advisory",
        kind_aliases={
            "api": SHARED_ROUTE_KIND_API,
            ROUTE_KIND_API: SHARED_ROUTE_KIND_API,
            ROUTE_KIND_SESSION: SHARED_ROUTE_KIND_SESSION,
        },
        pin_key="profile_id",
        allow_empty_target=True,
        reject_unknown=True,
        strict_strings=True,
        reject_api_pin=True,
    )
    if enabled and shared_route.is_session and not shared_route.target_id:
        raise ValueError(
            f"{REVIEWER_SLOTS_ENV}: enabled advisory agent_session route needs "
            "a non-empty target_id; shared-session fallback is legacy-only"
        )
    _validate_concrete_session_target(shared_route, "advisory")
    effort = _valid_effort(raw.get("effort"), "advisory")
    if not effort and not shared_route.is_session:
        effort = "low"
    validate_compound_session_effort(
        shared_route, effort, setting=REVIEWER_SLOTS_ENV, where="advisory",
    )
    target, disabled_reason = (
        _migrate_sdk_advisory_target(raw_kind, shared_route.target_id)
        if not shared_route.is_session else (shared_route.target_id, "")
    )
    if disabled_reason:
        import logging

        logging.getLogger(__name__).warning(
            "advisory row disabled: legacy Claude-SDK target %r has no same-model "
            "routed translation; pick a routed model or a configured subagent in "
            "Settings → Review lanes", target,
        )
    return AdvisorySlotConfig(
        enabled=enabled and not disabled_reason,
        kind=ROUTE_KIND_SESSION if shared_route.is_session else ROUTE_KIND_API,
        target_id=target,
        effort=effort,
        profile_id=shared_route.credential_profile_id,
        disabled_reason=disabled_reason,
        processing_preference=_row_processing(raw.get("processing_preference")),
    )


# removed by package A after package C freezes the lane readers.
def _parse_deep_review(raw: Any, seen_ids: set) -> Optional[ConfiguredReviewerSlot]:
    """The optional deep self-review row: the shared row vocabulary minus
    ``slot_id`` (a singleton's identity is fixed) and minus ``enabled``."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{REVIEWER_SLOTS_ENV}: deep_review must be an object")
    unknown = sorted(set(raw) - {"route", "subagent_id", "effort", "processing_preference"})
    if unknown:
        raise ValueError(f"{REVIEWER_SLOTS_ENV}: deep_review has unknown keys: {unknown}")
    return _parse_slot({**raw, "slot_id": DEEP_REVIEW_SLOT_ID}, "deep_review", seen_ids)


# removed by package A after package C freezes the lane readers.
def parse_reviewer_slots(raw: str) -> ReviewerSlotConfig:
    """Strict parse of the retired structured lane setting. Raises ValueError, row-precise."""
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"{REVIEWER_SLOTS_ENV} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{REVIEWER_SLOTS_ENV} must be a JSON object")
    unknown = sorted(set(payload) - {"triad", "scope", "advisory", "deep_review"})
    if unknown:
        raise ValueError(f"{REVIEWER_SLOTS_ENV} has unknown top-level keys: {unknown}")
    seen_ids: set = set()
    groups: Dict[str, List[ConfiguredReviewerSlot]] = {}
    for group, limit in (("triad", TRIAD_SLOT_LIMIT), ("scope", SCOPE_SLOT_LIMIT)):
        rows = payload.get(group)
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise ValueError(f"{REVIEWER_SLOTS_ENV}: {group} must be an array")
        if len(rows) > limit:
            raise ValueError(
                f"{REVIEWER_SLOTS_ENV}: {group} has {len(rows)} rows; the real "
                f"limit is {limit} (shown in the UI, not negotiable here)"
            )
        groups[group] = [
            _parse_slot(row, f"{group}[{idx}]", seen_ids, delivery_allowed=group == "triad")
            for idx, row in enumerate(rows)
        ]
    if not groups["triad"]:
        raise ValueError(f"{REVIEWER_SLOTS_ENV}: triad needs at least one slot")
    if not groups["scope"]:
        raise ValueError(f"{REVIEWER_SLOTS_ENV}: scope needs at least one slot")
    return ReviewerSlotConfig(
        triad=tuple(groups["triad"]),
        scope=tuple(groups["scope"]),
        advisory=_parse_advisory(payload.get("advisory")),
        source="structured",
        deep_review=_parse_deep_review(payload.get("deep_review"), seen_ids),
    )



# removed by package A after package C freezes the lane readers (package C's
# startup notice reads the migration snapshot instead).
def authored_reviewer_slots_state(raw: str) -> Tuple[str, str]:
    """The lane setting's three states as ``(state, parse_error)``: ``absent``
    (shipped default panel), ``authored`` (strict parse accepts), ``invalid``
    (row-precise error; the loader RAISES, so no lane panel serves)."""
    text = str(raw or "").strip()
    if not text:
        return "absent", ""
    try:
        parse_reviewer_slots(text)
    except ValueError as exc:
        return "invalid", str(exc)
    return "authored", ""

# ---------------------------------------------------------------------------
# Shipped default panel — removed by package A after package C freezes the lane
# readers (the pool has no default panel: the migration mints factory rows).
# ---------------------------------------------------------------------------


def _default_config() -> ReviewerSlotConfig:
    """The shipped default lane panel — api_chat rows over the derived env plane
    (`get_review_models` / `get_scope_review_models`); row effort stays '' so
    `row_effort` resolves the surface default; deterministic per-row slot ids."""
    from ouroboros.config import get_review_models, get_scope_review_models
    from ouroboros.review_model_routes import compatible_only_review_model
    from ouroboros.review_substrate import (
        SCOPE_SLOT_ID_PREFIX,
        SLOT_ID_PREFIX,
        slot_id_for_row,
    )

    def _rows(models, prefix, delivery=""):
        return tuple(
            ConfiguredReviewerSlot(
                slot_id=slot_id_for_row(idx + 1, prefix=prefix),
                kind=ROUTE_KIND_API,
                target_id=str(model),
                processing_preference=_row_processing(),
                delivery=delivery,
            )
            for idx, model in enumerate(
                str(m) for m in (models or []) if str(m or "").strip()
            )
        )

    # #1334: the shipped triad reads the work itself on the same models (scope
    # rows always read); a model without tool calling is switched to Packet by
    # the owner, never silently at dispatch.
    return ReviewerSlotConfig(
        triad=_rows(get_review_models(), SLOT_ID_PREFIX, DEFAULT_TRIAD_DELIVERY),
        scope=_rows(get_scope_review_models(), SCOPE_SLOT_ID_PREFIX),
        # An OpenAI-compatible-only install's advisory is Main's route too, shown as such.
        advisory=AdvisorySlotConfig(target_id=compatible_only_review_model(), processing_preference=_row_processing()),
        source="default",
    )


# removed by package A after package C freezes the lane readers (its remaining
# readers are the lane consumers packages B/D/E move to ``review_pool_slots``).
def load_reviewer_slot_config() -> ReviewerSlotConfig:
    """The lane loader: structured when present, the shipped default panel otherwise;
    inside ``composed_review_panel`` the composition that wave was given."""
    composed = _COMPOSED_PANEL.get()
    if composed is not None:
        return composed
    raw = structured_reviewer_slots_raw()
    if raw:
        return parse_reviewer_slots(raw)
    return _default_config()


# ---------------------------------------------------------------------------
# The composed pool: one ``review_change`` wave's seats.
# ---------------------------------------------------------------------------


class PoolSeat(NamedTuple):
    """One seat of a composed wave: the reviewer row as it runs, the parts of the
    brief it judges (``change`` / ``coupling``), and whether the author added it
    beside the configured pool (``additional``: it is heard, not counted)."""

    slot: ReviewSlot
    parts: Tuple[str, ...]
    additional: bool = False


# Context-local: a concurrent wave on another thread keeps the configured pool,
# while the wave's own threads run under ``contextvars.copy_context`` and read
# this composition.
_COMPOSED_POOL: "_contextvars.ContextVar[Optional[Tuple[PoolSeat, ...]]]" = _contextvars.ContextVar(
    "review_composed_pool", default=None)


@_contextlib.contextmanager
def composed_review_pool(seats: Sequence[PoolSeat]):
    """Every pool reader in this block sees exactly these seats (``review_pool_slots``
    returns their rows in this order; ``composed_pool_seats`` their parts)."""
    token = _COMPOSED_POOL.set(tuple(seats))
    try:
        yield
    finally:
        _COMPOSED_POOL.reset(token)


def composed_pool_seats() -> Optional[Tuple[PoolSeat, ...]]:
    """The composition in force, or ``None`` outside a composed wave (the pool
    projection reads a seat's ``parts`` from here; the ledger's ``seat_parts``
    derives them from delivery for the configured pool)."""
    return _COMPOSED_POOL.get()


# removed by package D (``review_change.compose_panel`` → ``composed_review_pool``):
# the PR-2 lane-shaped seam, kept so the author's wave composes until D lands; it
# threads the triad rows into the composed POOL and keeps scope on the lane seam.
_COMPOSED_PANEL: "_contextvars.ContextVar[Optional[ReviewerSlotConfig]]" = _contextvars.ContextVar(
    "review_composed_panel", default=None)


@_contextlib.contextmanager
def composed_review_panel(triad: Sequence[ConfiguredReviewerSlot], scope: Sequence[ConfiguredReviewerSlot]):
    """Every panel reader in this block sees exactly these triad/scope rows; the
    advisory and deep-review rows stay the configured ones."""
    from ouroboros.config import REVIEW_POOL_DEFAULT_EFFORT

    seats = tuple(
        PoolSeat(_delivery_slot(row, effort_surface="review", role_hint="multi-model review",
                                effort_fallback=REVIEW_POOL_DEFAULT_EFFORT),
                 ("change", "coupling") if row.retrieves else ("change",))
        for row in triad
    )
    token = _COMPOSED_PANEL.set(replace(load_reviewer_slot_config(), triad=tuple(triad), scope=tuple(scope)))
    pool_token = _COMPOSED_POOL.set(seats)
    try:
        yield
    finally:
        _COMPOSED_POOL.reset(pool_token)
        _COMPOSED_PANEL.reset(token)


# removed by package D (``catalog_review_row`` names the row by handle or id).
def roster_review_row(slot_id: str, subagent_id: str) -> ConfiguredReviewerSlot:
    """A configured subagent seated as one reviewer row (its roster route and
    effort); an unknown or disabled roster id raises the parser's ValueError."""
    return _resolve_actor_slot(slot_id, subagent_id, "", f"review seat {slot_id!r}")


def row_at_effort_order(row: ConfiguredReviewerSlot, effort: str) -> Optional[ConfiguredReviewerSlot]:
    """The row under a caller's effort order (``row_effort``'s rule): ``None`` for a
    compound Cursor/Agy route, whose encoded effort is the route's identity."""
    return None if _compound_effort(row) else replace(row, effort=effort)


def reviewer_slot_config_error() -> str:
    """The pool's configuration error — the catalog's row-precise parse text — or ''.

    Thin facade for the surfaces that must refuse loudly instead of reviewing
    with no pool at all (plan review, skill review, the settings panel). An
    empty pool is NOT an error here (it is a configured fact every surface
    reports as ``pool_empty``). No caching: the check re-parses so a hot-reloaded
    fix is seen immediately."""
    from ouroboros.configured_subagents import SUBAGENTS_SETTING

    return review_pool_state(_catalog_settings().get(SUBAGENTS_SETTING))["error"]


# ---------------------------------------------------------------------------
# Consumer accessors.
# ---------------------------------------------------------------------------


# removed by package A after package C freezes the lane readers.
def commit_triad_rows() -> List[ConfiguredReviewerSlot]:
    """Configured triad rows shared by commit, plan, and skill review."""
    return list(load_reviewer_slot_config().triad)


# removed by package A after package C freezes the lane readers (package D
# moves the preflight to ``review_change(surface=preflight, reviewers=[one])``).
def advisory_slot_config() -> AdvisorySlotConfig:
    return load_reviewer_slot_config().advisory


# removed by package A after package C freezes the lane readers (package D
# moves the self-review to ``review_change(surface=system, reviewers=[one])``).
def deep_review_slot(config: Optional[ReviewerSlotConfig] = None) -> ConfiguredReviewerSlot:
    """THE deep self-review row: the configured ``deep_review`` lane row, else the
    api row synthesized from ``OUROBOROS_MODEL_DEEP_SELF_REVIEW`` (the row's own
    effort outranks the surface key only when set; a malformed setting raises)."""
    selected = config if config is not None else load_reviewer_slot_config()
    row = selected.deep_review
    return row if row is not None else synthesized_deep_review_slot(authored_panel=selected.source == "structured")


# removed by package A after package C freezes the lane readers.
def synthesized_deep_review_slot(*, authored_panel: bool = False) -> ConfiguredReviewerSlot:
    """The api row the legacy model key stands for — the ONE synthesis rule shared
    by ``deep_review_slot`` and the settings endpoint's repair placeholder."""
    from ouroboros.config import get_deep_self_review_model

    return ConfiguredReviewerSlot(
        slot_id=DEEP_REVIEW_SLOT_ID, kind=ROUTE_KIND_API,
        target_id=get_deep_self_review_model(authored_panel=authored_panel),
        processing_preference=_row_processing(role="deep_review"),
    )


def _delivery_slot(
    row: ConfiguredReviewerSlot, *, effort_surface: str, role_hint: str,
    default_effort: str = "", effort_fallback: str = "", **slot_fields: Any,
) -> Any:
    """ONE configured row as the substrate's ``ReviewSlot``, carrying its own
    delivery: the route kind, the opaque session target and credential pin, the
    catalog binding, and — on an api row — the explicit native/packet fact."""
    from ouroboros.config import resolved_review_model_target
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_substrate import ReviewSlot

    # ABI-4: the local-route fact is read off the typed target constructed at
    # the review seam, not re-derived per model string here.
    return ReviewSlot(
        slot_id=row.slot_id,
        model=row.target_id,
        effort=row_effort(row, effort_surface, default=default_effort, fallback=effort_fallback),
        # "this row runs at the caller's order": every row but a compound route slug.
        declared_effort=default_effort if default_effort and not _compound_effort(row) else "",
        role_hint=role_hint,
        use_local=(row.use_local if row.use_local is not None else resolved_review_model_target(row.target_id).provider_route == "local"),
        route=(ReviewRouteKind.AGENT_SESSION if row.is_session
               else ReviewRouteKind.API_CHAT),
        session_target=row.session_target,
        session_profile=row.profile_id,
        subagent_id=row.subagent_id,
        processing_preference=row.processing_preference,
        # The row's saved delivery is its own fact, stated explicitly in BOTH
        # directions on an api row (F8); a session row always retrieves (None).
        native_retrieval_override=None if row.is_session else bool(row.native_retrieval),
        **slot_fields,
    )


def review_pool_slots(
    snapshot: Any = None,
    *,
    effort_surface: str = "review",
    role_hint: str = "",
    default_effort: str = "",
    **slot_fields: Any,
) -> List[Any]:
    """THE review pool as ``ReviewSlot`` rows — the one builder every review
    surface reads: the commit gate (through ``commit_triad_delivery``'s aligned
    vectors), plan review, skill review, task acceptance and the author's own
    wave. Inside ``composed_review_pool`` it is that wave's seats, in order.

    The pool is the catalog's enabled rows the owner marked review-eligible, in
    catalog order, read from ``snapshot`` (a task's frozen settings) or the
    applied runtime settings — never through the delegation resolver, and never
    gated by the catalog's global switch. Each row rides its own delivery and
    identity (``slot_id == subagent_id``). Effort: a caller's ``default_effort``
    (a wave's order) outranks everything but a compound Cursor/Agy route slug,
    whose encoded effort is the route's identity; with no order it is the row's
    own value, else that compound value, else ``REVIEW_POOL_DEFAULT_EFFORT``.
    ``slot_fields`` are the caller's per-surface ReviewSlot properties (timeout,
    output budget, temperature). A malformed catalog RAISES ValueError — every
    surface turns that into its typed refusal; a valid catalog with no marked
    row is ``[]`` (the surface reports ``pool_empty``, it does not fall back).
    """
    from ouroboros.config import REVIEW_POOL_DEFAULT_EFFORT

    composed = _COMPOSED_POOL.get()
    if composed is not None:
        extra = {**({"role_hint": role_hint} if role_hint else {}), **slot_fields}
        return [replace(seat.slot, **extra) if extra else seat.slot for seat in composed]
    return [
        _delivery_slot(
            row, effort_surface=effort_surface, role_hint=role_hint,
            default_effort=default_effort, effort_fallback=REVIEW_POOL_DEFAULT_EFFORT, **slot_fields,
        )
        for row in _pool_rows(_catalog_settings(snapshot))
    ]


def triad_delivery_slots(
    *,
    role_hint: str = "",
    default_effort: str = "",
    **slot_fields: Any,
) -> List[Any]:
    """The pool under its historical name: an alias of ``review_pool_slots`` for
    the surfaces that still spell the builder this way (plan review, skill
    review, task acceptance, the commit gate's projection)."""
    return review_pool_slots(role_hint=role_hint, default_effort=default_effort, **slot_fields)


def child_acceptance_slots(slots: Sequence[Any], reviewer_slot_id: str = "") -> Tuple[List[Any], Dict[str, Any]]:
    """At most ONE pool row for a child task's acceptance (#1334).

    One reviewer is panel BREADTH, not a reading or round cap: the row keeps its
    own delivery. The only pool row needs no name; otherwise the caller names a
    member — by its stored id (``slot_id == subagent_id``) or its catalog handle
    — and the host checks membership. Returns ``(slots, refusal)``; a refusal is
    a typed ``not_dispatched`` payload, never a review run.
    """
    rows = list(slots or [])
    wanted = str(reviewer_slot_id or "").strip()
    if wanted:
        chosen = [slot for slot in rows if wanted in (slot.slot_id, getattr(slot, "subagent_id", ""))]
        if not chosen:
            chosen = [slot for slot in rows if slot.slot_id == _stored_id_for_selector(wanted)]
        if chosen:
            return chosen, {}
        reason = "reviewer_slot_unknown"
    elif len(rows) <= 1:
        return rows, {}
    else:
        reason = "reviewer_selection_required"
    return [], {
        "status": "not_dispatched", "reason": reason,
        "detail": ("a child task's acceptance uses at most one review-pool row; name one "
                   "with reviewer_slot_id (its id or catalog handle) — no reviewer was called"),
        "reviewer_rows": [{"slot_id": str(getattr(slot, "slot_id", "") or ""),
                           "model": str(getattr(slot, "model", "") or ""),
                           "route": str(getattr(getattr(slot, "route", ""), "value", getattr(slot, "route", "")) or ""),
                           "delivery": ("native" if getattr(slot, "native_retrieval", False)
                                        else "agent_session" if getattr(slot, "retrieves", False) else "packet")}
                          for slot in rows],
    }


# removed by package B (the positional model-list builder of the scope lane).
def reviewer_slots(
    models: List[str] | None = None,
    *,
    effort: str = "medium",
    role_hint: str = "",
    id_prefix: str = "",
) -> List[Any]:
    """The configured reviewer rows, every one pinned ``api_chat``.

    Moved here from ``review_substrate`` for module altitude (P7); the
    substrate re-exports it. Per-row delegated delivery is a structured-SSOT
    fact (``OUROBOROS_REVIEWER_SLOTS`` rows — D14/6.1): the phase-5 per-row
    route envs are RETIRED settings keys (ABI-10) and are ignored here, so a
    row built from a plain model list is an api_chat call explicitly rather
    than by accident (the scope caller that fans out a delegated row overrides
    the route itself). Surfaces that follow the configured triad rows do not
    come here — they use ``triad_delivery_slots``.
    """
    from ouroboros.config import get_review_models, resolved_review_model_target
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_substrate import SLOT_ID_PREFIX, ReviewSlot, slot_id_for_row

    id_prefix = id_prefix or SLOT_ID_PREFIX
    raw_models = models if models is not None else get_review_models()
    named = [str(model) for model in (raw_models or []) if str(model or "").strip()]
    # ABI-4: the local-route fact comes off the typed target constructed at the
    # review seam (one predicate application, at construction) instead of a
    # per-string predicate call here.
    return [
        ReviewSlot(slot_id=slot_id_for_row(idx + 1, prefix=id_prefix), model=model, effort=effort,
                   role_hint=role_hint,
                   use_local=(resolved_review_model_target(model).provider_route == "local"),
                   route=ReviewRouteKind.API_CHAT)
        for idx, model in enumerate(named)
    ]


def commit_triad_delivery() -> Dict[str, Any]:
    """Aligned per-row delivery vectors for the commit triad and skill review.

    Those surfaces consume rows as parallel lists (models for display and slot
    construction, routes for delivery, efforts/session targets/ids as row
    properties); projecting them from ``triad_delivery_slots`` keeps the
    surfaces at their size gates, keeps the vectors impossible to misalign,
    and keeps ONE reader of the triad rows. Raises ValueError on a malformed
    configuration — the caller turns that into its typed infra block.
    """
    from ouroboros.review_records import apply_review_model_override
    from ouroboros.model_wait import current_model_wait

    slots = triad_delivery_slots(role_hint="multi-model review")
    waiter = current_model_wait()
    slots = [apply_review_model_override(slot, waiter.overrides) for slot in slots] if waiter else slots
    return {
        "models": [slot.model for slot in slots],
        "routes": [slot.route for slot in slots],
        "efforts": [slot.effort for slot in slots],
        "session_targets": [slot.session_target for slot in slots],
        "session_profiles": [slot.session_profile for slot in slots],
        "slot_ids": [slot.slot_id for slot in slots],
        "subagent_ids": [slot.subagent_id for slot in slots],
        # Per-row delivery class (#1334, F8): the row's own explicit fact; no
        # consumer may infer it from the catalog id every pool row carries.
        "retrieves": [bool(slot.retrieves) for slot in slots],
        "use_local": [slot.use_local for slot in slots],
        # Package B adds the aligned ``parts`` vector here (one row): a composed
        # seat's parts from ``composed_pool_seats()``, else ``seat_parts(slot)``.
        # The pool is always a configured panel (the migration mints the factory
        # rows into the catalog), so the pre-structured all-packet identity of
        # the skill-review fingerprint never applies to it.
        "legacy_skill_fingerprint": False,
    }


def row_plan_retrieves(row_plan: Dict[str, Any], index: int) -> bool:
    """Read the aligned delivery vector; a row the vector does not cover is its
    route's own class with no native retrieval (a session retrieves, an api row
    receives the packet) — never inferred from an actor id (F8)."""
    flags = list(row_plan.get("retrieves") or [])
    if index < len(flags):
        return bool(flags[index])
    from ouroboros.review_execution import delivery_retrieves

    routes = list(row_plan.get("routes") or [])
    return index < len(routes) and delivery_retrieves(routes[index], False)


def _compound_effort(row: ConfiguredReviewerSlot) -> str:
    """A Cursor/Agy compound route slug's encoded effort, '' for every other row.
    That effort is the route's model identity: sending ``model=…-xhigh`` with
    ``effort=low`` is the contradiction ``validate_compound_session_effort``
    already refuses at save time, so no caller's order may override it."""
    if row.is_session:
        return compound_session_effort(RouteSpec(
            kind=SHARED_ROUTE_KIND_SESSION,
            target_id=row.session_target or row.target_id,
            credential_profile_id=row.profile_id,
        )) or ""
    return ""


def _row_own_effort(row: ConfiguredReviewerSlot) -> str:
    """The effort the ROW itself carries: its explicit field, else a Cursor/Agy
    compound slug's encoded effort; '' when the row leaves it to its caller."""
    return row.effort or _compound_effort(row)


def row_effort(
    row: ConfiguredReviewerSlot,
    surface: str,
    *,
    default: str = "",
    fallback: str = "",
) -> str:
    """Resolve one effort authority without contradicting a compound route.

    A caller's ``default`` is an ORDER for this run (a plan envelope's
    ``reviewer_effort``): it outranks the owner's per-row pin on every row except
    a Cursor/Agy compound slug, whose encoded effort is the route's identity and
    stays. Only plan review passes an order; commit, scope, skill, acceptance and
    deep review call without one, and for them an explicit row field wins, then
    a compound slug's encoded effort, then ``fallback`` — the pool's
    ``REVIEW_POOL_DEFAULT_EFFORT`` — else the surface setting (lane rows only).
    """
    if default and not _compound_effort(row):
        return default
    own = _row_own_effort(row)
    if own:
        return own
    if fallback:
        return fallback
    from ouroboros.config import resolve_effort

    return resolve_effort(surface)


# ---------------------------------------------------------------------------
# Save-time validation and the legacy comma-key projection.
# ---------------------------------------------------------------------------


# Measured acceptance-panel cost on the API packet delivery (plan §4.1, traces of
# 2026-09-01): the ONE-TIME migration disclosure quotes them (owner R12) so an
# owner whose triad now retrieves knows what each substantive task's acceptance
# panel used to cost and what it spends instead. History, not a price table.
_ACCEPTANCE_API_PANEL_MEASURED = (
    "measured on the API packet panel it was ≈12 s and ≈$0.07 per model row per task "
    "(median, OSWorld traces; a three-row panel ≈75 s / ≈$0.82 on ProgramBench; "
    "7.5–8.9% of a run's cost)"
)


# removed by package A after package C freezes the lane readers.
def acceptance_delivery_disclosure(rows: Sequence[ConfiguredReviewerSlot]) -> str:
    """The one-time R12 disclosure for a triad that (newly) retrieves: which rows,
    and what every substantive task's acceptance panel spends on them."""
    named = ", ".join(
        f"{row.slot_id} ({'agent session ' + row.session_target if row.is_session else 'native inspection'}"
        f"{' via ' + row.subagent_id if row.subagent_id else ''} → {row.target_id})"
        for row in rows
    )
    return (
        f"Task acceptance now follows these triad rows, including the retrieving ones — {named}. "
        f"Every substantive task's acceptance panel runs on them from the next task: {_ACCEPTANCE_API_PANEL_MEASURED}. "
        "A native inspection row spends API money as rounds × one send; an agent-session row spends "
        "minutes of your subscription window per task instead. A triad that also carries an "
        "api_chat row keeps a packet panel beside them."
    )


# removed by package A after package C freezes the lane readers (package E
# replaces the lane save check with ``review_pool_save_error`` in the gateway).
def reviewer_slot_save_check(
    raw: str, *, subagents_raw: Optional[str] = None, previous_raw: Optional[str] = None,
) -> str:
    """Validate an incoming structured value; return the save-time disclosure.

    Raises ValueError (row-precise) on a malformed value so the save handler
    turns it into a 400. ``subagents_raw`` threads the roster the SAME save
    produces (S4 atomicity) through a context-local override — actor
    references validate against it without any process-env mutation.

    The disclosure is the ONE-TIME migration notice of owner R12: returned when
    the saved triad has a retrieving row (agent session or configured-subagent
    native inspection) and the previously stored value had none — a legacy
    comma-key config, a packet-only triad, an unknown/malformed previous value.
    A save that keeps an already-retrieving triad discloses nothing again. The
    former all-delegated API-fallback warning described a task-acceptance
    substitution that no longer exists (acceptance follows the rows, R2)."""
    with roster_env_override(subagents_raw) if subagents_raw is not None else _contextlib.nullcontext():
        retrieving = [row for row in parse_reviewer_slots(raw).triad if row.retrieves]
        if not retrieving:
            return ""
        try:
            # No stored value ran the shipped default panel, whose triad already
            # reads natively (#1334; the startup notice disclosed it).
            previous = parse_reviewer_slots(previous_raw) if previous_raw else _default_config()
            if any(row.retrieves for row in previous.triad):
                return ""  # already disclosed when that value was saved
        except ValueError:
            pass  # a malformed previous value never ran a retrieving panel: disclose
    return acceptance_delivery_disclosure(retrieving)


@_contextlib.contextmanager
def roster_env_override(subagents_raw: str, *, environ=None):
    """Parse reviewer rows against THIS roster instead of the process env —
    the save handler's incoming roster, or a benchmark container's one-model
    roster — without mutating the environment concurrent dispatch observes."""
    overlay = dict(runtime_environ() if environ is None else environ)
    overlay["OUROBOROS_SUBAGENTS"] = str(subagents_raw)
    token = _ROSTER_ENV_OVERRIDE.set(overlay)
    try:
        yield
    finally:
        _ROSTER_ENV_OVERRIDE.reset(token)


# removed by package A after package C freezes the lane readers (its one caller
# is ``config.apply_settings_to_env``; the benchmarks no longer need the comma
# projection — they pin N identical catalog rows).
def project_reviewer_slots_into_env(*, environ=None) -> None:
    """Project the structured lane config into the legacy comma keys at env-apply
    time (api rows only; a runtime derivation, never a second write), and own the
    historical default-if-empty floor of both keys. No review surface reads them;
    a malformed structured value is logged and left unprojected (startup must not
    die here — the surfaces re-parse strictly and block with the precise error)."""
    from ouroboros.settings_defaults import OPENROUTER_REVIEW_DEFAULTS

    environ = os.environ if environ is None else environ
    raw = str(environ.get(REVIEWER_SLOTS_ENV, "") or "").strip()
    if raw:
        try:
            with roster_env_override(str(environ.get("OUROBOROS_SUBAGENTS", "")), environ=environ):
                config = parse_reviewer_slots(raw)
        except ValueError:
            import logging

            logging.getLogger(__name__).error(
                "%s is malformed; legacy env keys left unprojected — review "
                "surfaces will block with the precise parse error",
                REVIEWER_SLOTS_ENV, exc_info=True,
            )
        else:
            api_triad = [r.target_id for r in config.triad if not r.is_session]
            api_scope = [r.target_id for r in config.scope if not r.is_session]
            if api_triad:
                environ["OUROBOROS_REVIEW_MODELS"] = ",".join(api_triad)
            else:
                environ.pop("OUROBOROS_REVIEW_MODELS", None)
            if api_scope:
                environ["OUROBOROS_SCOPE_REVIEW_MODELS"] = ",".join(api_scope)
            else:
                environ.pop("OUROBOROS_SCOPE_REVIEW_MODELS", None)
                environ.pop("OUROBOROS_SCOPE_REVIEW_MODEL", None)
    if not environ.get("OUROBOROS_REVIEW_MODELS"):
        environ["OUROBOROS_REVIEW_MODELS"] = ",".join(OPENROUTER_REVIEW_DEFAULTS["triad"])
    if not environ.get("OUROBOROS_SCOPE_REVIEW_MODELS") and not environ.get("OUROBOROS_SCOPE_REVIEW_MODEL"):
        environ["OUROBOROS_SCOPE_REVIEW_MODELS"] = ",".join(OPENROUTER_REVIEW_DEFAULTS["scope"])


# ---------------------------------------------------------------------------
# «Выполняется как» (D22): the last EFFECTIVE execution per seat.
#
# The UI projection of capability_delta — beside each SAVED catalog row, what the
# row REALLY ran as last time (route, model, effort, verdict method, any deltas),
# keyed by ``slot_id`` (== the row's ``subagent_id``). Disclosure, never
# enforcement: nothing reads this back into routing.
# ---------------------------------------------------------------------------

LAST_EXECUTION_FILENAME = "reviewer_slot_last_execution.json"
_LAST_EXECUTION_CAP = 64  # the pool is ≤ MAX_CONFIGURED_SUBAGENTS (26) rows; the cap only bounds junk growth


def _last_execution_path() -> "pathlib.Path":
    import pathlib

    from ouroboros.config import DATA_DIR

    return pathlib.Path(DATA_DIR) / "state" / LAST_EXECUTION_FILENAME


# `run_parallel_review` runs the triad and the scope surfaces CONCURRENTLY, in two
# threads of one process, and each finishes by folding its own rows into this one
# file. `write_text_atomic` makes the write untearable but cannot make the
# read-modify-write around it atomic: both threads read the same "before", and the
# slower one wrote its rows over the faster one's. The surface that vanished was
# whichever finished first — so the panel silently lost a whole row's «Выполняется
# как» line. In-process lock only: the concurrency is threads, not processes.
_LAST_EXECUTION_LOCK = threading.Lock()


def reviewer_slot_execution_rows(surface: str, actors: Any, slots_by_id: Dict[str, Any], *,
                                 record_id: str = "") -> Dict[str, Dict[str, Any]]:
    """One last-execution row per settled actor, keyed by slot id (pure: nothing written).

    The wave that ran these actors keeps the returned rows as ITS facts; the shared
    projection written by :func:`record_reviewer_slot_executions` may be overwritten by
    another surface's later run of the same seat."""
    from ouroboros.review_substrate import TYPED_FAILURE_FACT_KEYS
    from ouroboros.utils import utc_now_iso

    rows: Dict[str, Dict[str, Any]] = {}
    for actor in actors or []:
        slot = slots_by_id.get(getattr(actor, "slot_id", ""))
        if slot is None:
            continue
        if str(getattr(actor, "operation_state", "") or "") == "pending_dispatch":
            continue  # released at the dispatch barrier: still running, recorded when it settles
        usage = dict(getattr(actor, "usage", {}) or {})
        route_kind = str(getattr(getattr(slot, "route", None), "value", "") or "api_chat")
        delegated_route = str(usage.get("delegated_route") or "")
        session = route_kind == "agent_session" or bool(delegated_route)
        effective: Dict[str, Any] = {
            # For a session the harness resolves route/model on its side; for
            # api_chat what was sent is what ran.
            "route": (f"agent_session:{delegated_route}" if delegated_route
                      else route_kind),
            # APPLIED honesty: a session whose telemetry disclosed no resolved
            # model shows ABSENCE — the requested model must never be dressed
            # up as the applied one. An api row's sent model IS its applied one.
            "model": (str(usage.get("resolved_model") or "") if session
                      else str(getattr(slot, "model", "") or "")),
            # No scalar "effort": keep host-send evidence and the engine's
            # sourced report separate from the requested row below.
            "verdict_method": str(usage.get("verdict_method") or ""),
        }
        if isinstance(usage.get("processing"), dict):
            effective["processing"] = dict(usage["processing"])
        if isinstance(usage.get("effort_resolution"), dict):
            effective["effort_resolution"] = dict(usage["effort_resolution"])
        # D29 applied account/access, verbatim from the engine receipt; absent
        # keys mean the telemetry predates the receipt — shown as absence.
        if usage.get("applied_profile"):
            effective["profile_id"] = str(usage["applied_profile"])
        if usage.get("applied_access"):
            effective["access"] = str(usage["applied_access"])
        row: Dict[str, Any] = {
            "ts": utc_now_iso(),
            "surface": str(surface or ""),
            "requested": {
                "route_kind": route_kind,
                "model": str(getattr(slot, "model", "") or ""),
                # The ROW's effort. A caller-declared one-off (plan review's
                # reviewer_effort) is disclosed separately, never shown as the
                # row's saved configuration.
                "effort": "" if getattr(slot, "declared_effort", "") else str(getattr(slot, "effort", "") or ""),
                **({"declared_effort": str(slot.declared_effort)} if getattr(slot, "declared_effort", "") else {}),
                "session_target": str(getattr(slot, "session_target", "") or ""),
                "profile_id": str(getattr(slot, "session_profile", "") or ""),
                # Actor binding, when the row is a configured-subagent
                # reference ('' = direct row) — disclosure, never routing.
                "subagent_id": str(getattr(slot, "subagent_id", "") or ""),
                "processing_preference": str(getattr(slot, "processing_preference", "") or ""),
            },
            "effective": effective,
            **({"effort": dict(usage["effort"])} if isinstance(usage.get("effort"), dict) else {}),
            "capability_delta": usage.get("capability_delta") or [],
            "status": str(getattr(actor, "status", "") or ""),
            **({"review_record_id": str(record_id)} if record_id else {}),
        }
        # B1: typed failure facts, present only when the substrate carried them
        # (a later health surface reads them; absence stays honest absence).
        # ONE shared key list with the plan-row/wave projections (sources differ).
        for key in TYPED_FAILURE_FACT_KEYS:
            value = getattr(actor, key, None)
            if value:
                row[key] = value
        rows[str(actor.slot_id)] = row
    return rows


def record_reviewer_slot_executions(surface: str, actors: Any, slots_by_id: Dict[str, Any], *,
                                    record_id: str = "", keep_on: Any = None) -> Dict[str, Dict[str, Any]]:
    """Record each actor's last effective execution (best-effort, atomic) and return
    the rows this call wrote (:func:`reviewer_slot_execution_rows`).

    Written under the process data root (``config.DATA_DIR``), never a
    ToolContext review drive: UI state beside the saved settings, not per-task
    forensics — those live in the durable actor records already. An isolated
    contributor review's data root IS its review drive, so its markers stay there.
    ``record_id`` names the review ledger record the execution belongs to when the
    caller already holds it; a surface that learns the id only after its wave
    settled binds it afterwards with ``bind_reviewer_slot_record_id``. ``keep_on`` (the
    wave's ctx) receives the same rows as ``_last_review_slot_executions`` for the wave's
    ledger record, merged under the same lock: the triad and scope halves of one wave
    record concurrently, and a read-then-replace outside the lock would drop one half.
    """
    from ouroboros.utils import write_text_atomic

    rows = reviewer_slot_execution_rows(surface, actors, slots_by_id, record_id=record_id)
    path = _last_execution_path()
    with _LAST_EXECUTION_LOCK:
        if keep_on is not None and rows:
            kept = getattr(keep_on, "_last_review_slot_executions", None)
            setattr(keep_on, "_last_review_slot_executions", {**(kept if isinstance(kept, dict) else {}), **rows})
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data.update(rows)
        if len(data) > _LAST_EXECUTION_CAP:
            ordered = sorted(data.items(), key=lambda kv: str(kv[1].get("ts") or ""))
            data = dict(ordered[-_LAST_EXECUTION_CAP:])
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=1))
    return rows


def bind_reviewer_slot_record_id(executions: Any, record_id: str) -> None:
    """Name the review ledger record on the projection rows a settled wave itself wrote
    (the gate learns the id after its seats recorded themselves). ``executions`` are
    that wave's own rows (:func:`reviewer_slot_execution_rows`): a projection row is
    bound only when its timestamp is the wave's — a later run of the same seat by
    another surface keeps its own id. Best-effort, atomic."""
    from ouroboros.utils import write_text_atomic

    record_id = str(record_id or "")
    if not record_id or not isinstance(executions, dict):
        return
    path = _last_execution_path()
    with _LAST_EXECUTION_LOCK:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        changed = False
        for slot_id, own in executions.items():
            row = data.get(str(slot_id))
            own_ts = str(own.get("ts") or "") if isinstance(own, dict) else ""
            if isinstance(row, dict) and own_ts and str(row.get("ts") or "") == own_ts:
                row["review_record_id"] = record_id
                changed = True
        if changed:
            write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=1))


def reviewer_slot_last_executions() -> Dict[str, Any]:
    """Read the projection ('' shape on any read problem — disclosure only)."""
    try:
        data = json.loads(_last_execution_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


__all__ = [
    # The pool.
    "ConfiguredReviewerSlot",
    "PoolSeat",
    "ROUTE_KIND_API",
    "ROUTE_KIND_SESSION",
    "catalog_review_row",
    "child_acceptance_slots",
    "commit_triad_delivery",
    "composed_pool_seats",
    "composed_review_pool",
    "review_pool_rows",
    "review_pool_save_error",
    "review_pool_slots",
    "review_pool_state",
    "reviewer_slot_config_error",
    "roster_env_override",
    "row_at_effort_order",
    "row_effort",
    "row_plan_retrieves",
    "triad_delivery_slots",
    # «Выполняется как».
    "bind_reviewer_slot_record_id",
    "record_reviewer_slot_executions", "reviewer_slot_execution_rows",
    "reviewer_slot_last_executions",
    # removed by package A after package C freezes the lane readers (and by
    # packages B/D/E for their lane consumers): the retired lane surface.
    "DEEP_REVIEW_SLOT_ID",
    "REVIEWER_SLOTS_ENV",
    "SCOPE_SLOT_LIMIT",
    "TRIAD_SLOT_LIMIT",
    "AdvisorySlotConfig",
    "ReviewerSlotConfig",
    "advisory_slot_config",
    "commit_triad_rows",
    "deep_review_slot",
    "synthesized_deep_review_slot",
    "load_reviewer_slot_config",
    "parse_reviewer_slots",
    "authored_reviewer_slots_state",
    "project_reviewer_slots_into_env",
    "acceptance_delivery_disclosure",
    "reviewer_slot_save_check",
    "structured_reviewer_slots_present",
    "structured_reviewer_slots_raw",
]
