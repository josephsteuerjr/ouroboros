"""Stand-ins for the review-pool contract of packages A and B (PR-3 seam).

``review_change.compose_panel`` is written against package A's pool
(``reviewer_slot_config.review_pool_slots``, ``catalog_review_row``,
``composed_review_pool``, ``PoolSeat``) and package B's
``review_ledger.seat_parts``. Until they land, ``install`` sets ONLY the
attributes the modules lack, so after integration the real symbols win and
this module is inert. The stand-ins model the PR-2 panel: the pool is the
configured triad rows (change) followed by the configured scope rows
(coupling); a named catalog row is seated under its own id; a composed pool
maps back onto ``composed_review_panel``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, NamedTuple, Sequence, Tuple

from ouroboros import review_ledger
from ouroboros import reviewer_slot_config as slots


class PoolSeat(NamedTuple):
    slot: Any
    parts: Tuple[str, ...]
    additional: bool = False


_CATALOG: Dict[str, Any] = {}


def review_pool_slots(snapshot: Any = None, *, effort_surface: str = "review", role_hint: str = "",
                      default_effort: str = "", **slot_fields: Any) -> list:
    config = slots.load_reviewer_slot_config()
    pool = [slots._delivery_slot(row, effort_surface=effort_surface, role_hint=role_hint,
                                 default_effort=default_effort, **slot_fields) for row in config.triad]
    seated = {slot.slot_id for slot in pool}
    pool += [slots._delivery_slot(row, effort_surface="scope_review", role_hint=slots.SCOPE_ROLE_HINT,
                                  default_effort=default_effort, **slot_fields)
             for row in config.scope if row.slot_id not in seated]
    return pool


def catalog_review_row(snapshot: Any, selector: str) -> Any:
    row = slots.roster_review_row(selector, selector)
    row = replace(row, slot_id=row.subagent_id)
    _CATALOG[row.slot_id] = row
    return row


def seat_parts(slot: Any, *, coupling_only: bool = False) -> Tuple[str, ...]:
    if coupling_only or getattr(slot, "role_hint", "") == slots.SCOPE_ROLE_HINT:
        return (review_ledger.PART_COUPLING,)
    return (review_ledger.PART_CHANGE,)


def composed_review_pool(seats: Sequence[Any]) -> Any:
    config = slots.load_reviewer_slot_config()
    triad_rows = {row.slot_id: row for row in config.triad}
    scope_rows = {row.slot_id: row for row in config.scope}
    triad, scope = [], []
    for slot, parts, _additional in seats:
        for part, rows, target in ((review_ledger.PART_CHANGE, triad_rows, triad),
                                   (review_ledger.PART_COUPLING, scope_rows, scope)):
            if part not in parts:
                continue
            row = rows.get(slot.slot_id) or triad_rows.get(slot.slot_id) or scope_rows.get(slot.slot_id) \
                or _CATALOG[slot.slot_id]
            if slot.declared_effort:
                row = slots.row_at_effort_order(row, slot.declared_effort) or row
            target.append(row)
    return slots.composed_review_panel(triad, scope)


def install(monkeypatch: Any) -> None:
    _CATALOG.clear()
    for module, name, value in ((slots, "PoolSeat", PoolSeat), (slots, "review_pool_slots", review_pool_slots),
                                (slots, "catalog_review_row", catalog_review_row),
                                (slots, "composed_review_pool", composed_review_pool),
                                (review_ledger, "seat_parts", seat_parts)):
        if not hasattr(module, name):
            monkeypatch.setattr(module, name, value, raising=False)
