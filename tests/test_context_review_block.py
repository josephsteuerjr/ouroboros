"""The ``## Review`` block: the review POOL a task's settings snapshot serves, in both context paths (#1548, PR-3).

It is read through the builder every review surface uses (``review_pool_slots``), so the mind sees the pool its own
commit, plan and acceptance review would run — never a last-execution receipt and never settings saved after the
task started. The pool is the catalog's marked rows (contract §1.3): there is no default panel any more.
"""
from __future__ import annotations

import json
import os
import sys
import types

import pytest

from ouroboros.configured_subagents import SUBAGENTS_SETTING, roster_handles
from ouroboros.settings_integrity import task_settings_snapshot
from ouroboros.subagent_runtime import (
    COST_UNKNOWN_HINT,
    SESSION_SEAT_COST_HINT,
    review_facts_block,
    review_records_block,
)
from tests.test_doc_context import _make_env_and_memory

_HEADER = "## Review\n\n"


def _row(subagent_id: str, target: str, *, kind: str = "api_model", effort: str = "", marked: bool = True,
         **extra) -> dict:
    row = {"subagent_id": subagent_id, "recommended_use": "Reviews diffs.",
           "route": {"kind": kind, "target_id": target}, **extra}
    if effort:
        row["effort"] = effort
    if marked:
        row["review_eligible"] = True
    return row


def _roster(*rows: dict, enabled: bool = True) -> str:
    return json.dumps({"enabled": enabled, "items": list(rows)})


_POOL = _roster(
    _row("critic-key", "openai/gpt-5.6-terra", effort="medium"),
    _row("packet-key", "openai/gpt-5.6-sol", delivery="packet"),
    _row("session-key", "codex=gpt-5.6-sol", kind="agent_session", effort="high"),
    _row("helper-key", "openai/gpt-5.6-luna", marked=False),  # a free helper is never the panel
)


def _decode(text: str) -> dict:
    """The block inside a context part; it may sit between other sections."""
    assert _HEADER in text, "the ## Review block is missing"
    block, _end = json.JSONDecoder().raw_decode(text.split(_HEADER, 1)[1])
    return block


def _block(snapshot=None) -> tuple[dict, str]:
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
def _clean_review_plane(monkeypatch):
    for key in ("OUROBOROS_REVIEW_MODELS", "OUROBOROS_SCOPE_REVIEW_MODELS", "OUROBOROS_SCOPE_REVIEW_MODEL",
                "OUROBOROS_REVIEW_ENFORCEMENT", "OUROBOROS_REVIEWER_SLOTS", SUBAGENTS_SETTING):
        monkeypatch.delenv(key, raising=False)
    # No ledger reader is the graceful case; a test that needs records installs one.
    monkeypatch.setitem(sys.modules, "ouroboros.review_ledger", None)


def _handles() -> dict[str, str]:
    from ouroboros.configured_subagents import parse_configured_subagents

    return roster_handles(parse_configured_subagents(os.environ[SUBAGENTS_SETTING]), dict(os.environ))


def test_the_pool_is_the_marked_rows_named_by_their_catalog_handle(tmp_path, monkeypatch):
    monkeypatch.setenv(SUBAGENTS_SETTING, _POOL)
    block, text = _block()
    handles = _handles()

    assert (block["source"], block["error"], block["pool_empty"]) == ("structured", "", False)
    first, second, third = block["pool"]
    # seat_id is the stored row id (the identity the records carry); subagent_id
    # is the roster handle ``## Available subagents`` shows.
    assert first == {"seat_id": "critic-key", "subagent_id": handles["critic-key"], "model": "openai/gpt-5.6-terra",
                     "effort": "medium", "delivery": "native", "cost_hint": first["cost_hint"]}
    assert second == {"seat_id": "packet-key", "subagent_id": handles["packet-key"], "model": "openai/gpt-5.6-sol",
                      "effort": "high", "delivery": "packet", "cost_hint": second["cost_hint"]}
    assert third == {"seat_id": "session-key", "subagent_id": handles["session-key"], "model": "codex=gpt-5.6-sol",
                     "effort": "high", "delivery": "session", "cost_hint": SESSION_SEAT_COST_HINT}
    assert "helper-key" not in text and "gpt-5.6-luna" not in text, "an unmarked row is not a reviewer"
    # An api seat's hint is the wave's own estimate or an honest unknown — never a price table.
    for row in (first, second):
        assert row["cost_hint"] == COST_UNKNOWN_HINT or row["cost_hint"].startswith("≈$")
    # The preflight and /review seat whichever enabled catalog row is named per call
    # (decision 3A): the block names that rule, not a second panel.
    assert "catalog row" in block["surfaces"]["preflight"] and "Main" in block["surfaces"]["system_review"]
    assert block["omitted"] == {"rows": 0} and "recent_records" not in block
    assert block["full_source"] == {"pool": "GET /api/review-pool", "records": "## Review records"}
    assert _records(tmp_path) == {"recent_records": [], "omitted": {"records": 0}, "full_source": "state/review_ledger/"}


