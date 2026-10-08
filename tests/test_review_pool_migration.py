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
from ouroboros import config as cfg
from ouroboros import review_pool_migration as m
from ouroboros import server_maintenance
from ouroboros.settings_defaults import (
    OPENROUTER_REVIEW_DEFAULTS,
    RETIRED_COMMA_LIST_SETTING_KEYS,
    RETIRED_SETTING_KEYS,
    RETIRED_SETTING_SUCCESSORS,
    REVIEW_POOL_MIGRATED_SETTING_KEYS,
    SETTINGS_DEFAULTS,
)
from supervisor import message_bus, state

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "nminus1"
N1_DOC = json.loads((FIXTURES / "settings_v6.113.4.json").read_text(encoding="utf-8"))
N2_DOC = json.loads((FIXTURES / "settings_v6.87.5.json").read_text(encoding="utf-8"))

SLOTS, SUBAGENTS = "OUROBOROS_REVIEWER_SLOTS", "OUROBOROS_SUBAGENTS"
LANE_RECOMMENDATION = "Minted from the former review lane"


@pytest.fixture(autouse=True)
def _pool_ceiling(monkeypatch):
    """Package A raises the catalog ceiling to 26 for the pool; until it lands, this tree
    caps at 10. The migration reads the live constant, so the tests set the pool value."""
    monkeypatch.setattr(cs, "MAX_CONFIGURED_SUBAGENTS", 26)
    m._MIGRATIONS_SEEN.clear()
    cfg._RETIREMENT_NOTICE_SEEN.clear()
    yield
    m._MIGRATIONS_SEEN.clear()
    cfg._RETIREMENT_NOTICE_SEEN.clear()


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


def test_the_nminus2_document_direct_equals_sequential():
    """A pre-6.90 document (no lanes key, retired comma keys) migrates straight to the pool
    exactly as it would have through the 6.90+ ``""`` lanes era: one hop == two hops."""
    assert SLOTS not in N2_DOC and SUBAGENTS not in N2_DOC
    assert set(N2_DOC) & set(RETIRED_COMMA_LIST_SETTING_KEYS)
    one_hop = cfg.normalize_settings_raw(dict(N2_DOC))
    via_lanes = {k: v for k, v in N2_DOC.items() if k not in RETIRED_COMMA_LIST_SETTING_KEYS}
    via_lanes[SLOTS] = ""
    two_hops = cfg.normalize_settings_raw(via_lanes)
    assert one_hop[SUBAGENTS] == two_hops[SUBAGENTS]
    items = json.loads(one_hop[SUBAGENTS])["items"]
    assert _marked(items) == ["review-1", "review-2", "review-3"]
    assert items[3]["route"]["target_id"] == "openai/gpt-5.6-sol-pro" and "review_eligible" not in items[3]
    for key in REVIEW_POOL_MIGRATED_SETTING_KEYS + RETIRED_COMMA_LIST_SETTING_KEYS:
        assert key not in one_hop and key not in two_hops
    assert cfg.normalize_settings_raw(dict(one_hop)) == one_hop


@pytest.mark.parametrize("document", [anton_document(), N1_DOC, N2_DOC], ids=["anton", "n-1", "n-2"])
def test_the_migration_is_idempotent_by_bytes(document):
    once = cfg.normalize_settings_raw(dict(document))
    twice = cfg.normalize_settings_raw(dict(once))
    assert json.dumps(once, sort_keys=True) == json.dumps(twice, sort_keys=True)
    assert m.migrate_review_lanes(once) is None, "a pool document carries nothing to migrate"
    assert not m.migration_applies(once)


@pytest.mark.parametrize("document", [anton_document(), N1_DOC, N2_DOC], ids=["anton", "n-1", "n-2"])
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


# --- 3. F4-F8 -------------------------------------------------------------------------


