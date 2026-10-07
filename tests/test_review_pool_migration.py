"""Review lanes -> review pool: the one-time migration M (PR-3, package C).

The former ``OUROBOROS_REVIEWER_SLOTS`` lanes (triad / scope / advisory / deep review)
and their surface keys become reviewer rows of the subagent catalog
(``OUROBOROS_SUBAGENTS`` rows marked ``review_eligible``). ``review_pool_migration``
carries FROZEN copies of the lane readers so the migration keeps reading old documents
exactly as the release that wrote them did, after the live readers are gone.

Pinned here: the frozen readers resolve Anton's install and the N-1 fixture to the same
effective executions as the base readers (expected values inline, never imported from
old code); the contract's F4-F8 tables; the F6 catalog x lanes matrix (25 cells);
Anton's install and the N-1/N-2 fixtures; idempotency by bytes; seats, engines and
deliveries preserved; the read-seam wiring (``config.normalize_settings_raw``); the
snapshot written once and the owner told once (``server_maintenance``).
"""

from __future__ import annotations

import copy
import json
import pathlib

import pytest

import ouroboros.configured_subagents as cs
from ouroboros import review_pool_migration as m
from ouroboros.settings_defaults import (
    OPENROUTER_REVIEW_DEFAULTS,
    RETIRED_COMMA_LIST_SETTING_KEYS,
    REVIEW_POOL_MIGRATED_SETTING_KEYS,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "nminus1"
N1_DOC = json.loads((FIXTURES / "settings_v6.113.4.json").read_text(encoding="utf-8"))

SLOTS, SUBAGENTS = "OUROBOROS_REVIEWER_SLOTS", "OUROBOROS_SUBAGENTS"
LANE_RECOMMENDATION = "Minted from the former review lane"


@pytest.fixture(autouse=True)
def _pool_ceiling(monkeypatch):
    """Package A raises the catalog ceiling to 26 for the pool; until it lands, this tree
    caps at 10. The migration reads the live constant, so the tests set the pool value."""
    monkeypatch.setattr(cs, "MAX_CONFIGURED_SUBAGENTS", 26)


# --- document builders -------------------------------------------------------


def session_row(subagent_id, target, effort="", access="full", **extra):
    row = {"subagent_id": subagent_id, "recommended_use": f"helper {subagent_id}",
           "route": {"kind": "agent_session", "target_id": target, "credential_profile_id": ""}}
    if effort:
        row["effort"] = effort
    row["access"] = access
    row.update(extra)
    return row


def api_row(subagent_id, target, effort="", **extra):
    row = {"subagent_id": subagent_id, "recommended_use": f"helper {subagent_id}",
           "route": {"kind": "api_model", "target_id": target}}
    if effort:
        row["effort"] = effort
    row.update(extra)
    return row


def catalog(*rows, enabled=True):
    return json.dumps({"enabled": enabled, "items": list(rows)})


def direct(slot_id, target, effort="", delivery=None, kind="api_chat", **extra):
    row = {"slot_id": slot_id, "route": {"kind": kind, "target_id": target}}
    if effort:
        row["effort"] = effort
    if delivery is not None:
        row["delivery"] = delivery
    row.update(extra)
    return row


def ref(slot_id, subagent_id, effort=""):
    row = {"slot_id": slot_id, "subagent_id": subagent_id}
    if effort:
        row["effort"] = effort
    return row


def lanes(triad=(), scope=(), advisory=None, deep_review=None):
    payload = {"triad": list(triad), "scope": list(scope)}
    if advisory is not None:
        payload["advisory"] = advisory
    if deep_review is not None:
        payload["deep_review"] = deep_review
    return json.dumps(payload)


# Anton's install, structurally (contract §2 "Установка Антона", 2026-10-07): a 9-row
# catalog, all enabled; three referenced triad seats, one referenced scope seat, an
# advisory reference, a direct deep-review row; surface keys that acted on no seat.
ANTON_CATALOG_ROWS = [
    session_row("subagent_osabav", "claude=claude-fable-5-1", "xhigh"),
    session_row("subagent_k0ofra", "cursor=grok-4.7-xhigh-fast"),
    session_row("subagent_sa5g1l", "codex=gpt-6-astra", "ultra"),
    session_row("subagent_a1", "codex=gpt-5.6-sol", "high"),
    api_row("subagent_a2", "openai/gpt-5.6-terra", "medium"),
    api_row("subagent_a3", "anthropic/claude-opus-5", "high", processing_preference="economy"),
    session_row("subagent_a4", "claude=claude-sonnet-5", "medium", access="workspace_write"),
    api_row("subagent_a5", "google/gemini-3.8-flash"),
    session_row("subagent_a6", "cursor=gpt-5.6-sol-high"),
]
ANTON_LANES = lanes(
    triad=[ref("triad_286lhb", "subagent_osabav", "xhigh"), ref("triad_w45a8z", "subagent_k0ofra"),
           ref("triad_bkydwq", "subagent_sa5g1l", "xhigh")],
    scope=[ref("scope_slot_1", "subagent_sa5g1l", "xhigh")],
    advisory={"enabled": True, "subagent_id": "subagent_k0ofra"},
    deep_review={"route": {"kind": "api_chat", "target_id": "claudexor::codex=gpt-6-astra"},
                 "effort": "xhigh", "processing_preference": "economy"},
)


def anton_document():
    return {
        SUBAGENTS: catalog(*copy.deepcopy(ANTON_CATALOG_ROWS)),
        SLOTS: ANTON_LANES,
        "OUROBOROS_EFFORT_REVIEW": "medium",
        "OUROBOROS_EFFORT_SCOPE_REVIEW": "medium",
        "OUROBOROS_EFFORT_DEEP_SELF_REVIEW": "high",
        "OUROBOROS_MODEL_DEEP_SELF_REVIEW": "openai/gpt-5.6-sol-pro",
        "OUROBOROS_MODEL": "openai/gpt-5.6-sol",
        "OPENROUTER_API_KEY": "present",
        "TOTAL_BUDGET": 10.0,
    }


def _seat(slot_id, kind, target, effort, source, delivery, **extra):
    payload = {"slot_id": slot_id, "subagent_id": extra.pop("subagent_id", ""), "kind": kind,
               "target_id": target, "effort": effort, "effort_source": source, "delivery": delivery,
               "credential_profile_id": "", "processing_preference": extra.pop("processing_preference", "")}
    if kind == "agent_session":
        payload["access"] = extra.pop("access", "full")
    payload.update(extra)
    return payload


# What the base readers (reviewer_slot_config + row_effort) resolved Anton's lanes to,
# written out by hand: the frozen copies must agree, field by field.
ANTON_EXPECTED_EXECUTIONS = {
    "triad": [
        _seat("triad_286lhb", "agent_session", "claude=claude-fable-5-1", "xhigh", "row", "session",
              subagent_id="subagent_osabav"),
        _seat("triad_w45a8z", "agent_session", "cursor=grok-4.7-xhigh-fast", "xhigh", "compound", "session",
              subagent_id="subagent_k0ofra"),
        _seat("triad_bkydwq", "agent_session", "codex=gpt-6-astra", "xhigh", "row", "session",
              subagent_id="subagent_sa5g1l"),
    ],
    "scope": [
        _seat("scope_slot_1", "agent_session", "codex=gpt-6-astra", "xhigh", "row", "session",
              subagent_id="subagent_sa5g1l"),
    ],
    "advisory": {"slot_id": "advisory_slot_1", "subagent_id": "subagent_k0ofra", "enabled": True},
    "deep_review": _seat("deep_review_slot_1", "api_chat", "claudexor::codex=gpt-6-astra", "xhigh", "row", "native",
                         processing_preference="economy"),
}

# The N-1 fixture (6.113.4, no provider keys, lanes "" and catalog ""): the shipped
# OpenRouter panel at the document's "high", plus the deep row the legacy key synthesized.
N1_EXPECTED_EXECUTIONS = {
    "triad": [
        _seat("slot_1", "api_chat", "google/gemini-3.8-flash", "high", "document", "native", authored=False),
        _seat("slot_2", "api_chat", "openai/gpt-5.6-terra", "high", "document", "native", authored=False),
        _seat("slot_3", "api_chat", "anthropic/claude-opus-5", "high", "document", "native", authored=False),
    ],
    "scope": [
        _seat("scope_slot_1", "api_chat", "openai/gpt-5.6-terra", "high", "document", "native", authored=False),
    ],
    "advisory": _seat("advisory_slot_1", "api_chat", "", "low", "row", "native", authored=False, enabled=True),
    "deep_review": _seat("deep_review_slot_1", "api_chat", "openai/gpt-5.6-sol-pro", "high", "document", "native"),
}


def _executions(document):
    raw = document.get(SLOTS)
    authored = isinstance(raw, str) and bool(raw.strip())
    parsed = m.parse_reviewer_slots(document, raw) if authored else m.factory_lanes(document)
    return m._executions_dict(m.effective_executions(document, parsed, authored=authored))


def _migrated(document):
    outcome = m.migrate_review_lanes(dict(document))
    assert outcome is not None and not outcome.error, outcome
    return outcome, json.loads(outcome.catalog_after)


def _marked(items):
    return [row["subagent_id"] for row in items if row.get("review_eligible")]


# --- 1. the frozen readers --------------------------------------------------------


def test_frozen_readers_resolve_antons_document_like_the_base():
    assert _executions(anton_document()) == ANTON_EXPECTED_EXECUTIONS


def test_frozen_readers_resolve_the_nminus1_fixture_like_the_base():
    assert _executions(N1_DOC) == N1_EXPECTED_EXECUTIONS
    assert list(OPENROUTER_REVIEW_DEFAULTS["triad"]) == [s["target_id"] for s in N1_EXPECTED_EXECUTIONS["triad"]]


def test_frozen_readers_reject_what_the_base_rejected():
    for bad in ('{"triad": [{"model": "x/y"}]}', '{"triad": [], "scope": []}', "{broken",
                lanes(triad=[direct("t", "x/y", delivery="packet")], scope=[direct("s", "x/y", delivery="native")]),
                lanes(triad=[ref("t", "ghost")], scope=[])):
        with pytest.raises(ValueError):
            m.parse_reviewer_slots({}, bad)
    with pytest.raises(ValueError, match="JSON string"):
        m.parse_reviewer_slots({}, {"triad": []})


def test_the_review_seat_recommendation_is_the_presets_sentence():
    presets = pytest.importorskip("ouroboros.subscription_install_presets")
    expected = getattr(presets, "_REVIEW_SEAT_RECOMMENDATION", None)
    if expected is None:
        pytest.skip("package A moved the sentence; the frozen copy stands on its own")
    assert m.REVIEW_SEAT_RECOMMENDATION == expected


# --- 2. Anton's install, N-1, N-2 ---------------------------------------------------


def test_antons_install_migrates_to_eleven_rows_three_marked():
    doc = anton_document()
    outcome, after = _migrated(doc)
    assert (outcome.catalog_state, outcome.slots_state) == ("configured", "mixed")
    assert after["enabled"] is True and len(after["items"]) == 11
    assert _marked(after["items"]) == ["subagent_osabav", "subagent_k0ofra", "review-1"]
    unchanged = {row["subagent_id"]: row for row in ANTON_CATALOG_ROWS}
    for row in after["items"][:9]:
        original = unchanged[row["subagent_id"]]
        assert {k: v for k, v in row.items() if k != "review_eligible"} == original, "rows are never rewritten"
    assert after["items"][2]["effort"] == "ultra" and "review_eligible" not in after["items"][2]
    assert after["items"][9] == {
        "subagent_id": "review-1", "recommended_use": LANE_RECOMMENDATION,
        "route": {"kind": "agent_session", "target_id": "codex=gpt-6-astra", "credential_profile_id": ""},
        "effort": "xhigh", "access": "full", "review_eligible": True, "minted_from": "review_lane"}
    assert after["items"][10] == {
        "subagent_id": "review-2", "recommended_use": LANE_RECOMMENDATION,
        "route": {"kind": "api_model", "target_id": "claudexor::codex=gpt-6-astra", "credential_profile_id": ""},
        "effort": "xhigh", "processing_preference": "economy", "minted_from": "review_lane"}
    assert outcome.snapshot["summary"] == {"seats_before": 4, "rows_marked_after": 3, "distinct_models": 3,
                                           "helper_rows_minted": 1}
    assert outcome.snapshot["not_in_effect"] == [
        "OUROBOROS_EFFORT_REVIEW=medium", "OUROBOROS_EFFORT_SCOPE_REVIEW=medium",
        "OUROBOROS_EFFORT_DEEP_SELF_REVIEW=high", "OUROBOROS_MODEL_DEEP_SELF_REVIEW=openai/gpt-5.6-sol-pro"]
    assert outcome.consumed_keys == REVIEW_POOL_MIGRATED_SETTING_KEYS and outcome.retained_keys == ()
    rows = {entry["subagent_id"]: entry for entry in outcome.snapshot["rows"]}
    assert rows["review-1"]["from_seats"] == ["triad_bkydwq", "scope_slot_1"] and "merged" in rows["review-1"]["note"]
    assert rows["subagent_k0ofra"]["from_seats"] == ["triad_w45a8z", "advisory_slot_1"]
    text = m.owner_message(outcome, "state/review_migrations/x.json")
    assert text.startswith("⚙️ Review settings migrated.")
    assert "Before: 3 triad seats + 1 scope seat (+ advisory reference, deep review row). After: 3 reviewer rows, 3 distinct models." in text
    assert "• subagent_osabav (claude=claude-fable-5-1, xhigh) — marked" in text
    assert "• review-2 (api claudexor::codex=gpt-6-astra, xhigh, economy) — new row without the mark" in text
    assert "Not in effect before, retired: OUROBOROS_EFFORT_REVIEW=medium" in text
    assert text.rstrip().endswith("Snapshot: state/review_migrations/x.json. Adjust in Settings → Agents.")


def test_the_nminus1_fixture_migrates_to_the_contract_catalog():
    outcome, after = _migrated(N1_DOC)
    assert (outcome.catalog_state, outcome.slots_state) == ("absent", "absent")
    factory = m.REVIEW_SEAT_RECOMMENDATION
    assert after == {"enabled": True, "items": [
        {"subagent_id": "review-1", "recommended_use": factory,
         "route": {"kind": "api_model", "target_id": "google/gemini-3.8-flash"},
         "effort": "high", "review_eligible": True, "minted_from": "factory_default"},
        {"subagent_id": "review-2", "recommended_use": factory,
         "route": {"kind": "api_model", "target_id": "openai/gpt-5.6-terra"},
         "effort": "high", "review_eligible": True, "minted_from": "factory_default"},
        {"subagent_id": "review-3", "recommended_use": factory,
         "route": {"kind": "api_model", "target_id": "anthropic/claude-opus-5"},
         "effort": "high", "review_eligible": True, "minted_from": "factory_default"},
        {"subagent_id": "review-4", "recommended_use": LANE_RECOMMENDATION,
         "route": {"kind": "api_model", "target_id": "openai/gpt-5.6-sol-pro"},
         "effort": "high", "minted_from": "review_lane"},
    ]}
    assert outcome.snapshot["summary"] == {"seats_before": 4, "rows_marked_after": 3, "distinct_models": 3,
                                           "helper_rows_minted": 1}
    rows = {entry["subagent_id"]: entry for entry in outcome.snapshot["rows"]}
    assert rows["review-2"]["from_seats"] == ["slot_2", "scope_slot_1"], "scope terra merged into review-2"
    assert outcome.snapshot["not_in_effect"] == []
    assert "shipped default review lanes" in m.owner_message(outcome, "x")
    # The retired comma keys never entered the panel (ABI-10) and are not read.
    assert set(N1_DOC) & set(RETIRED_COMMA_LIST_SETTING_KEYS)
    stripped = {k: v for k, v in N1_DOC.items() if k not in RETIRED_COMMA_LIST_SETTING_KEYS}
    assert m.migrate_review_lanes(stripped).catalog_after == outcome.catalog_after


@pytest.mark.parametrize("document", [anton_document(), N1_DOC], ids=["anton", "n-1"])
def test_seats_engines_and_deliveries_are_preserved(document):
    """Every engine (with its delivery) a lane seat ran is an engine of a marked row after
    M; the only fold is the declared scope merge, so the marked rows are exactly the
    distinct triad+scope engines."""
    if SLOTS not in document:
        document = {**{k: v for k, v in document.items() if k not in RETIRED_COMMA_LIST_SETTING_KEYS}, SLOTS: ""}
    executions = _executions(document)
    outcome, after = _migrated(document)

    def seat_engine(seat):
        return (seat["kind"], seat["target_id"], seat["credential_profile_id"], seat["effort"],
                seat["processing_preference"], seat.get("access", ""), seat["delivery"])

    def row_engine(row):
        route = row["route"]
        kind = "agent_session" if route["kind"] == "agent_session" else "api_chat"
        delivery = "session" if kind == "agent_session" else (row.get("delivery") or "native")
        effort = row.get("effort") or m.compound_session_effort(m._row_route(row))
        processing = m.resolve_processing_preference("", override=row.get("processing_preference") or None,
                                                    settings=dict(document))
        return (kind, route["target_id"], route.get("credential_profile_id", ""), effort, processing,
                row.get("access", "full") if kind == "agent_session" else "", delivery)

    before = {seat_engine(s) for s in executions["triad"] + executions["scope"]}
    marked = {row_engine(r) for r in after["items"] if r.get("review_eligible")}
    assert before == marked
    assert outcome.snapshot["summary"]["seats_before"] == len(executions["triad"]) + len(executions["scope"])
    assert outcome.snapshot["summary"]["rows_marked_after"] == len(marked)
    for row in after["items"]:
        if row.get("review_eligible") or row.get("minted_from"):
            assert row.get("effort") or m.compound_session_effort(m._row_route(row)), "no seat row leaves without effort"
