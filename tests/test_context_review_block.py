"""The ``## Review`` block: the review lanes a task's settings snapshot serves, in both context paths (#1548).

It is read through the resolver every review surface uses, so the mind sees the panel its own commit, plan and
acceptance review would run — never a last-execution receipt and never settings saved after the task started.
"""
from __future__ import annotations

import json
import os
import sys
import types

import pytest

from ouroboros.reviewer_slot_config import REVIEWER_SLOTS_ENV, load_reviewer_slot_config, reviewer_slot_config_error
from ouroboros.settings_integrity import task_settings_snapshot
from ouroboros.subagent_runtime import review_facts_block, review_records_block
from tests.test_doc_context import _make_env_and_memory

_HEADER = "## Review\n\n"
_ROSTER = {"enabled": True, "items": [{
    "subagent_id": "critic-key", "name": "API critic", "recommended_use": "Reviews diffs.",
    "route": {"kind": "api_model", "target_id": "openai/gpt-5.6-terra"}, "effort": "medium",
}]}


def _payload(triad_model: str = "openai/gpt-5.6-sol", *, triad=None, scope=None) -> str:
    return json.dumps({
        "triad": triad or [{"slot_id": "t1", "route": {"kind": "api_chat", "target_id": triad_model}}],
        "scope": scope or [{"slot_id": "s1", "route": {"kind": "api_chat", "target_id": "openai/gpt-5.6-terra"}}],
    })


def _decode(text: str) -> dict:
    """The block inside a context part; it may sit between other sections."""
    assert _HEADER in text, "the ## Review block is missing"
    block, _end = json.JSONDecoder().raw_decode(text.split(_HEADER, 1)[1])
    return block


def _block(tmp_path, snapshot=None, task_id: str = "task-1") -> tuple[dict, str]:
    text = review_facts_block(snapshot)
    assert text.startswith(_HEADER)
    return _decode(text), text


_RECORDS_HEADER = "## Review records\n\n"


def _records(tmp_path, task_id: str = "task-1") -> dict:
    """The changing-part block: this task's recent ledger records, never inside the cached prefix."""
    text = review_records_block(drive_root=tmp_path, task_id=task_id)
    assert text.startswith(_RECORDS_HEADER)
    block, _end = json.JSONDecoder().raw_decode(text.split(_RECORDS_HEADER, 1)[1])
    return block


def _ledger(monkeypatch, recent_records, archived_segments_exist=lambda drive_root: False) -> None:
    module = types.ModuleType("ouroboros.review_ledger")
    module.recent_records = recent_records
    module.archived_segments_exist = archived_segments_exist
    monkeypatch.setitem(sys.modules, "ouroboros.review_ledger", module)


@pytest.fixture(autouse=True)
def _shipped_review_models(monkeypatch):
    for key in ("OUROBOROS_REVIEW_MODELS", "OUROBOROS_SCOPE_REVIEW_MODELS", "OUROBOROS_SCOPE_REVIEW_MODEL",
                "OUROBOROS_REVIEW_ENFORCEMENT"):
        monkeypatch.delenv(key, raising=False)
    # No ledger reader is the graceful case; a test that needs records installs one.
    monkeypatch.setitem(sys.modules, "ouroboros.review_ledger", None)