def test_f4_slot_ids_unique_subagent_ids_repeat_seat_effort_above_row_effort():
    doc = {SUBAGENTS: catalog(api_row("helper", "openai/gpt-5.6-terra", "medium")),
           SLOTS: lanes(triad=[ref("r1", "helper"), ref("r2", "helper", "xhigh")], scope=[ref("s1", "helper")])}
    outcome, after = _migrated(doc)
    assert after["items"] == [
        {**api_row("helper", "openai/gpt-5.6-terra", "medium"), "review_eligible": True},
        {"subagent_id": "review-1", "recommended_use": LANE_RECOMMENDATION,
         "route": {"kind": "api_model", "target_id": "openai/gpt-5.6-terra"}, "effort": "xhigh",
         "review_eligible": True, "minted_from": "review_lane"}]
    rows = {entry["subagent_id"]: entry for entry in outcome.snapshot["rows"]}
    assert rows["helper"]["action"] == "marked" and rows["helper"]["from_seats"] == ["r1", "s1"]
    assert "merged" in rows["helper"]["note"]
    assert rows["review-1"]["from_seats"] == ["r2"] and "helper keeps effort medium" in rows["review-1"]["note"]
    assert outcome.snapshot["summary"] == {"seats_before": 3, "rows_marked_after": 2, "distinct_models": 1,
                                           "helper_rows_minted": 0}


def test_f5a_the_pool_ceiling_is_the_catalog_ceiling():
    rows = [api_row(f"h{i}", f"vendor/model-{i}", "low") for i in range(10)]
    triad = [direct(f"t{i}", f"vendor/triad-{i}", "high", delivery="native") for i in range(10)]
    scope = [direct(f"s{i}", f"vendor/scope-{i}", "high") for i in range(4)]
    doc = {SUBAGENTS: catalog(*rows),
           SLOTS: lanes(triad=triad, scope=scope,
                        advisory={"enabled": True, "route": {"kind": "api_chat", "target_id": "vendor/adv"}, "effort": "low"},
                        deep_review={"route": {"kind": "api_chat", "target_id": "vendor/deep"}, "effort": "high"})}
    outcome, after = _migrated(doc)
    assert len(after["items"]) == 26 and len(_marked(after["items"])) == 14
    assert [r["subagent_id"] for r in after["items"][10:]] == [f"review-{i}" for i in range(1, 17)]
    # A 27th row refuses the WHOLE migration: no partial pool, the lanes stay.
    doc27 = dict(doc, **{SUBAGENTS: catalog(*rows, api_row("h10", "vendor/model-10", "low"))})
    refused = m.migrate_review_lanes(doc27)
    assert refused.error and "27 rows" in refused.error and "26" in refused.error
    assert refused.catalog_after is None and refused.retained_keys == (SLOTS,)
    loaded = dict(doc27)
    m.apply_outcome(loaded, refused)
    assert loaded == doc27


def test_f5b_twins_of_one_engine_become_two_marked_rows():
    doc = {SLOTS: lanes(triad=[direct("a", "openai/gpt-5.6-sol", delivery="native"),
                               direct("b", "openai/gpt-5.6-sol", delivery="native")],
                        scope=[direct("s", "openai/gpt-5.6-sol")])}
    outcome, after = _migrated(doc)
    assert _marked(after["items"]) == ["review-1", "review-2"] and len(after["items"]) == 2
    assert after["items"][0]["route"] == after["items"][1]["route"] == {"kind": "api_model", "target_id": "openai/gpt-5.6-sol"}
    assert all(r["effort"] == "high" and "delivery" not in r for r in after["items"])
    assert outcome.snapshot["summary"]["distinct_models"] == 1
    rows = {entry["subagent_id"]: entry for entry in outcome.snapshot["rows"]}
    assert rows["review-1"]["from_seats"] == ["a", "s"], "the scope twin merges into the first produced row"
    assert rows["review-2"]["from_seats"] == ["b"]


def test_f5c_a_direct_advisory_coinciding_with_a_direct_triad_seat_keeps_its_own_helper_row():
    doc = {SLOTS: lanes(triad=[direct("t", "anthropic/claude-sonnet-5", "low", delivery="native")],
                        scope=[direct("s", "openai/gpt-5.6-terra", "high")],
                        advisory={"enabled": True, "route": {"kind": "api_chat", "target_id": "anthropic/claude-sonnet-5"},
                                  "effort": "low"})}
    outcome, after = _migrated(doc)
    assert _marked(after["items"]) == ["review-1", "review-2"]
    helper = after["items"][2]
    assert helper["subagent_id"] == "review-3" and "review_eligible" not in helper
    assert helper["minted_from"] == "review_lane" and helper["recommended_use"] == LANE_RECOMMENDATION
    assert helper["route"] == after["items"][0]["route"] and helper["effort"] == "low"
    note = {e["subagent_id"]: e for e in outcome.snapshot["rows"]}["review-3"]["note"]
    assert "coincided with triad seat t" in note and "separate helper row review-3 (no mark)" in note
    assert "preflight is now chosen per commit" in note


