"""TZ-2 C4: an explicit author stop stays a stop (owner-confirmed defect).

The reported chain: Main calls ``task_acceptance_review(author_action=stop)``
while the task still has running services; the host stops them at delivery
(``services_stopped``), which changes the delivery evidence fingerprint; the
finish-style freshness check then rejected the stop, a reviewer panel was
bought, and the advisory ``author_finish`` recorded by ``_finish_cyber_acceptance``
turned the objective green — a Done card over text saying "not ready".

A stop grants nothing, so nothing about it needs to be fresh: it is recorded at
once as ``author_stop`` with no panel; only a finish binds to the reviewed
feedback and the three freshness facts. A later explicit finish still replaces
a stop through ``merge_agent_acceptance_stance`` (no new mechanism).
"""

from __future__ import annotations

import json
import queue
import subprocess
from types import SimpleNamespace

import pytest

STOP_RATIONALE = "Not ready: the export endpoint still fails on empty input."
STOP_TEXT = STOP_RATIONALE + " Stopping with the work unfinished."


def _fail_panel_result():
    from ouroboros.review_substrate import ReviewRunResult

    return ReviewRunResult(
        request={"surface": "task_acceptance", "policy": {"min_successful_slots": 1}},
        actors=[{"slot_id": "critic", "signal": "FAIL", "parsed": {
            "verdict": "FAIL", "outcome_tier": "best_effort", "completion_coach": "Fix the output.",
        }}], parsed_findings=[], aggregate_signal="FAIL",
    )