def test_the_pool_ignores_the_catalog_switch_and_reads_only_enabled_rows(monkeypatch):
    # Delegation off (``enabled: false``) does not switch review off (F6); a
    # row's own ``enabled: false`` does take it out of the pool.
    monkeypatch.setenv(SUBAGENTS_SETTING, _roster(
        _row("critic-key", "openai/gpt-5.6-terra", effort="medium"),
        _row("retired-key", "openai/gpt-5.6-sol", enabled=False),
        enabled=False,
    ))
    block, _ = _block()
    assert block["source"] == "structured"
    assert [row["seat_id"] for row in block["pool"]] == ["critic-key"]


def test_a_catalog_with_no_marked_row_is_an_empty_pool_a_loud_fact_not_a_default_panel(monkeypatch):
    monkeypatch.setenv(SUBAGENTS_SETTING, _roster(_row("helper-key", "openai/gpt-5.6-luna", marked=False)))
    block, text = _block()

    assert (block["source"], block["error"], block["pool"], block["pool_empty"]) == ("empty", "", [], True)
    assert "default" not in (block["source"], text.split('"rule"')[0]), "no shipped panel is implied"
    assert block["rule"] and block["surfaces"]["commit_gate"].startswith("every pool row")
    # No catalog at all is the same empty pool, not an error.
    monkeypatch.delenv(SUBAGENTS_SETTING)
    assert _block()[0]["source"] == "empty"


def test_an_invalid_catalog_is_an_error_with_an_empty_pool_not_an_absent_one(monkeypatch):
    from ouroboros.reviewer_slot_config import review_pool_state

    monkeypatch.setenv(SUBAGENTS_SETTING, '{"enabled": true, "items": [{"subagent_id": "x"}]}')
    block, _ = _block()

    assert block["source"] == "error"
    assert block["error"] == review_pool_state(os.environ[SUBAGENTS_SETTING])["error"] and block["error"]
    assert (block["pool"], block["pool_empty"]) == ([], False)
    assert block["rule"] and block["surfaces"]["plan_review"] == "every pool row"


def test_the_block_never_reads_the_last_execution(monkeypatch):
    from ouroboros import reviewer_slot_config

    def forbidden(*_args, **_kwargs):
        raise AssertionError("## Review must not read reviewer_slots_last")

    monkeypatch.setattr(reviewer_slot_config, "reviewer_slot_last_executions", forbidden)
    monkeypatch.setattr(reviewer_slot_config, "_last_execution_path", forbidden)
    monkeypatch.setenv(SUBAGENTS_SETTING, _POOL)
    block, text = _block()
    assert block["source"] == "structured" and "reviewer_slots_last" not in text


def test_the_block_reads_the_task_snapshot_not_settings_saved_after_the_task_started(tmp_path, monkeypatch):
    from ouroboros import context
    from ouroboros.config import load_settings
    from ouroboros.settings_integrity import task_settings_scope

    env, memory = _make_env_and_memory(tmp_path)
    frozen = _roster(_row("critic-key", "openai/snapshot-model"))
    # A task snapshot carries the document AND its projected environment as
    # they were at task start; the catalog is a document key.
    snapshot = task_settings_snapshot({**load_settings(), SUBAGENTS_SETTING: frozen},
                                      {**os.environ, SUBAGENTS_SETTING: frozen})
    monkeypatch.setenv(SUBAGENTS_SETTING, _roster(_row("critic-key", "openai/live-model")))
    with task_settings_scope(snapshot):
        core = context._capture_context_core(env, memory, {"id": "task-1", "type": "task", "text": "w"}, None, None)

    assert [row["model"] for row in _decode(core.semi_stable_text)["pool"]] == ["openai/snapshot-model"]
    assert "openai/live-model" not in core.semi_stable_text
    assert _block(snapshot)[0]["pool"][0]["model"] == "openai/snapshot-model"
    assert _block()[0]["pool"][0]["model"] == "openai/live-model"


def test_both_context_paths_carry_the_block(tmp_path, monkeypatch):
    from ouroboros import context

    monkeypatch.setenv(SUBAGENTS_SETTING, _POOL)
    env, memory = _make_env_and_memory(tmp_path)
    shared = context._capture_context_core(env, memory, {"id": "root", "type": "task", "text": "w"}, None, None)
    declared = context._capture_context_core(env, memory, {
        "id": "child", "type": "task", "delegation_role": "subagent", "text": "Q",
        "configured_subagent": {"route": {"kind": "api_model"}}, "task_contract": {"input_sources": "declared"},
    }, None, None)

    assert _decode(shared.semi_stable_text)["source"] == "structured"
    assert _decode(declared.semi_stable_text) == _decode(shared.semi_stable_text)
    assert _HEADER not in shared.dynamic_text + declared.dynamic_text
    # The task's records are a changing fact: both paths carry them in the dynamic part only.
    assert _RECORDS_HEADER in shared.dynamic_text and _RECORDS_HEADER in declared.dynamic_text
    assert _RECORDS_HEADER not in shared.semi_stable_text + declared.semi_stable_text