def test_f7_an_empty_scope_effort_materializes_before_the_key_is_retired():
    doc = {"OUROBOROS_EFFORT_REVIEW": "high", "OUROBOROS_EFFORT_SCOPE_REVIEW": "xhigh",
           SLOTS: lanes(triad=[direct("t", "openai/gpt-5.6-terra")], scope=[direct("s", "openai/gpt-5.6-terra")])}
    outcome, after = _migrated(doc)
    assert after["items"] == [
        {"subagent_id": "review-1", "recommended_use": LANE_RECOMMENDATION,
         "route": {"kind": "api_model", "target_id": "openai/gpt-5.6-terra"}, "effort": "high",
         "review_eligible": True, "delivery": "packet", "minted_from": "review_lane"},
        {"subagent_id": "review-2", "recommended_use": LANE_RECOMMENDATION,
         "route": {"kind": "api_model", "target_id": "openai/gpt-5.6-terra"}, "effort": "xhigh",
         "review_eligible": True, "minted_from": "review_lane"}]
    assert outcome.snapshot["not_in_effect"] == []
    loaded = cfg.normalize_settings_raw(dict(doc))
    assert "OUROBOROS_EFFORT_SCOPE_REVIEW" not in loaded and json.loads(loaded[SUBAGENTS]) == after


def test_f7b_packet_triad_and_native_scope_of_one_engine_stay_two_rows_at_equal_effort():
    doc = {SLOTS: lanes(triad=[direct("t", "openai/gpt-5.6-terra", "high")],
                        scope=[direct("s", "openai/gpt-5.6-terra", "high")])}
    _outcome, after = _migrated(doc)
    assert [(r["subagent_id"], r.get("delivery")) for r in after["items"]] == [("review-1", "packet"), ("review-2", None)]


def test_f8_delivery_is_serialized_only_as_packet_and_only_on_direct_api_triad_rows():
    doc = {SLOTS: lanes(triad=[direct("p", "a/one", "high", delivery="packet"),
                               direct("n", "b/two", "high", delivery="native"),
                               direct("d", "c/three", "high"),
                               direct("s", "codex=gpt-5.6-sol", "high", kind="agent_session")],
                        scope=[direct("sc", "a/one", "high")])}
    _outcome, after = _migrated(doc)
    by_id = {r["subagent_id"]: r for r in after["items"]}
    assert by_id["review-1"]["delivery"] == "packet"
    assert "delivery" not in by_id["review-2"], "native is the default and is not written"
    assert by_id["review-3"]["delivery"] == "packet", "a direct api triad row without delivery ran as a packet"
    assert "delivery" not in by_id["review-4"] and by_id["review-4"]["route"]["kind"] == "agent_session"
    assert by_id["review-4"]["access"] == "full"
    assert "delivery" not in by_id["review-5"], "the scope seat of a/one (reads) is its own row beside the packet twin"
    assert len(after["items"]) == 5
    assert m.parse_reviewer_slots({}, lanes(triad=[direct("t", "a/one")],
                                            scope=[direct("s", "a/one")])).triad[0].delivery == ""
    with pytest.raises(ValueError, match="delivery"):
        m.parse_reviewer_slots({}, lanes(triad=[direct("t", "a/one")], scope=[direct("s", "a/one", delivery="packet")]))


# --- 4. F6: the catalog x lanes matrix ------------------------------------------------


def _catalog_for(state_id):
    rows = [api_row("helper", "openai/gpt-5.6-terra", "medium"), session_row("coder", "codex=gpt-5.6-sol", "high")]
    return {
        "A": None,
        "E": catalog(),
        "I": '{"enabled": true, "items": [{"subagent_id": "helper"}]}',
        "D": catalog(*rows, enabled=False),
        "C": catalog(*rows),
    }[state_id]


def _lanes_for(state_id):
    return {
        "0": "",
        "P": lanes(triad=[direct("t1", "x/one", "high", delivery="native"), direct("t2", "y/two", "high")],
                   scope=[direct("s1", "x/one", "high")],
                   advisory={"enabled": True, "route": {"kind": "api_chat", "target_id": "z/adv"}, "effort": "low"},
                   deep_review={"route": {"kind": "api_chat", "target_id": "w/deep"}, "effort": "high"}),
        "R": lanes(triad=[ref("t1", "helper"), ref("t2", "coder")], scope=[ref("s1", "helper")],
                   advisory={"enabled": True, "subagent_id": "coder"}),
        "M": lanes(triad=[ref("t1", "helper"), direct("t2", "y/two", "high")], scope=[direct("s1", "x/one", "high")]),
        "X": '{"triad": [{"model": "x/y"}]}',
    }[state_id]