def _run_stop_loop(tmp_path, monkeypatch, responses, *, stop_services):
    """A real loop: real tool round, real nomination, real host acceptance pass.

    Only the model, the reviewer panel and the service teardown are substituted.
    """
    import ouroboros.loop as loop
    import ouroboros.review_substrate as review_substrate
    from ouroboros.tools import services as services_mod
    from ouroboros.tools.registry import ToolRegistry
    from tests.test_loop_acceptance_gate import _seed_acceptance_root

    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "required")
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "3")
    monkeypatch.setenv("OUROBOROS_MAX_ROUNDS", "12")
    monkeypatch.setenv("OUROBOROS_SAFETY_MODE", "off")
    monkeypatch.setenv("MCP_ENABLED", "false")
    monkeypatch.setattr(loop, "_maybe_inject_finalization_nudges", lambda *_args: False)
    monkeypatch.setattr(review_substrate, "triad_delivery_slots", lambda **_kw: [object()])
    panels: list = []

    def panel(ctx):
        panels.append(ctx.content)
        return _fail_panel_result()

    monkeypatch.setattr(loop, "_execute_task_acceptance_panel", panel)
    teardowns = {"count": 0}

    def fake_stop(_ctx):
        teardowns["count"] += 1
        if teardowns["count"] == 1 and stop_services:
            return [{"service_id": "preview", "name": "preview", "lifecycle": "stopped",
                     "artifact_outputs": "report.html was captured"}]
        return []

    monkeypatch.setattr(services_mod, "stop_task_services", fake_stop)
    answers = iter(responses)
    model_inputs: list = []

    def fake_call(_llm, request_messages, *_args, **_kwargs):
        model_inputs.append([dict(row) for row in request_messages])
        answer = next(answers)
        if isinstance(answer, dict):
            return {"role": "assistant", **answer}, 0.0
        return {"role": "assistant", "content": answer}, 0.0

    monkeypatch.setattr(loop, "call_llm_with_retry", fake_call)
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    repo.mkdir()
    data.mkdir()
    for args in (["init"], ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                           "commit", "--allow-empty", "-m", "fixture baseline"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    registry = ToolRegistry(repo_dir=repo, drive_root=data)
    ctx = registry._ctx
    task_id = "stop-root"
    _seed_acceptance_root(data, task_id, ctx)
    ctx.is_direct_chat = False
    ctx.task_attempt = 1
    ctx.current_chat_id = 1
    result, usage, trace = loop.run_llm_loop(
        messages=[{"role": "user", "content": "Ship the export endpoint."}],
        tools=registry, llm=SimpleNamespace(default_model=lambda: "test-model"),
        drive_logs=data / "logs", emit_progress=lambda _text, *, incident=None: None,
        incoming_messages=queue.Queue(), task_id=task_id, drive_root=data,
    )
    return SimpleNamespace(result=result, usage=usage, trace=trace, panels=panels,
                           model_inputs=model_inputs, teardowns=teardowns["count"], ctx=ctx)


def _stop_tool_call():
    return {"content": None, "tool_calls": [{
        "id": "stop-1", "type": "function",
        "function": {"name": "task_acceptance_review", "arguments": json.dumps({
            "claim": STOP_TEXT, "author_action": "stop", "rationale": STOP_RATIONALE,
        })},
    }]}


def test_a_stop_survives_the_service_teardown_round_and_buys_no_panel(tmp_path, monkeypatch):
    """(a) stop → a running service → the host stops it at delivery → one more
    round between the cleanup and the panel → recorded ``author_stop``, no
    panel, not Done, and the agent's rationale in the row's reason slot."""
    from ouroboros.outcomes import derive_loop_outcome
    from ouroboros.project_dialogue import _completion_verdict, completion_status_label

    run = _run_stop_loop(tmp_path, monkeypatch, [
        _stop_tool_call(),
        # The nomination stopped the service; the evidence changed; the host armed a
        # replacement round. Main restates the same unfinished stop.
        json.dumps({"delivery_control": "replace", "full_answer": STOP_TEXT}),
        STOP_TEXT,
    ], stop_services=True)
    assert run.teardowns >= 1
    assert [event["kind"] for event in run.trace.get("verification_events") or []] == ["services_stopped"]
    assert any("Task services were finalized before acceptance" in note
               for note in run.trace.get("reasoning_notes") or [])
    assert run.panels == [], "an explicit stop must never buy a reviewer panel"
    decision = run.trace["acceptance_decision"]
    assert decision["reason"] == "author_stop" and decision["author_action"] == "stop"
    assert decision["author_disposition"]["action"] == "stop"
    assert decision["author_disposition"]["rationale"] == STOP_RATIONALE
    assert run.result == STOP_TEXT
    outcome = derive_loop_outcome(run.result, run.usage, run.trace)
    axes = outcome["outcome_axes"]
    assert axes["objective"]["status"] == "fail" and axes["objective"]["reason"] == "author_stop"
    record = {"status": "completed", "reason_code": str(outcome.get("reason_code") or "final_message"),
              "outcome_axes": axes}
    assert completion_status_label(record, {}) == "Failed"
    verdict = _completion_verdict(record, {})
    assert STOP_RATIONALE.rstrip(".") in verdict, verdict
    assert verdict.startswith("Ouroboros stopped with unfinished work")


def test_the_same_teardown_round_still_buys_the_panel_for_a_finish(tmp_path, monkeypatch):
    """The control: a FINISH over changed evidence is not honoured — the panel runs."""
    run = _run_stop_loop(tmp_path, monkeypatch, [
        {"content": None, "tool_calls": [{
            "id": "finish-1", "type": "function",
            "function": {"name": "task_acceptance_review", "arguments": json.dumps({
                "claim": "The export endpoint ships.", "agent_disposition": "accepted",
                "author_action": "finish", "rationale": "Everything verified.",
            })},
        }]},
        json.dumps({"delivery_control": "replace", "full_answer": "The export endpoint ships."}),
        "The export endpoint ships.",
        "The export endpoint ships.",
    ], stop_services=True)
    assert run.panels, "a finish bound to stale evidence must still be reviewed"
    assert run.trace["acceptance_decision"]["reason"] != "author_stop"


def _finality_pass(tmp_path, monkeypatch):
    """The reviewer-bound host pass over a fake tool ctx (as test_review_author_finality)."""
    import ouroboros.loop as loop_mod
    import ouroboros.loop_acceptance_review as review
    from tests.test_loop_acceptance_gate import _seed_acceptance_root

    monkeypatch.setattr(loop_mod, "get_task_review_mode", lambda: "required")
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "3")
    monkeypatch.setattr(loop_mod, "get_review_enforcement", lambda: "advisory")
    tools_ctx = SimpleNamespace(_task_acceptance_reviewed=False, is_direct_chat=False,
                               drive_root=str(tmp_path), _owner_directives=[])
    _seed_acceptance_root(tmp_path, "author-root", tools_ctx)
    tools = SimpleNamespace(_ctx=tools_ctx)
    trace = {"tool_calls": [{"tool": "write_file", "args": {"path": "answer.txt"}}]}
    messages = [{"role": "user", "content": "Solve the task."}]
    calls: list = []

    def panel(ctx):
        calls.append(ctx.content)
        return _fail_panel_result()

    monkeypatch.setattr(loop_mod, "_execute_task_acceptance_panel", panel)

    def run(text):
        return review._run_task_acceptance_review_once(
            tools=tools, content=text, task_id="author-root", task_type="task", llm_trace=trace,
            drive_root=tmp_path, messages=messages, emit_progress=lambda *_a, **_k: None)

    return SimpleNamespace(ctx=tools_ctx, trace=trace, messages=messages, calls=calls, run=run)