def test_root_and_child_acceptance_differ_and_a_seat_id_or_handle_is_the_child_selector(monkeypatch):
    from ouroboros.reviewer_slot_config import child_acceptance_slots, review_pool_slots

    monkeypatch.setenv(SUBAGENTS_SETTING, _roster(
        _row("sol-key", "openai/gpt-5.6-sol"), _row("terra-key", "openai/gpt-5.6-terra"),
    ))
    block, _ = _block()
    acceptance = block["surfaces"]["task_acceptance"]
    slots = review_pool_slots()

    assert acceptance["root"] != acceptance["child"] and "name one" in acceptance["child"]
    assert child_acceptance_slots(slots)[1]["reason"] == "reviewer_selection_required"
    for seat in block["pool"]:
        for selector in (seat["seat_id"], seat["subagent_id"]):
            chosen, refusal = child_acceptance_slots(slots, selector)
            assert not refusal and [slot.slot_id for slot in chosen] == [seat["seat_id"]]


@pytest.mark.parametrize("enforcement, mode, blocks, opening", [
    ("blocking", "pro", True, "Blocking:"),
    ("advisory", "pro", False, "Advisory:"),
    ("blocking", "cyber_pro", False, "Cyber Pro:"),
    ("advisory", "cyber_pro", False, "Cyber Pro:"),
])
def test_one_rule_sentence_per_effective_authority(monkeypatch, enforcement, mode, blocks, opening):
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", enforcement)
    monkeypatch.setattr("ouroboros.config.get_runtime_mode", lambda: mode)
    block, _ = _block()

    assert (block["enforcement"], block["mode"], block["enforcement_blocks"]) == (enforcement, mode, blocks)
    assert block["rule"].startswith(opening) and block["rule"].count(".") == 1
    if opening == "Advisory:":  # the handback before Git effects, never an automatic commit (DEVELOPMENT 05)
        assert "before any Git effect" in block["rule"] and "continue explicitly" in block["rule"]
        assert "commit proceeds" not in block["rule"]


def test_a_fourteen_seat_pool_shrinks_its_rows_and_stays_within_four_kilobytes(tmp_path, monkeypatch):
    keys = [f"{'k' * 60}{index:04d}" for index in range(14)]
    monkeypatch.setenv(SUBAGENTS_SETTING, _roster(*[
        _row(key, f"openai/{'m' * 53}{index:04d}") for index, key in enumerate(keys)]))
    _ledger(monkeypatch, lambda drive_root, task_id="", limit=20, hot_only=False: [
        {"record_id": f"rv-{'r' * 40}-{index:02d}", "surface": "commit_gate",
         "ts": f"2026-10-07T12:{index:02d}:00.000000+00:00", "verdict": {"aggregate": "QUORUM_FAILED"}}
        for index in range(7)][:int(limit)])
    block, text = _block()
    rows = block["pool"]

    assert len(text.encode("utf-8")) <= 4096
    assert block["omitted"] == {"rows": 14} and "recent_records" not in block
    records = _records(tmp_path)
    assert records["omitted"]["records"] == "1+" and len(records["recent_records"]) == 5
    assert len(rows) == 14 and all(set(row) == {"seat_id", "model"} and len(row["seat_id"]) == 64 for row in rows)
    assert block["full_source"]["pool"] == "GET /api/review-pool"


def test_a_refused_migration_is_the_blocks_error_with_its_snapshot(monkeypatch):
    """The A↔C seam: package C reports the slots→pool migration through
    ``config.review_pool_migrations_seen()``; a refusal is an error here even
    over a readable catalog, and the snapshot path is disclosed."""
    from ouroboros import config as cfg

    monkeypatch.setenv(SUBAGENTS_SETTING, _POOL)
    monkeypatch.setattr(cfg, "review_pool_migrations_seen", lambda: {
        "error": "slots-to-pool migration refused: lane row t2 names no catalog row",
        "snapshot": "state/review_migrations/20261007-slots-to-pool.json"}, raising=False)
    block, _ = _block()
    assert block["source"] == "error" and block["error"].startswith("slots-to-pool migration refused")
    assert block["migration_snapshot"] == "state/review_migrations/20261007-slots-to-pool.json"
    assert [row["seat_id"] for row in block["pool"]] == ["critic-key", "packet-key", "session-key"]


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