F6_CELLS = [f"{c}{s}" for c in "AEIDC" for s in "0PRMX"]


@pytest.mark.parametrize("cell", F6_CELLS)
def test_f6_catalog_by_lanes_matrix(cell):
    catalog_id, lanes_id = cell
    doc = {"OUROBOROS_MODEL": "openai/gpt-5.6-sol", "OPENROUTER_API_KEY": "present", SLOTS: _lanes_for(lanes_id)}
    stored = _catalog_for(catalog_id)
    if stored is not None:
        doc[SUBAGENTS] = stored
    before = copy.deepcopy(doc)
    outcome = m.migrate_review_lanes(doc)
    assert doc == before, "pure: the input is never mutated"
    assert outcome is not None
    expected_catalog = {"A": "absent", "E": "empty", "I": "invalid", "D": "disabled", "C": "configured"}[catalog_id]
    assert outcome.catalog_state == expected_catalog
    loaded = dict(doc)
    m.apply_outcome(loaded, outcome)

    # The refusal cells: an invalid catalog, invalid lanes, or references that cannot
    # resolve (no catalog, an empty one, a disabled one) — no partial migration, the
    # lane keys stay for the owner's catalog save, the catalog is untouched.
    refused = catalog_id == "I" or lanes_id == "X" or (lanes_id in "RM" and catalog_id in "AED")
    if refused:
        assert outcome.error and outcome.catalog_after is None, outcome
        assert outcome.retained_keys == (SLOTS,) and outcome.consumed_keys == ()
        assert loaded == doc, "nothing rewritten, the lanes key stays"
        assert outcome.slots_state == ("invalid" if lanes_id in "XRM" or catalog_id != "I" else outcome.slots_state)
        assert m.owner_message(outcome, "snap.json").startswith("⚙️ Review settings could not be migrated automatically")
        assert "snap.json" in m.owner_message(outcome, "snap.json")
        return

    assert not outcome.error and outcome.consumed_keys == (SLOTS,) and outcome.retained_keys == ()
    after = json.loads(loaded[SUBAGENTS])
    assert SLOTS not in loaded
    expected_slots = {"0": "absent", "P": "direct", "R": "referenced", "M": "mixed"}[lanes_id]
    assert outcome.slots_state == expected_slots
    # The catalog switch is never touched: a disabled catalog stays disabled (review
    # stays on through the pool; delegation stays off) and the report says so.
    assert after["enabled"] is (catalog_id != "D")
    if catalog_id == "D":
        assert any("review stays on; delegation stays off" in note for note in outcome.snapshot["notes"])
    existing = json.loads(stored)["items"] if stored else []
    assert [r["subagent_id"] for r in after["items"][:len(existing)]] == [r["subagent_id"] for r in existing]
    marked = _marked(after["items"])
    if lanes_id == "0":
        # The shipped default panel ran: factory rows, minted as such, three distinct
        # OpenRouter models with the scope seat merged into the terra row.
        minted = [r for r in after["items"] if r.get("minted_from") == "factory_default"]
        assert [r["route"]["target_id"] for r in minted] == list(OPENROUTER_REVIEW_DEFAULTS["triad"])
        assert marked == [r["subagent_id"] for r in minted] and all(r["effort"] == "high" for r in minted)
        assert all(r["recommended_use"] == m.REVIEW_SEAT_RECOMMENDATION for r in minted)
        assert len(after["items"]) == len(existing) + 3, "no advisory row (unauthored), no deep row (key empty)"
        assert outcome.snapshot["summary"]["distinct_models"] == 3
    elif lanes_id == "P":
        minted = after["items"][len(existing):]
        assert [r["subagent_id"] for r in minted] == ["review-1", "review-2", "review-3", "review-4"]
        assert marked == ["review-1", "review-2"], "advisory and deep helpers carry no mark"
        assert minted[1]["delivery"] == "packet" and "delivery" not in minted[0]
        assert minted[2]["route"]["target_id"] == "z/adv" and minted[3]["route"]["target_id"] == "w/deep"
        assert all(r["minted_from"] == "review_lane" for r in minted)
    elif lanes_id == "R":
        assert marked == ["helper", "coder"] and len(after["items"]) == len(existing)
        assert not any(r.get("minted_from") for r in after["items"])
    else:  # M
        assert marked == ["helper", "review-1", "review-2"]
        assert after["items"][2]["route"]["target_id"] == "y/two" and after["items"][3]["route"]["target_id"] == "x/one"
    assert all(r.get("effort") for r in after["items"] if r.get("minted_from")), "no minted row without effort"