def test_a_structured_panel_names_reference_rows_by_their_catalog_handle(tmp_path, monkeypatch):
    from ouroboros.config import resolve_effort
    from ouroboros.subagent_runtime import current_model_visible_subagent_catalog

    monkeypatch.setenv("OUROBOROS_SUBAGENTS", json.dumps(_ROSTER))
    monkeypatch.setenv(REVIEWER_SLOTS_ENV, _payload(triad=[
        {"slot_id": "t1", "subagent_id": "critic-key"},
        {"slot_id": "t2", "route": {"kind": "api_chat", "target_id": "openai/gpt-5.6-sol"}, "delivery": "packet"},
        {"slot_id": "t3", "route": {"kind": "agent_session", "target_id": "codex=gpt-5.6-sol"}, "effort": "high"},
    ]))
    block, text = _block(tmp_path)
    handle = current_model_visible_subagent_catalog()["rows"][0]["subagent_id"]

    assert (block["source"], block["error"]) == ("structured", "")
    first, second, third = block["panel"]["triad"]
    assert first == {"seat_id": "t1", "subagent_id": handle, "model": "openai/gpt-5.6-terra",
                     "effort": "medium", "delivery": "native"}
    assert second == {"seat_id": "t2", "route": "api_chat", "model": "openai/gpt-5.6-sol",
                      "effort": resolve_effort("review"), "delivery": "packet"}
    assert third == {"seat_id": "t3", "route": "agent_session", "model": "codex=gpt-5.6-sol",
                     "effort": "high", "delivery": "agent_session"}
    assert block["panel"]["scope"] == [{"seat_id": "s1", "route": "api_chat", "model": "openai/gpt-5.6-terra",
                                        "effort": resolve_effort("scope_review"), "delivery": "native"}]
    assert "critic-key" not in text, "the stored key is not model-facing"
    assert block["panel"]["deep_review"]["seat_id"] == "deep_review_slot_1"
    assert block["omitted"] == {"rows": 0} and "recent_records" not in block
    assert _records(tmp_path) == {"recent_records": [], "omitted": {"records": 0}, "full_source": "state/review_ledger/"}


def test_the_factory_panel_shows_the_models_it_runs(tmp_path):
    from ouroboros.tools.claude_advisory_review import _advisory_native_model

    block, text = _block(tmp_path)
    config = load_reviewer_slot_config()
    seats = block["panel"]["triad"] + block["panel"]["scope"]

    assert block["source"] == config.source == "default"
    assert [row["model"] for row in block["panel"]["triad"]] == [slot.target_id for slot in config.triad]
    assert [row["model"] for row in block["panel"]["scope"]] == [slot.target_id for slot in config.scope]
    advisory, deep = block["panel"]["advisory"], block["panel"]["deep_review"]
    assert advisory["enabled"] is True and advisory["model"] == _advisory_native_model(config.advisory)
    assert all(row["model"] not in ("", "route default") for row in [*seats, advisory, deep])
    # A panel of at most four seats keeps every row whole inside the two-kilobyte target.
    assert 0 < len(seats) <= 4 and block["omitted"]["rows"] == 0
    assert all(set(row) == {"seat_id", "route", "model", "effort", "delivery"} for row in seats)
    assert len(text.encode("utf-8")) <= 2048


def test_an_invalid_panel_is_an_error_with_an_empty_panel_not_an_absent_one(tmp_path, monkeypatch):
    monkeypatch.setenv(REVIEWER_SLOTS_ENV, json.dumps({"triad": [], "scope": []}))
    block, _ = _block(tmp_path)

    assert block["source"] == "error"
    assert block["error"] == reviewer_slot_config_error() and "triad needs at least one slot" in block["error"]
    assert block["panel"] == {"triad": [], "scope": [], "advisory": None, "deep_review": None}
    assert block["rule"] and block["surfaces"]["commit_gate"] == ["triad", "scope"]


def test_the_block_never_reads_the_last_execution(tmp_path, monkeypatch):
    from ouroboros import reviewer_slot_config

    def forbidden(*_args, **_kwargs):
        raise AssertionError("## Review must not read reviewer_slots_last")

    monkeypatch.setattr(reviewer_slot_config, "reviewer_slot_last_executions", forbidden)
    monkeypatch.setattr(reviewer_slot_config, "_last_execution_path", forbidden)
    block, text = _block(tmp_path)
    assert block["source"] == "default" and "reviewer_slots_last" not in text


def test_the_block_reads_the_task_snapshot_not_settings_saved_after_the_task_started(tmp_path, monkeypatch):
    from ouroboros import context
    from ouroboros.config import load_settings
    from ouroboros.settings_integrity import task_settings_scope

    env, memory = _make_env_and_memory(tmp_path)
    snapshot = task_settings_snapshot(dict(load_settings()),
                                      {**os.environ, REVIEWER_SLOTS_ENV: _payload("openai/snapshot-model")})
    monkeypatch.setenv(REVIEWER_SLOTS_ENV, _payload("openai/live-model"))
    with task_settings_scope(snapshot):
        core = context._capture_context_core(env, memory, {"id": "task-1", "type": "task", "text": "w"}, None, None)

    assert [row["model"] for row in _decode(core.semi_stable_text)["panel"]["triad"]] == ["openai/snapshot-model"]
    assert "openai/live-model" not in core.semi_stable_text
    assert _block(tmp_path, snapshot)[0]["panel"]["triad"][0]["model"] == "openai/snapshot-model"
    assert _block(tmp_path)[0]["panel"]["triad"][0]["model"] == "openai/live-model"


