"""Configured-subagent references on reviewer LANE rows (generic-actor bridge).

removed by package A after package C freezes the lane readers: the triad
surfaces now read the catalog rows themselves (``tests/test_review_pool.py``);
these tests cover the lane parser's actor reference while it still exists.

A reviewer row may reference an ``OUROBOROS_SUBAGENTS`` roster row instead of
carrying an inline route. Resolution happens once at load/admission from the
APPLIED env; the resolved slot carries the actor id as identity/provenance and
the roster row's execution facts. An api_model actor is the RETRIEVES class
(bounded native tool rounds) and must never enter the assembled-packet plane
(the pack-assembly predicate); its roster model id still projects into the
legacy comma key, which no review surface reads once the structured key exists.
"""

import json

import pytest

from ouroboros.reviewer_slot_config import (
    REVIEWER_SLOTS_ENV,
    load_reviewer_slot_config,
    parse_reviewer_slots,
    project_reviewer_slots_into_env,
)

_ROSTER = {
    "enabled": True,
    "items": [
        {
            "subagent_id": "api-critic",
            "name": "API critic",
            "recommended_use": "Exact recursive API reviewer.",
            "route": {"kind": "api_model", "target_id": "openai/gpt-5.6-terra"},
            "effort": "medium",
        },
        {
            "subagent_id": "session-critic",
            "name": "Session critic",
            "recommended_use": "Subscription reviewer.",
            "route": {
                "kind": "agent_session",
                "target_id": "codex=gpt-5.6-sol",
                "credential_profile_id": "profile-1",
            },
            "effort": "high",
        },
    ],
}


def _payload(triad_rows, scope_rows=None):
    return json.dumps({
        "triad": triad_rows,
        "scope": scope_rows or [
            {"slot_id": "s1", "route": {"kind": "api_chat", "target_id": "openai/gpt-5.6-terra"}},
        ],
    })


@pytest.fixture()
def roster_env(monkeypatch):
    monkeypatch.setenv("OUROBOROS_SUBAGENTS", json.dumps(_ROSTER))
    for key in ("OUROBOROS_REVIEW_MODELS", "OUROBOROS_SCOPE_REVIEW_MODELS",
                "OUROBOROS_SCOPE_REVIEW_MODEL"):
        monkeypatch.delenv(key, raising=False)
    yield monkeypatch


def test_session_actor_row_resolves_from_roster(roster_env):
    roster_env.setenv(REVIEWER_SLOTS_ENV, _payload(
        [{"slot_id": "t1", "subagent_id": "session-critic"}]))
    row = load_reviewer_slot_config().triad[0]
    assert row.subagent_id == "session-critic"
    assert row.is_session and row.retrieves and not row.native_retrieval
    assert row.target_id == "codex=gpt-5.6-sol"
    assert row.session_target == "codex=gpt-5.6-sol"
    assert row.profile_id == "profile-1"
    # Roster effort applies when the row has no explicit one.
    assert row.effort == "high"


def test_api_actor_row_is_native_retrieval(roster_env):
    roster_env.setenv(REVIEWER_SLOTS_ENV, _payload(
        [{"slot_id": "t1", "subagent_id": "api-critic"}]))
    row = load_reviewer_slot_config().triad[0]
    assert row.subagent_id == "api-critic"
    assert row.kind == "api_chat"  # wire vocabulary stays closed
    assert row.native_retrieval and row.retrieves and not row.is_session
    assert row.target_id == "openai/gpt-5.6-terra"
    assert row.effort == "medium"


def test_explicit_row_effort_outranks_roster_effort(roster_env):
    roster_env.setenv(REVIEWER_SLOTS_ENV, _payload(
        [{"slot_id": "t1", "subagent_id": "api-critic", "effort": "xhigh"}]))
    assert load_reviewer_slot_config().triad[0].effort == "xhigh"


def test_unknown_subagent_id_refuses_typed(roster_env):
    with pytest.raises(ValueError, match="unknown_subagent_id"):
        parse_reviewer_slots(_payload([{"slot_id": "t1", "subagent_id": "ghost"}]))


def test_route_and_subagent_id_are_mutually_exclusive(roster_env):
    with pytest.raises(ValueError, match="either route or subagent_id"):
        parse_reviewer_slots(_payload([{
            "slot_id": "t1", "subagent_id": "api-critic",
            "route": {"kind": "api_chat", "target_id": "openai/gpt-5.6-terra"},
        }]))


def test_empty_subagent_id_refuses(roster_env):
    with pytest.raises(ValueError, match="subagent_id"):
        parse_reviewer_slots(_payload([{"slot_id": "t1", "subagent_id": "  "}]))


def test_api_rows_of_both_forms_project_their_model_ids_into_the_legacy_key(roster_env):
    """The legacy comma key is a projection of api MODEL IDS for legacy readers
    (external review tooling, benchmark manifests) — an actor row's roster model
    id is one, a session row's `harness[=model]` target is not. No review surface
    reads the key while the structured key exists (owner R2 retired the acceptance
    pin that used to filter actor rows out of it)."""
    roster_env.setenv(REVIEWER_SLOTS_ENV, _payload([
        {"slot_id": "t1", "subagent_id": "api-critic"},
        {"slot_id": "t2", "route": {"kind": "api_chat", "target_id": "openai/gpt-5.5"}},
        {"slot_id": "t3", "subagent_id": "session-critic"},
    ]))
    project_reviewer_slots_into_env()
    import os

    assert os.environ["OUROBOROS_REVIEW_MODELS"] == "openai/gpt-5.6-terra,openai/gpt-5.5"