def test_f6_c0_is_the_ordinary_upgrade_of_antons_catalog_without_lanes():
    doc = anton_document()
    doc[SLOTS] = ""
    doc["OUROBOROS_MODEL_DEEP_SELF_REVIEW"] = ""
    for key in ("OUROBOROS_EFFORT_REVIEW", "OUROBOROS_EFFORT_SCOPE_REVIEW"):
        doc.pop(key)  # the shipped "high"
    outcome, after = _migrated(doc)
    assert (outcome.catalog_state, outcome.slots_state) == ("configured", "absent")
    assert len(after["items"]) == 12 and _marked(after["items"]) == ["review-1", "review-2", "review-3"]
    assert [r["route"]["target_id"] for r in after["items"][9:]] == list(OPENROUTER_REVIEW_DEFAULTS["triad"])
    rows = {entry["subagent_id"]: entry for entry in outcome.snapshot["rows"]}
    assert rows["review-2"]["from_seats"] == ["slot_2", "scope_slot_1"]
    assert "advisory" in " ".join(outcome.snapshot["notes"]) and outcome.snapshot["summary"]["helper_rows_minted"] == 0
    # The panel at the document's "medium" coincides with an existing terra@medium row:
    # that row is marked (at most once) instead of a new one, the other two are minted.
    doc["OUROBOROS_EFFORT_REVIEW"] = doc["OUROBOROS_EFFORT_SCOPE_REVIEW"] = "medium"
    outcome, after = _migrated(doc)
    assert len(after["items"]) == 11 and _marked(after["items"]) == ["subagent_a2", "review-1", "review-2"]
    assert {e["subagent_id"]: e["from_seats"] for e in outcome.snapshot["rows"]}["subagent_a2"] == ["slot_2", "scope_slot_1"]


def test_a_document_with_neither_lanes_nor_catalog_nor_legacy_keys_is_left_to_onboarding():
    assert m.migrate_review_lanes({"TOTAL_BUDGET": 1.0}) is None
    assert m.migrate_review_lanes({"OUROBOROS_MODEL": "x/y", "OPENROUTER_API_KEY": "present"}) is None
    pool = {SUBAGENTS: catalog(api_row("r", "x/y", "high", review_eligible=True, minted_from="factory_default"))}
    assert m.migrate_review_lanes(pool) is None, "a pool document without the lanes key is done"


def test_a_pool_catalog_that_still_carries_the_lanes_key_drops_it_without_a_rewrite():
    pool = catalog(api_row("r", "x/y", "high", review_eligible=True, minted_from="factory_default"))
    doc = {SUBAGENTS: pool, SLOTS: ANTON_LANES, "OUROBOROS_EFFORT_REVIEW": "medium"}
    outcome = m.migrate_review_lanes(doc)
    assert outcome.noop and not outcome.error and outcome.catalog_after is None
    assert outcome.consumed_keys == (SLOTS, "OUROBOROS_EFFORT_REVIEW")
    loaded = cfg.normalize_settings_raw(dict(doc))
    assert loaded[SUBAGENTS] == pool and SLOTS not in loaded and "OUROBOROS_EFFORT_REVIEW" not in loaded
    assert m.owner_message(outcome, "x") == ""


def test_a_deep_review_reference_and_a_disabled_advisory_mint_nothing():
    doc = {SUBAGENTS: catalog(api_row("helper", "a/one", "high"), api_row("deep", "b/two", "xhigh")),
           SLOTS: lanes(triad=[ref("t", "helper")], scope=[ref("s", "helper")],
                        advisory={"enabled": False, "route": {"kind": "api_chat", "target_id": "c/adv"}},
                        deep_review={"subagent_id": "deep"})}
    outcome, after = _migrated(doc)
    assert len(after["items"]) == 2 and _marked(after["items"]) == ["helper"]
    assert "the deep review reference to deep needed no row" in outcome.snapshot["notes"]
    assert any("disabled" in note for note in outcome.snapshot["notes"])
    assert outcome.snapshot["summary"]["helper_rows_minted"] == 0