def test_both_context_paths_carry_the_block(tmp_path):
    from ouroboros import context

    env, memory = _make_env_and_memory(tmp_path)
    shared = context._capture_context_core(env, memory, {"id": "root", "type": "task", "text": "w"}, None, None)
    declared = context._capture_context_core(env, memory, {
        "id": "child", "type": "task", "delegation_role": "subagent", "text": "Q",
        "configured_subagent": {"route": {"kind": "api_model"}}, "task_contract": {"input_sources": "declared"},
    }, None, None)

    assert _decode(shared.semi_stable_text)["source"] == "default"
    assert _decode(declared.semi_stable_text) == _decode(shared.semi_stable_text)
    assert _HEADER not in shared.dynamic_text + declared.dynamic_text
    # The task's records are a changing fact: both paths carry them in the dynamic part only.
    assert _RECORDS_HEADER in shared.dynamic_text and _RECORDS_HEADER in declared.dynamic_text
    assert _RECORDS_HEADER not in shared.semi_stable_text + declared.semi_stable_text


def test_root_and_child_acceptance_differ_and_a_seat_id_is_the_child_selector(tmp_path, monkeypatch):
    from ouroboros.reviewer_slot_config import child_acceptance_slots, triad_delivery_slots

    monkeypatch.setenv(REVIEWER_SLOTS_ENV, _payload(triad=[
        {"slot_id": "t1", "route": {"kind": "api_chat", "target_id": "openai/gpt-5.6-sol"}},
        {"slot_id": "t2", "route": {"kind": "api_chat", "target_id": "openai/gpt-5.6-terra"}},
    ]))
    block, _ = _block(tmp_path)
    acceptance = block["surfaces"]["task_acceptance"]
    slots = triad_delivery_slots()

    assert acceptance["root"] != acceptance["child"] and "reviewer_slot_id" in acceptance["child"]
    assert child_acceptance_slots(slots)[1]["reason"] == "reviewer_selection_required"
    for seat in block["panel"]["triad"]:
        chosen, refusal = child_acceptance_slots(slots, seat["seat_id"])
        assert not refusal and [slot.slot_id for slot in chosen] == [seat["seat_id"]]


@pytest.mark.parametrize("enforcement, mode, blocks, opening", [
    ("blocking", "pro", True, "Blocking:"),
    ("advisory", "pro", False, "Advisory:"),
    ("blocking", "cyber_pro", False, "Cyber Pro:"),
    ("advisory", "cyber_pro", False, "Cyber Pro:"),
])
def test_one_rule_sentence_per_effective_authority(tmp_path, monkeypatch, enforcement, mode, blocks, opening):
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", enforcement)
    monkeypatch.setattr("ouroboros.config.get_runtime_mode", lambda: mode)
    block, _ = _block(tmp_path)

    assert (block["enforcement"], block["mode"], block["enforcement_blocks"]) == (enforcement, mode, blocks)
    assert block["rule"].startswith(opening) and block["rule"].count(".") == 1
    if opening == "Advisory:":  # the handback before Git effects, never an automatic commit (DEVELOPMENT 05)
        assert "before any Git effect" in block["rule"] and "continue explicitly" in block["rule"]
        assert "commit proceeds" not in block["rule"]