def test_a_later_explicit_finish_replaces_the_stop(tmp_path, monkeypatch):
    """(b) stop → later explicit finish → finish (the existing stance merge)."""
    from ouroboros.acceptance_settlement import expose_acceptance_feedback
    from ouroboros.loop_acceptance import merge_agent_acceptance_stance

    fx = _finality_pass(tmp_path, monkeypatch)
    assert fx.run("initial answer") is True
    expose_acceptance_feedback(fx.trace, fx.messages, "author-root")
    fx.trace["tool_calls"].append({"tool": "task_acceptance_review", "args": {}})
    merge_agent_acceptance_stance(fx.trace, {"explicit_finish": True, "author_action": "stop",
                                             "rationale": STOP_RATIONALE}, fx.ctx)
    assert fx.trace["acceptance_decision"]["author_action"] == "stop"
    fx.trace["tool_calls"].append({"tool": "task_acceptance_review", "args": {}})
    merge_agent_acceptance_stance(fx.trace, {"disposition": "partial", "explicit_finish": True,
                                             "author_action": "finish",
                                             "rationale": "Fixed the empty-input case after all."}, fx.ctx)
    assert fx.run("revised answer") is False
    decision = fx.trace["acceptance_decision"]
    assert decision["reason"] == "author_finish" and decision["author_action"] == "finish"
    assert decision["author_disposition"]["action"] == "finish"
    assert fx.calls == ["initial answer"]


@pytest.mark.parametrize("change", ["tools", "owner_directives", "evidence"])
def test_the_three_freshness_facts_bind_a_finish_but_never_a_stop(tmp_path, monkeypatch, change):
    from ouroboros.acceptance_settlement import expose_acceptance_feedback
    from ouroboros.loop_acceptance import merge_agent_acceptance_stance

    fx = _finality_pass(tmp_path, monkeypatch)
    assert fx.run("initial answer") is True
    expose_acceptance_feedback(fx.trace, fx.messages, "author-root")
    fx.trace["tool_calls"].append({"tool": "task_acceptance_review", "args": {}})
    merge_agent_acceptance_stance(fx.trace, {"explicit_finish": True, "author_action": "stop",
                                             "rationale": STOP_RATIONALE}, fx.ctx)
    if change == "tools":
        fx.trace["tool_calls"].append({"tool": "write_file", "args": {"path": "later.txt"}})
    elif change == "owner_directives":
        fx.ctx._owner_directives.append({"source": "owner_mailbox", "content": "any news?"})
    else:
        fx.trace["verification_events"] = [{"kind": "services_stopped",
                                            "services": [{"service_id": "preview", "lifecycle": "stopped"}]}]
    assert fx.run(STOP_TEXT) is False
    decision = fx.trace["acceptance_decision"]
    assert decision["reason"] == "author_stop" and decision["author_action"] == "stop"
    assert decision["author_disposition"]["rationale"] == STOP_RATIONALE
    assert fx.calls == ["initial answer"], "no second panel for a stop"


def test_the_row_reason_slot_carries_the_stop_rationale():
    """The typed stop sentence, then the agent's own reason; a reviewer rationale stays off the row."""
    from ouroboros.project_dialogue import TASK_CAUSE_PHRASES, _completion_verdict, completion_status_label

    record = {"status": "completed", "reason_code": "final_message", "outcome_axes": {
        "execution": {"status": "ok"},
        "objective": {"status": "fail", "source": "task_acceptance_review", "reason": "author_stop",
                      "outcome_tier": "blocked_with_evidence"},
        "review": {"status": "skipped", "acceptance_decision": {
            "status": "finalized_unaccepted", "reason": "author_stop", "author_action": "stop",
            "enforcement": "advisory", "rationale": "reviewer prose that must not reach the row",
            "author_disposition": {"disposition": "", "action": "stop", "rationale": STOP_RATIONALE,
                                   "subject_hash": "s1", "reviewer_signal": "", "enforcement": "advisory",
                                   "recorded_at": "2026-09-25T00:00:00+00:00", "source": "author"}}}}}
    assert completion_status_label(record, {}) == "Failed"
    verdict = _completion_verdict(record, {})
    assert verdict == TASK_CAUSE_PHRASES["author_stop"][:-1] + " · " + STOP_RATIONALE
    assert "reviewer prose" not in verdict
    # No rationale recorded (a malformed or stale disposition): the typed sentence alone.
    record["outcome_axes"]["review"]["acceptance_decision"]["author_disposition"] = ""
    assert _completion_verdict(record, {}) == TASK_CAUSE_PHRASES["author_stop"]