def test_a_referenced_row_with_divergent_processing_or_pin_mints_from_the_source_row():
    doc = {SUBAGENTS: catalog(api_row("helper", "claudexor::codex=gpt-6-astra", "medium",
                                      processing_preference="economy")),
           SLOTS: lanes(triad=[ref("t", "helper", "xhigh")], scope=[ref("s", "helper", "xhigh")])}
    _outcome, after = _migrated(doc)
    assert _marked(after["items"]) == ["review-1"]
    minted = after["items"][1]
    assert minted["route"] == {"kind": "api_model", "target_id": "claudexor::codex=gpt-6-astra", "credential_profile_id": ""}
    assert minted["effort"] == "xhigh" and minted["processing_preference"] == "economy"
    assert "review_eligible" not in after["items"][0]


def test_package_a_factory_rows_seam_is_adopted_when_bound(monkeypatch):
    """``factory_review_rows(doc)`` (package A) mints the rows a fresh install's onboarding
    writes; when it is bound, the factory cell adopts those rows and only mints what they
    do not cover — the seam is a module attribute so the two packages meet without an import."""
    calls = []

    def factory_rows(document):
        calls.append(dict(document))
        return [{"subagent_id": "review-1", "recommended_use": "A's row",
                 "route": {"kind": "api_model", "target_id": "google/gemini-3.8-flash"}, "effort": "",
                 "review_eligible": True, "minted_from": "factory_default"}]

    monkeypatch.setattr(m, "factory_review_rows", factory_rows)
    outcome, after = _migrated(N1_DOC)
    assert calls, "the seam was consulted"
    assert after["items"][0]["recommended_use"] == "A's row" and after["items"][0]["effort"] == "high"
    assert [r["route"]["target_id"] for r in after["items"][:3]] == list(OPENROUTER_REVIEW_DEFAULTS["triad"])
    assert _marked(after["items"]) == ["review-1", "review-2", "review-3"]
    assert outcome.snapshot["summary"]["rows_marked_after"] == 3


@pytest.mark.parametrize("install, main", [
    ("local-only", {"USE_LOCAL_MAIN": True, "LOCAL_MODEL_SOURCE": "owner/local.gguf", "OUROBOROS_MODEL": "owner/local-main"}),
    ("compatible-only", {"OPENAI_COMPATIBLE_BASE_URL": "https://llm.example/v1", "OUROBOROS_MODEL": "openai-compatible::glm-5.3"}),
])
def test_factory_cells_of_a_one_model_install_mint_the_three_runs_of_main(install, main):
    """I3-1: the factory pool of a local-only or compatible-only install (cells (A,0),
    (C,0), (D,0), (E,0)) is what those installs ran — three independent seats of
    Main (twins, quorum 2 of 3) — through package A's bound seam, so the migration
    and a fresh onboarding mint one shape; an OpenRouter install keeps three models."""
    from ouroboros.review_model_routes import adaptive_quorum

    outcome, after = _migrated({SLOTS: "", **main})
    marked = [row for row in after["items"] if row.get("review_eligible")]
    assert [row["subagent_id"] for row in marked] == ["review-1", "review-2", "review-3"], install
    assert [row["route"]["target_id"] for row in marked] == [main["OUROBOROS_MODEL"]] * 3, install
    assert all(row["minted_from"] == "factory_default" and row["effort"] == "high" for row in marked), install
    assert outcome.snapshot["summary"]["rows_marked_after"] == 3 and adaptive_quorum(len(marked)) == 2
    assert outcome.snapshot["summary"]["distinct_models"] == 1, "twins are one engine, disclosed"
    # The same seam on an existing catalog with one row of that engine: marked, not twinned (F6).
    twin = api_row("mine", main["OUROBOROS_MODEL"], "high")
    _outcome, after = _migrated({SLOTS: "", SUBAGENTS: catalog(twin), **main})
    assert _marked(after["items"]) == ["mine", "review-1", "review-2"], install
    # OpenRouter: three different models, as before.
    _outcome, after = _migrated({SLOTS: "", "OPENROUTER_API_KEY": "present"})
    assert [r["route"]["target_id"] for r in after["items"] if r.get("review_eligible")] == list(
        OPENROUTER_REVIEW_DEFAULTS["triad"])


# --- 5. the read seam -----------------------------------------------------------------


