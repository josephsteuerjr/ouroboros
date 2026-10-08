"""Exact structured reviewer LANE route and effort-authority regressions.

removed by package A after package C freezes the lane readers: the triad
effort/identity rules now run on the review pool (``tests/test_review_pool.py``);
what stays covers the lane parser, the advisory row and the scope lane.
"""

import json

import pytest

from ouroboros.reviewer_slot_config import (
    REVIEWER_SLOTS_ENV,
    parse_reviewer_slots,
)


def _payload() -> dict:
    return {
        "triad": [
            {
                "slot_id": "triad-route",
                "route": {"kind": "agent_session", "target_id": "codex=gpt-5.6-sol"},
            },
        ],
        "scope": [
            {
                "slot_id": "scope-route",
                "route": {"kind": "api_chat", "target_id": "openai/gpt-5.6-sol"},
            },
        ],
        "advisory": {"enabled": True, "route": {"kind": "api", "target_id": ""}},
    }


@pytest.mark.parametrize("target", ["off", "OFF", "=malformed", ":high"])
@pytest.mark.parametrize("surface", ["triad", "scope", "advisory"])
def test_structured_session_target_must_name_a_concrete_harness(target, surface):
    payload = _payload()
    if surface == "advisory":
        payload["advisory"] = {
            "enabled": True,
            "route": {"kind": "agent_session", "target_id": target},
        }
    else:
        payload[surface][0]["route"] = {
            "kind": "agent_session",
            "target_id": target,
        }

    with pytest.raises(ValueError, match="does not name a concrete harness route"):
        parse_reviewer_slots(json.dumps(payload))


def test_disabled_advisory_allows_empty_session_but_not_persisted_junk():
    payload = _payload()
    payload["advisory"] = {
        "enabled": False,
        "route": {"kind": "agent_session", "target_id": ""},
    }
    advisory = parse_reviewer_slots(json.dumps(payload)).advisory
    assert advisory.enabled is False and advisory.target_id == ""

    payload["advisory"]["route"]["target_id"] = "off"
    with pytest.raises(ValueError, match="does not name a concrete harness route"):
        parse_reviewer_slots(json.dumps(payload))


def test_settings_save_refuses_unparseable_session_target_before_persistence():
    from starlette.requests import Request

    from ouroboros.gateway.settings import _api_settings_post_locked

    payload = _payload()
    payload["triad"][0]["route"]["target_id"] = "=malformed"
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/settings",
        "headers": [],
        "query_string": b"",
    })
    response = _api_settings_post_locked(
        request,
        {REVIEWER_SLOTS_ENV: json.dumps(payload)},
    )
    body = json.loads(response.body)
    assert response.status_code == 400
    assert body["saved"] is False
    assert "does not name a concrete harness route" in body["error"]


def test_malformed_advisory_target_never_consults_the_shared_route(monkeypatch):
    from ouroboros import reviewer_slot_config

    payload = _payload()
    payload["advisory"] = {
        "enabled": True,
        "route": {"kind": "agent_session", "target_id": "off"},
    }
    monkeypatch.setenv(REVIEWER_SLOTS_ENV, json.dumps(payload))
    monkeypatch.setenv("OUROBOROS_REVIEW_SESSION_ROUTE", "codex=gpt-5.6-sol:high")

    with pytest.raises(ValueError, match="does not name a concrete harness route"):
        reviewer_slot_config.advisory_slot_config()


def test_last_execution_projection_keeps_a_declared_effort_apart_from_the_row(tmp_path, monkeypatch):
    """«Выполняется как» must not show the agent's one-off panel strength as the
    row's saved configuration: requested.effort is the ROW's effort ('' when the
    declaration filled it) and the declaration rides its own field."""
    from types import SimpleNamespace

    from ouroboros import reviewer_slot_config
    from ouroboros.review_substrate import ReviewSlot

    monkeypatch.setattr(reviewer_slot_config, "_last_execution_path", lambda: tmp_path / "last.json")
    slots = {
        "declared": ReviewSlot(slot_id="declared", model="m/a", effort="max", declared_effort="max"),
        "own": ReviewSlot(slot_id="own", model="m/b", effort="low"),
    }
    actors = [SimpleNamespace(slot_id=sid, status="ok", usage={}) for sid in slots]
    reviewer_slot_config.record_reviewer_slot_executions("plan_review", actors, slots)
    last = reviewer_slot_config.reviewer_slot_last_executions()
    assert last["declared"]["requested"]["effort"] == "" and last["declared"]["requested"]["declared_effort"] == "max"
    assert last["own"]["requested"]["effort"] == "low" and "declared_effort" not in last["own"]["requested"]


def test_compound_effort_stabilizes_commit_fingerprint_against_global_drift(
    monkeypatch,
):
    from ouroboros.tools.commit_gate import commit_review_contract_fingerprint
    from tests.review_pool_rosters import pool_roster, pool_seat, set_review_pool

    def pool(second: str) -> str:
        return pool_roster(pool_seat("grok-row", "cursor=cursor-grok-4.6-xhigh", kind="agent_session"),
                           pool_seat("agy-row", second, kind="agent_session"))

    # The pool's compound session rows carry their effort in the route slug: a
    # changed global effort moves nothing on them.
    set_review_pool(monkeypatch, pool("agy=gemini-3.1-pro-max-fast"))
    monkeypatch.setenv("OUROBOROS_EFFORT_REVIEW", "low")
    first = commit_review_contract_fingerprint()

    monkeypatch.setenv("OUROBOROS_EFFORT_REVIEW", "high")
    assert commit_review_contract_fingerprint() == first

    set_review_pool(monkeypatch, pool("agy=gemini-3.1-pro-xhigh-fast"))
    assert commit_review_contract_fingerprint() != first