def test_a_fourteen_seat_panel_shrinks_its_rows_and_stays_within_four_kilobytes(tmp_path, monkeypatch):
    keys = [f"{'k' * 60}{index:04d}" for index in range(10)]
    monkeypatch.setenv("OUROBOROS_SUBAGENTS", json.dumps({"enabled": True, "items": [
        {"subagent_id": key, "name": f"Critic {index}", "recommended_use": "Reviews.",
         "route": {"kind": "api_model", "target_id": f"openai/{'m' * 53}{index:04d}"}}
        for index, key in enumerate(keys)]}))
    triad = [{"slot_id": f"t{'s' * 59}{index:04d}", "subagent_id": keys[index]} for index in range(10)]
    scope = [{"slot_id": f"c{'s' * 59}{index:04d}", "subagent_id": keys[index]} for index in range(4)]
    monkeypatch.setenv(REVIEWER_SLOTS_ENV, json.dumps({"triad": triad, "scope": scope}))
    _ledger(monkeypatch, lambda drive_root, task_id="", limit=20, hot_only=False: [
        {"record_id": f"rv-{'r' * 40}-{index:02d}", "surface": "commit_gate",
         "ts": f"2026-10-07T12:{index:02d}:00.000000+00:00", "verdict": {"aggregate": "QUORUM_FAILED"}}
        for index in range(7)][:int(limit)])
    block, text = _block(tmp_path)
    rows = block["panel"]["triad"] + block["panel"]["scope"]

    assert len(text.encode("utf-8")) <= 4096
    assert block["omitted"] == {"rows": 14} and "recent_records" not in block
    records = _records(tmp_path)
    assert records["omitted"]["records"] == "1+" and len(records["recent_records"]) == 5
    assert len(rows) == 14 and all(set(row) == {"seat_id", "model"} and len(row["seat_id"]) == 64 for row in rows)
    assert all(key not in text for key in keys), "stored keys never become model-facing"
    assert block["full_source"]["panel"] == "GET /api/reviewer-slots"


def test_recent_records_are_the_readers_newest_five_of_this_task_from_a_bounded_hot_read(tmp_path, monkeypatch):
    calls = []
    newest_first = [{"record_id": f"r{index}", "surface": "commit_gate", "ts": f"2026-10-07T00:00:0{index}+00:00",
                     "verdict": {"aggregate": "FAIL" if index % 2 else "PASS"}} for index in reversed(range(7))]

    # review_ledger.recent_records' signature: the limit is an int, newest first; the context
    # capture reads the hot index only, with a six-row cap (never the whole history).
    def recent_records(drive_root, task_id="", limit=20, hot_only=False):
        calls.append((drive_root, task_id, int(limit), hot_only))
        return newest_first[:max(1, int(limit))]

    _ledger(monkeypatch, recent_records)
    block = _records(tmp_path, task_id="task-9")

    assert calls == [(tmp_path, "task-9", 6, True)]
    assert [record["record_id"] for record in block["recent_records"]] == ["r6", "r5", "r4", "r3", "r2"]
    assert block["recent_records"][:2] == [
        {"record_id": "r6", "surface": "commit_gate", "aggregate": "PASS", "ts": "2026-10-07T00:00:06+00:00"},
        {"record_id": "r5", "surface": "commit_gate", "aggregate": "FAIL", "ts": "2026-10-07T00:00:05+00:00"},
    ]
    assert block["omitted"]["records"] == "1+", "more rows than shown in the hot index: a bounded fact, not a count"
    calls.clear()
    empty = _records(tmp_path, task_id="")
    assert (empty["recent_records"], empty["omitted"]["records"], calls) == ([], 0, []), "empty selects every task"
    few = lambda drive_root, task_id="", limit=20, hot_only=False: newest_first[:3]  # noqa: E731
    _ledger(monkeypatch, few)
    assert _records(tmp_path, task_id="task-9")["omitted"]["records"] == 0, "nothing else in the hot index, no archive"
    _ledger(monkeypatch, few, archived_segments_exist=lambda drive_root: True)
    assert _records(tmp_path, task_id="task-9")["omitted"]["records"] == "unknown", "an archive may hold older records"


def test_an_unreadable_ledger_is_unknown_never_a_silent_zero(tmp_path, monkeypatch):
    def recent_records(drive_root, task_id="", limit=20, hot_only=False):
        raise OSError("index unreadable")

    _ledger(monkeypatch, recent_records)
    block = _records(tmp_path)
    assert block["recent_records"] == [] and block["omitted"]["records"] == "unknown"