def test_actor_binding_is_attempt_identity(roster_env):
    """A changed actor reference mints a new custody attempt key (#285 class)."""
    from types import SimpleNamespace

    from ouroboros.review_custody import _attempt_key
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_substrate import ReviewSlot

    request = SimpleNamespace(retry_key="", slot_messages={}, surface="multi_model_review",
                              task_id="t", call_type="multi_model_review")
    base = dict(slot_id="t1", model="openai/gpt-5.6-terra", effort="medium",
                route=ReviewRouteKind.API_CHAT)
    a = ReviewSlot(subagent_id="api-critic", **base)
    b = ReviewSlot(subagent_id="", **base)
    assert _attempt_key(request, a) != _attempt_key(request, b)


def test_roster_edit_changes_next_load_only(roster_env):
    roster_env.setenv(REVIEWER_SLOTS_ENV, _payload(
        [{"slot_id": "t1", "subagent_id": "api-critic"}]))
    before = load_reviewer_slot_config().triad[0]
    mutated = json.loads(json.dumps(_ROSTER))
    mutated["items"][0]["route"]["target_id"] = "openai/gpt-5.5"
    roster_env.setenv("OUROBOROS_SUBAGENTS", json.dumps(mutated))
    after = load_reviewer_slot_config().triad[0]
    assert before.target_id == "openai/gpt-5.6-terra"  # frozen materialization
    assert after.target_id == "openai/gpt-5.5"  # next load sees the edit


def test_legacy_sdk_advisory_target_migration_branches(roster_env):
    """The three non-trivial branches of the retired Claude-SDK target
    migration (owner decision 2026-08-29): same-model translation, [1m]
    strip, and the fail-closed unmapped target that force-disables the row
    with a typed reason — never a silently swapped reviewer model."""
    import json as _json

    from ouroboros.reviewer_slot_config import parse_reviewer_slots

    def _advisory(kind, target, enabled=True):
        return parse_reviewer_slots(_json.dumps({
            "triad": [{"slot_id": "t1", "route": {"kind": "api_chat", "target_id": "openai/m"}}],
            "scope": [{"slot_id": "s1", "route": {"kind": "api_chat", "target_id": "openai/m"}}],
            "advisory": {"enabled": enabled, "route": {"kind": kind, "target_id": target}},
        })).advisory

    # claude-* bare name → routed catalog id of the SAME model.
    migrated = _advisory("api", "claude-opus-4.6")
    assert migrated.kind == "api_chat"
    assert migrated.target_id == "anthropic/claude-opus-4.6"
    assert migrated.enabled is True and not migrated.disabled_reason

    # The [1m] Claude-SDK selector is stripped before translation.
    stripped = _advisory("api", "claude-opus-4.6[1m]")
    assert stripped.target_id == "anthropic/claude-opus-4.6"

    # An unmapped legacy spelling ('opus') force-disables with the typed
    # reason — the row is never silently pointed at a different model.
    unmapped = _advisory("api", "opus")
    assert unmapped.enabled is False
    assert unmapped.disabled_reason == "legacy_claude_sdk_target_unmapped"




def test_a_reference_to_an_owner_disabled_roster_row_refuses_typed(roster_env):
    """The roster's per-row switch binds reviewer references exactly as it binds
    delegation: the parser is the one authority, so the refusal is the same
    fail-closed ValueError every consumer already treats as malformed — never a
    silent fallback to another model or route."""
    roster = json.loads(json.dumps(_ROSTER))
    roster["items"][0]["enabled"] = False
    roster_env.setenv("OUROBOROS_SUBAGENTS", json.dumps(roster))
    roster_env.setenv(REVIEWER_SLOTS_ENV, _payload(
        [{"slot_id": "t1", "subagent_id": "api-critic"}]))

    with pytest.raises(ValueError) as refused:
        load_reviewer_slot_config()
    message = str(refused.value)
    assert "api-critic" in message
    assert "subagent_disabled" in message
    assert "switched off" in message

    # An enabled sibling reference still resolves: only the switched-off row is
    # withdrawn, and the surviving positive path stays open.
    roster_env.setenv(REVIEWER_SLOTS_ENV, _payload(
        [{"slot_id": "t1", "subagent_id": "session-critic"}]))
    config = load_reviewer_slot_config()
    assert config.triad[0].subagent_id == "session-critic"


def test_the_advisory_reference_is_bound_by_the_same_row_switch(roster_env):
    roster = json.loads(json.dumps(_ROSTER))
    roster["items"][1]["enabled"] = False
    roster_env.setenv("OUROBOROS_SUBAGENTS", json.dumps(roster))
    raw = json.dumps({
        "triad": [{"slot_id": "t1", "subagent_id": "api-critic"}],
        "scope": [{"slot_id": "s1", "route": {"kind": "api_chat", "target_id": "openai/gpt-5.6-terra"}}],
        "advisory": {"enabled": True, "subagent_id": "session-critic"},
    })
    with pytest.raises(ValueError, match="subagent_disabled"):
        parse_reviewer_slots(raw)