def test_normalize_settings_raw_migrates_once_per_document_digest(monkeypatch):
    doc = anton_document()
    loaded = cfg.normalize_settings_raw(dict(doc))
    assert SLOTS not in loaded and len(json.loads(loaded[SUBAGENTS])["items"]) == 11
    for key in REVIEW_POOL_MIGRATED_SETTING_KEYS:
        assert key not in loaded
    outcomes = cfg.review_pool_migrations_seen()
    assert len(outcomes) == 1 and outcomes[0].input_sha256 == m.input_sha256(doc)
    # The same document again replays the recorded outcome: the migration is not recomputed.
    monkeypatch.setattr(m, "migrate_review_lanes", lambda _doc: pytest.fail("recomputed"))
    assert cfg.normalize_settings_raw(dict(doc)) == loaded
    assert len(cfg.review_pool_migrations_seen()) == 1
    # The purge never reports the consumed lane keys as a loss.
    assert cfg.retired_key_sets_seen() == ()


def test_the_read_seam_keeps_the_lane_keys_of_a_migration_that_could_not_finish(caplog):
    import logging

    doc = {SUBAGENTS: catalog(), SLOTS: lanes(triad=[ref("t", "ghost")], scope=[]),
           "OUROBOROS_EFFORT_REVIEW": "medium", "OUROBOROS_SCOPE_REVIEW_FLOOR": "blocking_1m"}
    with caplog.at_level(logging.WARNING, logger="ouroboros.review_pool_migration"):
        loaded = cfg.normalize_settings_raw(dict(doc))
    assert loaded[SLOTS] == doc[SLOTS] and loaded["OUROBOROS_EFFORT_REVIEW"] == "medium"
    assert loaded[SUBAGENTS] == catalog(), "no partial migration"
    assert "OUROBOROS_SCOPE_REVIEW_FLOOR" not in loaded, "the ordinary retired purge still runs"
    assert cfg.retired_key_sets_seen() == (("OUROBOROS_SCOPE_REVIEW_FLOOR",),)
    (outcome,) = cfg.review_pool_migrations_seen()
    assert outcome.error and set(outcome.retained_keys) == {SLOTS, "OUROBOROS_EFFORT_REVIEW"}
    assert any("review lanes not migrated" in r.getMessage() for r in caplog.records)


def test_the_five_lane_keys_are_retired_with_the_catalog_as_successor():
    assert set(REVIEW_POOL_MIGRATED_SETTING_KEYS) <= set(RETIRED_SETTING_KEYS)
    for key in REVIEW_POOL_MIGRATED_SETTING_KEYS:
        assert key not in SETTINGS_DEFAULTS
        assert RETIRED_SETTING_SUCCESSORS[key] == (SUBAGENTS,)
    assert not set(REVIEW_POOL_MIGRATED_SETTING_KEYS) & set(RETIRED_COMMA_LIST_SETTING_KEYS)
    # A stray effort key without lanes on a pool document reaches the ordinary notice.
    pool = {SUBAGENTS: catalog(api_row("r", "x/y", "high", review_eligible=True)), "OUROBOROS_EFFORT_REVIEW": "low"}
    loaded = cfg.normalize_settings_raw(dict(pool))
    assert "OUROBOROS_EFFORT_REVIEW" not in loaded
    assert cfg.retired_key_sets_seen() == (("OUROBOROS_EFFORT_REVIEW",),)


def test_the_input_digest_reads_credentials_by_presence_only():
    doc = anton_document()
    other = dict(doc, OPENROUTER_API_KEY="another-value")
    assert m.input_sha256(doc) == m.input_sha256(other)
    assert m.input_sha256(doc) != m.input_sha256({k: v for k, v in doc.items() if k != "OPENROUTER_API_KEY"})
    assert m.input_sha256(doc) != m.input_sha256(dict(doc, OUROBOROS_EFFORT_REVIEW="high"))


# --- 6. the supervisor boot: snapshot once, owner told once -------------------------------


@pytest.fixture
def boot(tmp_path, monkeypatch):
    state.init(tmp_path)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "locks").mkdir(parents=True, exist_ok=True)
    state.save_state({})
    monkeypatch.setattr(server_maintenance, "DATA_DIR", tmp_path)
    sent: list = []
    monkeypatch.setattr(message_bus, "send_with_budget",
                        lambda chat_id, text, *args, **kwargs: sent.append((chat_id, text, kwargs)))
    return tmp_path, sent


def _snapshots(root):
    return sorted(p for p in (root / "state" / "review_migrations").glob("*-slots-to-pool.json")) \
        if (root / "state" / "review_migrations").exists() else []


def test_the_snapshot_is_written_once_and_the_owner_hears_once(boot):
    root, sent = boot
    loaded = cfg.normalize_settings_raw(anton_document())
    state.update_state(lambda st: st.__setitem__("owner_chat_id", 7))

    server_maintenance._startup_review_pool_notice(loaded)
    server_maintenance._startup_review_pool_notice(loaded)
    cfg.normalize_settings_raw(anton_document())  # the same document read again
    server_maintenance._startup_review_pool_notice(loaded)

    files = _snapshots(root)
    assert len(files) == 1
    name = files[0].name
    assert len(name) == len("20261007T214000Z-slots-to-pool.json") and name.endswith("Z-slots-to-pool.json")
    snapshot = json.loads(files[0].read_text(encoding="utf-8"))
    assert snapshot["schema"] == 1 and snapshot["ts"] == name.split("-", 1)[0]
    assert snapshot["input_sha256"] == m.input_sha256(anton_document())
    assert snapshot["before"]["OUROBOROS_REVIEWER_SLOTS"] == ANTON_LANES
    assert snapshot["before"]["OUROBOROS_EFFORT_REVIEW"] == "medium"
    assert snapshot["before"]["catalog_state"] == "configured" and snapshot["before"]["slots_state"] == "mixed"
    assert snapshot["effective_before"] == ANTON_EXPECTED_EXECUTIONS
    assert json.loads(loaded[SUBAGENTS]) == snapshot["after"][SUBAGENTS]
    assert len(snapshot["rows"]) == 4 and snapshot["error"] == ""

    assert len(sent) == 1
    chat_id, text, kwargs = sent[0]
    assert chat_id == 7 and kwargs == {"role": "system", "system_type": "review_pool_migration_notice"}
    assert text == m.owner_message(cfg.review_pool_migrations_seen()[0], f"state/review_migrations/{name}")
    assert f"Snapshot: state/review_migrations/{name}." in text

    records = server_maintenance.review_pool_migration_records()
    (record,) = records.values()
    assert record["snapshot"] == f"state/review_migrations/{name}" and record["reported"] and record["error"] == ""
    assert record["ts"] == snapshot["ts"]

    # A fresh process (empty in-process seam) reading the same document is quiet.
    m._MIGRATIONS_SEEN.clear()
    cfg.normalize_settings_raw(anton_document())
    server_maintenance._startup_review_pool_notice(loaded)
    assert len(_snapshots(root)) == 1 and len(sent) == 1


def test_without_an_owner_chat_the_snapshot_is_written_but_the_message_waits(boot):
    root, sent = boot
    loaded = cfg.normalize_settings_raw(dict(N1_DOC))
    server_maintenance._startup_review_pool_notice(loaded)
    assert len(_snapshots(root)) == 1 and sent == []
    (record,) = server_maintenance.review_pool_migration_records().values()
    assert record["reported"] is None

    state.update_state(lambda st: st.__setitem__("owner_chat_id", 3))
    server_maintenance._startup_review_pool_notice(loaded)
    assert len(_snapshots(root)) == 1 and [row[0] for row in sent] == [3]
    assert "shipped default review lanes" in sent[0][1]
    server_maintenance._startup_review_pool_notice(loaded)
    assert len(sent) == 1


def test_a_refused_migration_is_recorded_and_reported_with_its_error(boot):
    root, sent = boot
    state.update_state(lambda st: st.__setitem__("owner_chat_id", 7))
    doc = {SUBAGENTS: catalog(), SLOTS: lanes(triad=[ref("t", "ghost")], scope=[])}
    loaded = cfg.normalize_settings_raw(dict(doc))
    server_maintenance._startup_review_pool_notice(loaded)
    (path,) = _snapshots(root)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    assert snapshot["error"] and snapshot["after"] is None and snapshot["summary"] is None
    assert len(sent) == 1 and sent[0][1].startswith("⚙️ Review settings could not be migrated automatically")
    assert "ghost" in sent[0][1] and path.name in sent[0][1]
    (record,) = server_maintenance.review_pool_migration_records().values()
    assert record["error"] == snapshot["error"]


def test_a_noop_outcome_leaves_no_receipt(boot):
    root, sent = boot
    state.update_state(lambda st: st.__setitem__("owner_chat_id", 7))
    pool = catalog(api_row("r", "x/y", "high", review_eligible=True, minted_from="factory_default"))
    loaded = cfg.normalize_settings_raw({SUBAGENTS: pool, SLOTS: ""})
    assert cfg.review_pool_migrations_seen()[0].noop
    server_maintenance._startup_review_pool_notice(loaded)
    assert _snapshots(root) == [] and sent == []
    assert server_maintenance.review_pool_migration_records() == {}
