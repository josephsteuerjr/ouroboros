"""Review ledger record: one durable, honest record per authoritative review wave."""
import json
import pathlib
import shutil
from types import SimpleNamespace

import pytest

from ouroboros import review_ledger as rl
from ouroboros.contracts.schema_versions import read_schema_version
from ouroboros.review_state import load_state
from ouroboros.tools import git
from ouroboros.tools.scope_review import ScopeReviewResult
from tests.test_advisory_inline_freshness import candidate  # noqa: F401

TREE, PARENT, DIFF = "t" * 40, "p" * 40, "d" * 64


def _raw(slot_id, model, status="responded", **extra):
    row = {"slot_id": slot_id, "model_id": model, "status": status, "raw_text": "[]", "parsed_items": [], "cost_usd": 0.01}
    row.update(extra)
    return row


def _plan(slot_id, model, route="api_chat", **extra):
    return {"slot_id": slot_id, "model": model, "route": route, "effort": "high", **extra}


def _facts(triad_raw, scope_raw=None, *, triad_rows=None, scope_rows=None, task_id="task-1", **over):
    facts = {
        "task_id": task_id, "root_task_id": task_id, "repo_dir": "/nowhere", "goal": "goal", "scope": "scope",
        "enforcement": "blocking", "enforcement_blocks": True, "blocked": False, "block_reason": "",
        "triad_raw": triad_raw, "scope_raw": scope_raw if scope_raw is not None else {},
        "structured": {
            "triad_prompt": "TRIAD BRIEF", "scope_brief": "SCOPE BRIEF", "started_ts": "2026-10-07T00:00:00+00:00",
            "triad_rows": triad_rows if triad_rows is not None else [_plan(r["slot_id"], r["model_id"]) for r in triad_raw],
            "scope_rows": scope_rows if scope_rows is not None else (
                [_plan("scope", scope_raw.get("model_id", ""))] if scope_raw else []),
        },
        "binding": {"tree_sha": TREE, "parents": [PARENT], "diff_sha256": DIFF}, "binding_fingerprint": "fp",
        "review_contract_fingerprint": "cf", "review_wave_id": "wave-1",
    }
    facts.update(over)
    return facts


def _three(models=("openai/gpt-5", "anthropic/claude-x", "google/gemini"), status="responded"):
    return [_raw(f"s{i}", m, status) for i, m in enumerate(models, 1)]


def _scope(model="openai/gpt-5", status="responded", **extra):
    return {"slot_id": "scope", "status": status, "model_id": model, "raw_text": "scope answer", "raw_results": [], **extra}


# --- record shape, schema version, read by id -------------------------------


def test_record_round_trip_schema_version_and_read_by_id(tmp_path):
    record = rl.build_commit_gate_record(_facts(_three(), _scope()), drive_root=tmp_path)
    written = rl.write_record(tmp_path, record)
    assert read_schema_version(written) == 1 and written["_schema_version"] == 1
    loaded = rl.load_record(tmp_path, record.record_id)
    assert loaded == written
    again = rl.ReviewLedgerRecord.from_dict(loaded)
    assert again.record_id == record.record_id and again.surface == "commit_gate"
    assert loaded["subject"]["tree_sha"] == TREE and loaded["subject"]["base"] == PARENT and loaded["subject"]["diff_sha"] == DIFF
    assert loaded["brief"]["parts"] == ["change", "coupling"] and loaded["brief"]["checklist"]["body_fact"] == "unknown"
    assert loaded["brief"]["checklist"]["how"] == "unknown"
    assert loaded["panel"]["composition"] == "configured" and loaded["panel"]["chosen_by"] == "owner"
    fingerprints = loaded["fingerprints"]
    assert fingerprints["review_contract"] == "cf" and fingerprints["binding"] == "fp"
    # The reuse key is derived from the record's own fields when the gate hook gives none;
    # the gate's retry key rides the structured facts into the record.
    assert len(fingerprints["reuse_key"]) == 64 and fingerprints["retry_key"] == ""
    assert set(fingerprints) == {"review_contract", "binding", "reuse_key", "retry_key"}
    assert rl.recent_records(tmp_path, "task-1", 5)[0]["reuse_key"] == fingerprints["reuse_key"]
    assert loaded["review_wave_id"] == "wave-1" and loaded["state"] == "settled"
    assert json.loads(rl.record_path(tmp_path, record.record_id).read_text(encoding="utf-8")) == loaded
    assert rl.load_record(tmp_path, "rl-missing") is None and rl.load_record(tmp_path, "../evil") is None
    rows = rl.recent_records(tmp_path, "task-1", 5)
    assert [r["record_id"] for r in rows] == [record.record_id]
    assert rows[0]["source_ref"]["path"] == f"state/review_ledger/{record.record_id}.json"
    assert rl.recent_records(tmp_path, "other-task", 5) == []


def test_corrupt_record_file_is_unreadable_never_absent(tmp_path):
    record = rl.build_commit_gate_record(_facts(_three(), _scope()), drive_root=tmp_path)
    rl.write_record(tmp_path, record)
    path = rl.record_path(tmp_path, record.record_id)
    path.write_text("{not a record", encoding="utf-8")
    with pytest.raises(ValueError, match="exists but is not readable"):
        rl.load_record(tmp_path, record.record_id)
    path.write_text(json.dumps(["a list", "not a mapping"]), encoding="utf-8")
    with pytest.raises(ValueError):
        rl.load_record(tmp_path, record.record_id)
    path.unlink()
    assert rl.load_record(tmp_path, record.record_id) is None


def test_hot_only_reads_never_open_an_archived_segment(tmp_path):
    record = rl.build_commit_gate_record(_facts(_three(), _scope()), drive_root=tmp_path)
    rl.write_record(tmp_path, record)
    assert rl.archived_segments_exist(tmp_path) is False
    archived = rl.ledger_dir(tmp_path) / "index.20261001T000000.jsonl"
    older = dict(rl.index_row(tmp_path, rl.load_record(tmp_path, record.record_id)), record_id="rl-older-0001")
    archived.write_text(json.dumps(older) + "\n", encoding="utf-8")
    assert rl.archived_segments_exist(tmp_path) is True
    assert [r["record_id"] for r in rl.recent_records(tmp_path, "task-1", 5)] == [record.record_id, "rl-older-0001"]
    assert [r["record_id"] for r in rl.recent_records(tmp_path, "task-1", 5, hot_only=True)] == [record.record_id]


def test_record_vocabulary_is_enforced(tmp_path):
    record = rl.build_commit_gate_record(_facts(_three(), _scope()))
    bad = record.to_dict()
    bad["verdict"]["aggregate"] = "MAYBE"
    with pytest.raises(ValueError):
        rl.write_record(tmp_path, bad)
    with pytest.raises(ValueError):
        rl.write_record(tmp_path, {**record.to_dict(), "record_id": "../escape"})
    with pytest.raises(ValueError):
        rl.write_record(tmp_path, {**record.to_dict(), "state": "done"})
    with pytest.raises(ValueError):
        rl.ReviewLedgerRecord.from_dict({**record.to_dict(), "_schema_version": 2})


# --- panels and observed-model distinctness ---------------------------------


@pytest.mark.parametrize("models,distinct,single", [
    (("openai/gpt-5", "anthropic/claude-x", "google/gemini"), 3, False),
    (("openai/gpt-5", "openai/gpt-5", "openai/gpt-5"), 1, True),
    (("openai/gpt-5",), 1, True),
    # three route selectors that SERVED one model: namespace, tag and case do not make three models
    (("openrouter/openai/gpt-5", "openai/GPT-5", "gpt-5:free"), 1, True),
])
def test_panel_distinct_models_follow_observed_models(models, distinct, single):
    record = rl.build_commit_gate_record(_facts(_three(models)))
    panel = record.to_dict()["panel"]
    assert panel["seats"] == len(models) and panel["distinct_models"] == distinct
    assert panel["single_model_panel"] is single and panel["observed_unknown_seats"] == 0
    assert panel["distinct_engines"] >= 1 and panel["assigned"] == [f"s{i}" for i in range(1, len(models) + 1)]
    assert all(row["requested"]["model"] == row["observed_model"] for row in record.to_dict()["rows"])


def test_unobserved_seat_is_unknown_never_a_distinct_model():
    # an agent session reports its own model; none reported -> unknown, counted nowhere
    plans = [_plan("s1", "openai/gpt-5"), _plan("s2", "claude", route="agent_session", session_target="claude=opus"),
             _plan("s3", "openai/gpt-5")]
    record = rl.build_commit_gate_record(_facts(_three(("openai/gpt-5", "claude", "openai/gpt-5")), triad_rows=plans))
    panel, rows = record.to_dict()["panel"], record.to_dict()["rows"]
    assert rows[1]["observed_model"] == "unknown" and rows[1]["requested"]["model"] == "claude"
    assert panel["distinct_models"] == 1 and panel["observed_unknown_seats"] == 1
    assert panel["single_model_panel"] == "unknown"  # the unknown seat could be a second model
    assert rl.distinct_model_facts(["", None, "unknown"]) == {
        "distinct_models": 0, "observed_unknown_seats": 3, "single_model_panel": "unknown"}
    assert rl.distinct_model_facts([]) == {"distinct_models": 0, "observed_unknown_seats": 0, "single_model_panel": "unknown"}


def test_session_seat_observed_model_comes_from_its_execution_record():
    plans = [_plan("s1", "claude", route="agent_session", session_target="claude=opus")]
    executions = {"s1": {"ts": "2026-10-07T00:00:01+00:00", "effective": {"route": "agent_session:claude", "model": "claude-opus-5",
                                                                           "verdict_method": "structured"}}}
    facts = _facts([_raw("s1", "claude")], triad_rows=plans, slot_executions=executions)
    row = rl.build_commit_gate_record(facts).to_dict()["rows"][0]
    assert row["observed_model"] == "claude-opus-5" and row["effective"]["model"] == "claude-opus-5"
    assert row["effective"]["source"] == "reviewer_slot_last_execution" and row["requested"]["model"] == "claude"
    stale = {"s1": {**executions["s1"], "ts": "2026-10-06T00:00:00+00:00"}}  # an earlier wave's execution
    row = rl.build_commit_gate_record(_facts([_raw("s1", "claude")], triad_rows=plans, slot_executions=stale)).to_dict()["rows"][0]
    assert row["observed_model"] == "unknown" and row["effective"]["source"] == "requested"


# --- verdict reduction -----------------------------------------------------


def test_verdict_pass_fail_quorum_failed_not_dispatched_and_pending():
    passed = rl.build_commit_gate_record(_facts(_three(), _scope())).to_dict()["verdict"]
    assert passed["aggregate"] == "PASS" and passed["per_question"] == {"change": "PASS", "coupling": "PASS"}
    assert passed["quorum"] == {"required": 3, "responded": 4, "assigned": 4, "parts": {
        "change": {"required": 2, "assigned": 3, "responded": 3}, "coupling": {"required": 1, "assigned": 1, "responded": 1}}}

    rows = _three()
    rows[0]["parsed_items"] = [{"item": "bug", "verdict": "FAIL", "severity": "critical"}]
    failed = rl.build_commit_gate_record(_facts(rows, _scope(), blocked=True, block_reason="critical_findings",
                                                critical_findings=rows[0]["parsed_items"])).to_dict()["verdict"]
    assert failed["aggregate"] == "FAIL" and failed["per_question"] == {"change": "FAIL", "coupling": "PASS"}
    assert failed["per_row"]["s1"] == "FAIL" and failed["critical_findings"] == rows[0]["parsed_items"]

    scope_fail = rl.build_commit_gate_record(_facts(_three(), _scope(critical_findings=[{"item": "coupling broke"}]),
                                                    blocked=True, block_reason="scope")).to_dict()["verdict"]
    assert scope_fail["per_question"] == {"change": "PASS", "coupling": "FAIL"} and scope_fail["aggregate"] == "FAIL"

    short = _three(status="error")
    short[0]["status"] = "responded"
    quorum = rl.build_commit_gate_record(_facts(short, _scope(), blocked=True, block_reason="quorum_failed")).to_dict()["verdict"]
    assert quorum["aggregate"] == "QUORUM_FAILED" and quorum["per_question"]["change"] == "unanswered"
    assert quorum["quorum"]["parts"]["change"] == {"required": 2, "assigned": 3, "responded": 1}

    refused = rl.build_commit_gate_record(_facts([], dispatch_refusal={"kind": "identical_diff_refused", "message": "same bytes"},
                                                 blocked=True, block_reason="identical_diff_refused")).to_dict()
    assert refused["verdict"]["aggregate"] == "NOT_DISPATCHED" and refused["dispatch_refusal"]["kind"] == "identical_diff_refused"
    assert refused["verdict"]["per_question"] == {"change": "not_performed", "coupling": "not_performed"}
    assert refused["state"] == "settled" and refused["cost"] == {"usd": 0.0, "unknown": False}

    withheld = _three(status="not_dispatched")
    for row in withheld:
        row["operation_state"] = "not_dispatched"
    budget = rl.build_commit_gate_record(_facts(withheld, blocked=True, block_reason="review_wave_budget_insufficient")).to_dict()
    assert budget["verdict"]["aggregate"] == "NOT_DISPATCHED" and set(budget["verdict"]["per_row"].values()) == {"NOT_DISPATCHED"}

    pending_rows = _three()
    pending_rows[2] = _raw("s3", "google/gemini", "pending", operation_state="in_flight", late_result_pending=True)
    pending = rl.build_commit_gate_record(_facts(pending_rows, _scope())).to_dict()
    assert pending["state"] == "pending" and pending["verdict"]["aggregate"] == "NOT_PERFORMED"
    assert pending["cost"]["unknown"] is True  # the open seat has no cost yet

    # a gate that blocked never reads as PASS even with clean answers
    blocked = rl.build_commit_gate_record(_facts(_three(), _scope(), blocked=True, block_reason="owner_stopped")).to_dict()["verdict"]
    assert blocked["aggregate"] == "NOT_PERFORMED" and "gate_block:owner_stopped" in blocked["degraded_reasons"]


# --- retention, index, rotation, GC -----------------------------------------


def _ctx_on_child(root, child):
    return SimpleNamespace(drive_root=child, budget_drive_root=root, task_metadata={})


def test_sources_are_retained_before_the_index_row_and_survive_rotation_and_child_gc(tmp_path, monkeypatch):
    root, child = tmp_path / "canonical", tmp_path / "child"
    root.mkdir()
    child.mkdir()
    ctx = _ctx_on_child(root, child)
    assert rl.ledger_root(ctx) == root.resolve()
    monkeypatch.setattr(rl, "INDEX_MAX_BYTES", 1)  # every write after the first rotates the hot index
    ids = []
    for n in range(3):
        record = rl.build_commit_gate_record(_facts(_three(), _scope(), task_id="task-1"), drive_root=rl.ledger_root(ctx))
        rl.write_record(rl.ledger_root(ctx), record)
        ids.append(record.record_id)
    assert not (child / "state").exists(), "the child execution drive holds no ledger"
    segments = sorted(p.name for p in rl.ledger_dir(root).iterdir() if p.name.startswith("index.") and p.name != "index.jsonl")
    assert len(segments) == 2 and all(name.endswith(".jsonl") for name in segments)
    shutil.rmtree(child)  # child-drive GC
    rows = rl.recent_records(root, "task-1", 10)
    assert [r["record_id"] for r in rows] == list(reversed(ids))
    assert all(r["heavy_stripped"] is True and "rows" not in r for r in rows)
    for record_id in ids:
        payload = rl.load_record(root, record_id)
        refs = [ref for row in payload["rows"] for ref in row["source_refs"]]
        prompts = {ref["part"]: ref for ref in refs if ref["role"] == "prompt"}
        assert rl.read_source(root, "task-1", prompts["change"]) == b"TRIAD BRIEF"
        assert rl.read_source(root, "task-1", prompts["coupling"]) == b"SCOPE BRIEF"
        answers = [ref for ref in refs if ref["role"] == "response"]
        assert len(answers) == 4 and {rl.read_source(root, "task-1", ref) for ref in answers} == {b"[]", b"scope answer"}
        assert all(ref["ref"]["path"].startswith("source_handles/context_checkpoints/review-ledger-") for ref in refs)
        assert all(rl.source_ref_resolvable(root, "task-1", ref) for ref in refs)


def test_reuse_lookup_reads_the_hot_index_first_and_opens_the_archive_only_on_a_miss(tmp_path, monkeypatch):
    """A6: the reuse lookup is staged — the bounded hot index, then (only after a
    hot miss) the archived segments newest-first until the first match — and it
    discloses what it read."""
    monkeypatch.setattr(rl, "INDEX_MAX_BYTES", 1)  # every write after the first rotates the hot index
    keys = [f"reuse-key-{n}" for n in range(3)]
    for key in keys:  # three records → the two oldest keys live in archived segments, the newest in the hot index
        rl.write_record(tmp_path, rl.build_commit_gate_record(_facts(_three(), _scope(), reuse_key=key), drive_root=tmp_path))
    assert len(rl._index_segments(tmp_path)) == 2
    opened = []
    real_iter = rl.iter_jsonl_objects
    monkeypatch.setattr(rl, "iter_jsonl_objects", lambda path: (opened.append(pathlib.Path(path).name), real_iter(path))[1])

    lookup = {}
    hit = rl.find_reusable(tmp_path, keys[2], lookup=lookup)
    assert hit is not None and hit["fingerprints"]["reuse_key"] == keys[2]
    assert lookup == {"rows_read": 1, "archive_segments": 0} and opened == ["index.jsonl"]

    opened.clear()
    lookup = {}
    hit = rl.find_reusable(tmp_path, keys[1], lookup=lookup)  # the newest archived segment: one is opened, not both
    assert hit is not None and hit["fingerprints"]["reuse_key"] == keys[1]
    assert lookup == {"rows_read": 2, "archive_segments": 1}
    assert opened[0] == "index.jsonl" and len(opened) == 2 and opened[1] != "index.jsonl"

    opened.clear()
    lookup = {}
    assert rl.find_reusable(tmp_path, "reuse-key-of-a-new-subject", lookup=lookup) is None  # a miss reads everything, disclosed
    assert lookup == {"rows_read": 3, "archive_segments": 2} and len(opened) == 3
    assert rl.find_reusable(tmp_path, "", lookup=(lookup := {})) is None and lookup["rows_read"] == 0


def test_index_keeps_heavy_fields_for_a_row_whose_source_does_not_resolve(tmp_path, monkeypatch):
    rows = _three()
    rows[0]["parsed_items"] = [{"item": "bug", "verdict": "FAIL", "severity": "critical"}]
    record = rl.build_commit_gate_record(_facts(rows, _scope(), critical_findings=rows[0]["parsed_items"]), drive_root=tmp_path)
    payload = rl.write_record(tmp_path, record)
    hot = rl.index_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert json.loads(hot[-1])["heavy_stripped"] is True and "critical_findings" not in json.loads(hot[-1])["verdict"]
    lost = [ref for row in payload["rows"] for ref in row["source_refs"] if ref["role"] == "response"][0]
    from ouroboros.artifacts import task_artifact_dir_path
    target = task_artifact_dir_path(tmp_path, "task-1").joinpath(*pathlib.PurePosixPath(lost["ref"]["path"]).parts)
    target.unlink()
    assert not rl.record_sources_resolvable(tmp_path, payload)
    row = rl.index_row(tmp_path, payload)
    assert row["heavy_stripped"] is False and row["verdict"]["critical_findings"] == rows[0]["parsed_items"]
    assert row["rows"] == payload["rows"]
    # rotation never archives it: the row is carried into the fresh hot index
    monkeypatch.setattr(rl, "INDEX_MAX_BYTES", 1)
    rl.write_record(tmp_path, rl.build_commit_gate_record(_facts(_three(), _scope()), drive_root=tmp_path))
    kept = [json.loads(line) for line in rl.index_path(tmp_path).read_text(encoding="utf-8").splitlines()]
    assert [r["record_id"] for r in kept][0] == record.record_id
    unresolvable_record = {**payload, "record_id": "rl-never-written"}
    assert rl.index_row(tmp_path, unresolvable_record)["heavy_stripped"] is False


def test_late_settle_raises_revision_and_a_stale_snapshot_never_overwrites(tmp_path):
    rows = _three()
    rows[2] = _raw("s3", "google/gemini", "pending", operation_state="in_flight", late_result_pending=True)
    record = rl.build_commit_gate_record(_facts(rows, _scope()), drive_root=tmp_path)
    first = rl.write_record(tmp_path, record)
    assert first["revision"] == 1 and first["state"] == "pending"
    stale = {**first, "state": "settled", "verdict": {**first["verdict"], "aggregate": "PASS"}}
    assert rl.write_record(tmp_path, stale) == first, "same revision: the existing record wins unchanged"
    assert rl.load_record(tmp_path, record.record_id)["state"] == "pending"
    settled = rl.build_commit_gate_record(_facts(_three(), _scope()), record_id=record.record_id, drive_root=tmp_path).to_dict()
    revised = rl.revise_record(tmp_path, record.record_id, lambda payload: {**settled, "revision": payload["revision"], "ts": payload["ts"]})
    assert revised["revision"] == 2 and revised["state"] == "settled" and revised["verdict"]["aggregate"] == "PASS"
    assert rl.write_record(tmp_path, {**first, "revision": 1}) == revised, "a lower revision never overwrites"
    assert rl.write_record(tmp_path, {**first, "revision": 2}) == revised
    recent = rl.recent_records(tmp_path, "task-1", 5)
    assert len(recent) == 1 and recent[0]["revision"] == 2 and recent[0]["state"] == "settled"
    lines = rl.index_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["revision"] for line in lines] == [1, 2]
    assert rl.revise_record(tmp_path, "rl-absent", lambda p: p) is None
    noted = rl.note_author_decision(tmp_path, record.record_id, {"disposition": "accepted", "rationale": "ok"})
    assert noted["revision"] == 3 and noted["author_decision"] == {
        "disposition": "accepted", "rationale": "ok", "reused_record_id": record.record_id}


def test_tests_evidence_lands_only_on_the_record_of_the_tested_tree(tmp_path):
    """A hermetic test run proves ONE tree. Its fact is written on the record whose
    subject is that tree and refused (record untouched, ``None``) for any other, so
    an external runner cannot stamp a passed suite onto a different candidate."""
    record = rl.build_commit_gate_record(_facts(_three(), _scope()), drive_root=tmp_path)
    written = rl.write_record(tmp_path, record)
    assert written["subject"]["tree_sha"] == TREE and written["tests"]["policy"] != "run"
    tests = {"policy": "run", "result": "passed", "proof": "candidate_bound"}

    assert rl.attach_tests_evidence(tmp_path, record.record_id, tests=tests, tree_sha="x" * 40) is None
    assert rl.attach_tests_evidence(tmp_path, record.record_id, tests=tests, tree_sha="") is None
    assert rl.attach_tests_evidence(tmp_path, "rl-absent", tests=tests, tree_sha=TREE) is None
    assert rl.load_record(tmp_path, record.record_id) == written, "a refused attachment changes nothing"

    revised = rl.attach_tests_evidence(tmp_path, record.record_id, tests=tests, tree_sha=TREE)
    assert revised["revision"] == 2 and revised["tests"] == {**tests, "tree_sha": TREE}
    assert rl.load_record(tmp_path, record.record_id)["tests"] == {**tests, "tree_sha": TREE}


# --- commit gate hook ---------------------------------------------------------


def _wire(ctx, monkeypatch, reviewer):
    git._reset_commit_review_state(ctx)
    monkeypatch.setattr(git, "_run_review_preflight_tests", lambda *a, **kw: None)
    monkeypatch.setattr(git, "_run_parallel_review", reviewer)


def _cycle(ctx, message="Review changed candidate"):
    return git._run_reviewed_stage_cycle(ctx, message, 0, skip_advisory_pre_review=True, require_release_tag=False)


def _attempt_rows(ctx):
    return [row for row in load_state(ctx.drive_root).attempts if row.tool_name == "commit_reviewed"]


def _commit_reviewed(ctx, *args, **kwargs):
    """The registered public handler (what the model calls), not the internal commit routine."""
    handler = next(entry.handler for entry in git.get_tools() if entry.name == "commit_reviewed")
    return handler(ctx, *args, **kwargs)


def test_gate_pass_writes_a_settled_record_bound_to_the_attempt(candidate, monkeypatch):  # noqa: F811
    ctx = candidate

    def reviewer(_ctx, message, **kw):
        ctx._last_triad_raw_results = _three()
        ctx._last_scope_raw_result = _scope()
        ctx._last_review_structured = {"triad_rows": [_plan(r["slot_id"], r["model_id"]) for r in _three()],
                                       "scope_rows": [_plan("scope", "openai/gpt-5")], "triad_prompt": "TRIAD", "scope_brief": "SCOPE",
                                       "started_ts": "2026-10-07T00:00:00+00:00"}
        return None, ScopeReviewResult(blocked=False, status="responded"), "", []

    _wire(ctx, monkeypatch, reviewer)
    result = _cycle(ctx)
    assert result["status"] == "passed" and result["review_record_id"]
    record = rl.load_record(rl.ledger_root(ctx), result["review_record_id"])
    assert record["verdict"]["aggregate"] == "PASS" and record["state"] == "settled" and record["surface"] == "commit_gate"
    assert record["subject"]["tree_sha"] == result["pre_fingerprint"]["binding"]["tree_sha"]
    assert record["subject"]["diff_sha"] == result["pre_fingerprint"]["binding"]["diff_sha256"]
    assert record["fingerprints"]["binding"] == result["pre_fingerprint"]["fingerprint"]
    assert record["enforcement"] == "blocking" and record["enforcement_blocks"] is True
    assert record["panel"]["seats"] == 4 and record["panel"]["distinct_models"] == 3
    assert record["review_wave_id"] and record["task_id"] == ctx.task_id
    refs = [ref for row in record["rows"] for ref in row["source_refs"] if ref["role"] == "prompt"]
    assert {rl.read_source(rl.ledger_root(ctx), ctx.task_id, ref) for ref in refs} == {b"TRIAD", b"SCOPE"}
    assert _attempt_rows(ctx)[-1].review_record_id == result["review_record_id"]
    assert rl.recent_records(rl.ledger_root(ctx), ctx.task_id, 5)[0]["record_id"] == result["review_record_id"]


def test_gate_fail_and_quorum_failed_records_follow_the_gate(candidate, monkeypatch):  # noqa: F811
    ctx = candidate
    outcomes = iter(["fail", "quorum"])

    def reviewer(_ctx, message, **kw):
        kind = next(outcomes)
        if kind == "fail":
            rows = _three()
            rows[0]["parsed_items"] = [{"item": "amount", "verdict": "FAIL", "severity": "critical"}]
            ctx._last_triad_raw_results = rows
            ctx._last_review_critical_findings = rows[0]["parsed_items"]
            ctx._last_scope_raw_result = _scope()
            return "Critical feedback", ScopeReviewResult(blocked=False, status="responded"), "critical_findings", []
        rows = _three(status="error")
        rows[0]["status"] = "responded"
        ctx._last_triad_raw_results = rows
        ctx._last_review_block_reason = "quorum_failed"
        ctx._last_scope_raw_result = _scope()
        return "⚠️ REVIEW_BLOCKED: quorum", ScopeReviewResult(blocked=False, status="responded"), "quorum_failed", []

    _wire(ctx, monkeypatch, reviewer)
    failed = _cycle(ctx)
    assert failed["status"] == "blocked" and failed["review_record_id"]
    record = rl.load_record(rl.ledger_root(ctx), failed["review_record_id"])
    assert record["verdict"]["aggregate"] == "FAIL" and record["verdict"]["per_question"]["change"] == "FAIL"
    assert record["verdict"]["critical_findings"] == [{"item": "amount", "verdict": "FAIL", "severity": "critical"}]
    assert _attempt_rows(ctx)[-1].review_record_id == failed["review_record_id"]

    (ctx.repo_dir / "change.py").write_text("value = 3\n", encoding="utf-8")
    git.run_cmd(["git", "add", "change.py"], cwd=ctx.repo_dir)
    quorum = _cycle(ctx, "Second candidate")
    assert quorum["status"] == "blocked" and quorum["review_record_id"] not in ("", failed["review_record_id"])
    record = rl.load_record(rl.ledger_root(ctx), quorum["review_record_id"])
    assert record["verdict"]["aggregate"] == "QUORUM_FAILED" and record["verdict"]["quorum"]["parts"]["change"]["responded"] == 1
    assert len(rl.recent_records(rl.ledger_root(ctx), ctx.task_id, 5)) == 2


def test_free_refusal_before_dispatch_is_a_not_dispatched_record(candidate, monkeypatch):  # noqa: F811
    ctx = candidate
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "1")
    calls = []

    def reviewer(_ctx, message, **kw):
        from ouroboros.review_dispatch import invoke_review_paid_stamp
        invoke_review_paid_stamp(ctx._review_paid_stamp)
        calls.append(message)
        rows = _three()
        rows[0]["parsed_items"] = [{"item": "amount", "verdict": "FAIL", "severity": "critical"}]
        ctx._last_triad_raw_results = rows
        ctx._last_review_critical_findings = rows[0]["parsed_items"]
        ctx._last_scope_raw_result = _scope()
        return "Critical feedback", ScopeReviewResult(blocked=False, status="responded"), "critical_findings", []

    _wire(ctx, monkeypatch, reviewer)
    first = _cycle(ctx)
    assert first["status"] == "blocked" and first["block_reason"] == "critical_findings"
    # the exhausted paid-cycle ceiling refuses the next candidate for free
    (ctx.repo_dir / "change.py").write_text("value = 3\n", encoding="utf-8")
    git.run_cmd(["git", "add", "change.py"], cwd=ctx.repo_dir)
    git._reset_commit_review_state(ctx)
    second = _cycle(ctx, "Another candidate")
    assert second["block_reason"] == "review_cycles_exhausted" and len(calls) == 1
    assert second["review_record_id"] and second["review_record_id"] != first["review_record_id"]
    record = rl.load_record(rl.ledger_root(ctx), second["review_record_id"])
    assert record["verdict"]["aggregate"] == "NOT_DISPATCHED" and record["dispatch_refusal"]["kind"] == "review_cycles_exhausted"
    assert record["rows"] == [] and record["cost"] == {"usd": 0.0, "unknown": False}
    assert _attempt_rows(ctx)[-1].review_record_id == second["review_record_id"]
    assert _attempt_rows(ctx)[-1].block_reason == "review_cycles_exhausted"


def _advisory_push_setup(ctx, monkeypatch):
    """The Advisory/pro commit surface of ``test_commit_finish_requires_received_outcome``."""
    from ouroboros.mutation_attribution import capture_mutation_baseline
    from ouroboros.task_results import write_task_result
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "pro")
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "unlimited")
    ctx.branch_dev = git.run_cmd(["git", "branch", "--show-current"], cwd=ctx.repo_dir).strip()
    git.run_cmd(["git", "reset", "--hard", "HEAD"], cwd=ctx.repo_dir)
    (ctx.repo_dir / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    git.run_cmd(["git", "add", "VERSION"], cwd=ctx.repo_dir)
    git.run_cmd(["git", "commit", "-m", "fixture version"], cwd=ctx.repo_dir)
    write_task_result(ctx.drive_root, ctx.task_id, "running")
    capture_mutation_baseline(ctx.drive_root, ctx.task_id, [{"surface_type": "system_repo", "host_root": str(ctx.repo_dir)}])
    (ctx.repo_dir / "change.py").write_text("value = 2\n", encoding="utf-8")
    monkeypatch.setattr(git, "_post_commit_result", lambda *_a, **_kw: None)
    monkeypatch.setattr(git, "_auto_push", lambda *_a, **_kw: "")


def test_pending_custody_record_settles_in_place_on_the_exact_retry(candidate, monkeypatch):  # noqa: F811
    ctx = candidate
    _advisory_push_setup(ctx, monkeypatch)
    calls = []

    def reviewer(_ctx, message, **kw):
        from ouroboros.review_custody import prepare_frozen_review_reconciliation
        from ouroboros.review_dispatch import invoke_review_paid_stamp
        if ctx._review_reconcile_only:  # what the real dispatcher does first on an exact retry
            prepare_frozen_review_reconciliation(ctx, ctx._pending_review_attempt)
        invoke_review_paid_stamp(ctx._review_paid_stamp)
        calls.append(message)
        if len(calls) == 1:
            ctx._last_triad_raw_results = [_raw("critic", "openai/gpt-5", "pending", operation_state="in_flight",
                                                operation_id="pending-triad", late_result_pending=True)]
            ctx._last_scope_raw_result = {"slot_id": "scope", "status": "pending", "operation_state": "in_flight",
                                          "operation_id": "pending-scope", "late_result_pending": True}
            return "All reviewers still running.", ScopeReviewResult(blocked=True, status="pending"), "infra_failure", []
        ctx._last_triad_raw_results = [_raw("critic", "openai/gpt-5", operation_id="pending-triad")]
        ctx._last_scope_raw_result = {"raw_results": [{"slot_id": "scope", "status": "responded", "model_id": "openai/gpt-5",
                                                       "raw_text": "scope answer", "operation_id": "pending-scope"}]}
        return None, ScopeReviewResult(blocked=False, status="responded"), "", []

    _wire(ctx, monkeypatch, reviewer)
    first = _commit_reviewed(ctx, "Fix amount", skip_advisory_review=True)
    reference = json.loads(first.split("\n", 1)[1])["review_reference"]
    record_id = reference["review_record_id"]
    assert record_id and _attempt_rows(ctx)[-1].late_result_pending
    pending = rl.load_record(rl.ledger_root(ctx), record_id)
    assert pending["state"] == "pending" and pending["revision"] == 1 and pending["verdict"]["aggregate"] == "NOT_PERFORMED"
    second = _commit_reviewed(ctx, "Fix amount", skip_advisory_review=True)  # exact retry reconciles the same attempt
    assert len(calls) == 2, second
    settled = rl.load_record(rl.ledger_root(ctx), record_id)
    assert settled["state"] == "settled" and settled["revision"] == 2 and settled["verdict"]["aggregate"] == "PASS", second
    assert f"review_record_id: {record_id}" in second
    assert [r["record_id"] for r in rl.recent_records(rl.ledger_root(ctx), ctx.task_id, 5)] == [record_id]
    assert rl.recent_records(rl.ledger_root(ctx), ctx.task_id, 5)[0]["revision"] == 2


def test_author_continuation_notes_its_decision_on_the_answered_record(candidate, monkeypatch):  # noqa: F811
    ctx = candidate
    _advisory_push_setup(ctx, monkeypatch)
    calls = []

    def reviewer(_ctx, message, **kw):
        from ouroboros.review_dispatch import invoke_review_paid_stamp
        invoke_review_paid_stamp(ctx._review_paid_stamp)
        calls.append(message)
        rows = _three()
        rows[0]["parsed_items"] = [{"item": "amount", "verdict": "FAIL", "severity": "critical"}]
        ctx._last_triad_raw_results = rows
        ctx._last_review_critical_findings = rows[0]["parsed_items"]
        ctx._last_scope_raw_result = _scope()
        return "Critical feedback", ScopeReviewResult(blocked=False, status="responded"), "critical_findings", []

    _wire(ctx, monkeypatch, reviewer)
    first = _commit_reviewed(ctx, "Fix amount", skip_advisory_review=True)
    reference = json.loads(first.split("\n", 1)[1])["review_reference"]
    assert reference["review_record_id"]
    second = _commit_reviewed(ctx, "Fix amount", review_reference=reference,
                                   author_disposition={"disposition": "accepted", "rationale": "Known tradeoff."})
    assert len(calls) == 1 and f"review_record_id: {reference['review_record_id']}" in second
    record = rl.load_record(rl.ledger_root(ctx), reference["review_record_id"])
    assert record["revision"] == 2 and record["verdict"]["aggregate"] == "FAIL"
    assert record["author_decision"]["disposition"] == "accepted" and record["author_decision"]["rationale"] == "Known tradeoff."
    assert len(rl.recent_records(rl.ledger_root(ctx), ctx.task_id, 5)) == 1, "author continuation buys no record"
    assert _attempt_rows(ctx)[-1].status == "succeeded" and _attempt_rows(ctx)[-1].review_record_id == reference["review_record_id"]


# --- reviewer slot execution rows carry the record id ------------------------


def test_slot_executions_carry_and_bind_the_record_id(tmp_path, monkeypatch):
    from ouroboros import reviewer_slot_config as cfg
    monkeypatch.setattr(cfg, "_last_execution_path", lambda: tmp_path / "last.json")
    slot = SimpleNamespace(slot_id="s1", model="openai/gpt-5", route=SimpleNamespace(value="api_chat"), effort="high",
                           session_target="", session_profile="", subagent_id="", processing_preference="", declared_effort="")
    actor = SimpleNamespace(slot_id="s1", status="responded", usage={}, operation_state="settled")
    import ouroboros.utils as utils_mod
    stamps = iter(f"2026-10-07T00:00:0{i}+00:00" for i in range(1, 9))
    monkeypatch.setattr(utils_mod, "utc_now_iso", lambda: next(stamps))
    rows = cfg.record_reviewer_slot_executions("commit_gate", [actor], {"s1": slot})
    assert set(rows) == {"s1"} and rows["s1"]["surface"] == "commit_gate" and "review_record_id" not in rows["s1"]
    assert "review_record_id" not in cfg.reviewer_slot_last_executions()["s1"], "three positional args stay the old shape"
    cfg.record_reviewer_slot_executions("commit_gate", [actor], {"s1": slot}, record_id="rl-1")
    assert cfg.reviewer_slot_last_executions()["s1"]["review_record_id"] == "rl-1"
    mine = cfg.record_reviewer_slot_executions("commit_gate", [actor], {"s1": slot})
    cfg.bind_reviewer_slot_record_id({**mine, "missing": {"ts": mine["s1"]["ts"]}}, "rl-2")
    assert cfg.reviewer_slot_last_executions()["s1"]["review_record_id"] == "rl-2", "the wave's own row takes its id"
    # Another surface finished the SAME seat after this wave: the projection row is no longer
    # the wave's, so binding with the wave's own rows never labels the foreign run.
    theirs = cfg.record_reviewer_slot_executions("plan_review", [actor], {"s1": slot})
    assert theirs["s1"]["ts"] != mine["s1"]["ts"]
    cfg.bind_reviewer_slot_record_id(mine, "rl-3")
    assert "review_record_id" not in cfg.reviewer_slot_last_executions()["s1"], "a later run of the seat keeps its own identity"
    cfg.bind_reviewer_slot_record_id(theirs, "rl-4")
    assert cfg.reviewer_slot_last_executions()["s1"]["review_record_id"] == "rl-4"
    cfg.bind_reviewer_slot_record_id(theirs, "")
    assert cfg.reviewer_slot_last_executions()["s1"]["review_record_id"] == "rl-4"


def test_a_passed_critical_item_is_a_clean_answer_and_only_a_failed_one_is_a_finding():
    clean = [_raw(f"s{i}", m, parsed_items=[{"item": "bible_compliance", "verdict": "PASS", "severity": "critical"}])
             for i, m in enumerate(("openai/gpt-5", "anthropic/claude-x", "google/gemini"), 1)]
    record = rl.build_commit_gate_record(_facts(clean, _scope()))
    change_rows = [row for row in record.rows if row["parts"] == ["change"]]
    assert [row["critical_count"] for row in change_rows] == [0, 0, 0]
    assert [rl.row_verdict(row) for row in change_rows] == ["PASS"] * 3 and record.verdict["aggregate"] == "PASS"
    failing = [_raw("s1", "openai/gpt-5", parsed_items=[{"item": "secrets_check", "verdict": "FAIL", "severity": "critical"}]),
               *clean[1:]]
    record = rl.build_commit_gate_record(_facts(failing, _scope()))
    assert record.rows[0]["critical_count"] == 1 and rl.row_verdict(record.rows[0]) == "FAIL"
    assert record.verdict["aggregate"] == "FAIL"


def test_ledger_facts_take_this_waves_own_execution_rows_never_the_shared_projection(candidate, monkeypatch):  # noqa: F811
    from ouroboros import reviewer_slot_config as cfg
    from ouroboros.tools import commit_gate

    ctx = candidate
    git._reset_commit_review_state(ctx)
    assert ctx._last_review_slot_executions == {}
    monkeypatch.setattr(cfg, "_last_execution_path", lambda: ctx.drive_root / "last.json")
    slot = SimpleNamespace(slot_id="s1", model="openai/gpt-5", route=SimpleNamespace(value="api_chat"), effort="high",
                           session_target="", session_profile="", subagent_id="", processing_preference="", declared_effort="")
    cfg.record_reviewer_slot_executions("plan_review", [SimpleNamespace(slot_id="s1", status="responded", usage={},
                                                                         operation_state="settled")], {"s1": slot})
    mine = {"s1": {"ts": "2026-10-07T00:00:01+00:00", "surface": "commit_gate", "effective": {"model": "openai/gpt-5"}}}
    ctx._last_review_slot_executions = dict(mine)
    facts = commit_gate._review_ledger_facts(ctx, "msg", goal="", scope="", pre_fingerprint={}, advisory_paths=None,
                                             blocked=False, block_reason="", combined_findings=None,
                                             dispatch_refusal=None, pending=False)
    assert facts["slot_executions"] == mine, "another surface's row for the same seat is not this wave's fact"


def test_tests_fact_is_passed_only_for_a_candidate_bound_proof(candidate, monkeypatch):  # noqa: F711,F811
    from ouroboros import commit_admission
    from ouroboros.tools import commit_gate

    ctx = candidate
    git._reset_commit_review_state(ctx)
    facts = lambda: commit_gate._review_ledger_facts(  # noqa: E731
        ctx, "msg", goal="", scope="", pre_fingerprint={}, advisory_paths=None, blocked=False, block_reason="",
        combined_findings=None, dispatch_refusal=None, pending=False)["tests"]
    assert facts() == {"policy": "NOT_RUN", "result": "unknown"}
    ctx._preflight_tests_passed = True  # the runner's flag from an EARLIER candidate survives in the process
    ctx._preflight_test_proof = None
    assert facts() == {"policy": "NOT_RUN", "result": "unknown", "reason": "tests_proof_not_for_this_candidate"}
    monkeypatch.setattr(commit_admission, "preflight_test_proof_matches", lambda ctx, repo: True)
    assert facts() == {"policy": "run", "result": "passed", "proof": "candidate_bound"}
    ctx._preflight_tests_passed = False
    assert facts() == {"policy": "NOT_RUN", "result": "unknown"}


def test_the_public_commit_outcome_names_the_record_when_the_review_blocks(candidate, monkeypatch):  # noqa: F811
    ctx = candidate

    def reviewer(_ctx, message, **kw):
        rows = _three()
        rows[0]["parsed_items"] = [{"item": "amount", "verdict": "FAIL", "severity": "critical"}]
        ctx._last_triad_raw_results = rows
        ctx._last_review_critical_findings = rows[0]["parsed_items"]
        ctx._last_scope_raw_result = _scope()
        return "Critical feedback", ScopeReviewResult(blocked=False, status="responded"), "critical_findings", []

    _wire(ctx, monkeypatch, reviewer)
    import subprocess
    subprocess.run(["git", "checkout", "-q", "-b", ctx.branch_dev], cwd=ctx.repo_dir, check=True, capture_output=True)
    result = _commit_reviewed(ctx, "Review changed candidate", skip_tests=True, skip_advisory_pre_review=True)
    record_id = _attempt_rows(ctx)[-1].review_record_id
    assert record_id and rl.load_record(rl.ledger_root(ctx), record_id)["verdict"]["aggregate"] == "FAIL"
    assert f"review_record_id: {record_id}" in result, "the actor-facing outcome names the record the canon promises"


def test_concurrent_halves_of_one_wave_both_keep_their_execution_rows(tmp_path, monkeypatch):
    """Triad and scope record concurrently: both reads may see the same empty stash, so the
    merge must happen under the shared lock. The ctx below holds every reader at a barrier,
    which lets two unsynchronized writers read the same snapshot (one row would be lost)."""
    import threading

    from ouroboros import reviewer_slot_config as cfg
    monkeypatch.setattr(cfg, "_last_execution_path", lambda: tmp_path / "last.json")

    class RacingCtx:
        def __init__(self):
            self._kept, self.barrier = {}, threading.Barrier(2, timeout=1.0)

        @property
        def _last_review_slot_executions(self):
            snapshot = self._kept  # read first, then pause: two unsynchronized readers hold the same snapshot
            try:
                self.barrier.wait()
            except threading.BrokenBarrierError:
                pass
            return snapshot

        @_last_review_slot_executions.setter
        def _last_review_slot_executions(self, value):
            self._kept = value

    def slot(slot_id):
        return SimpleNamespace(slot_id=slot_id, model="openai/gpt-5", route=SimpleNamespace(value="api_chat"), effort="high",
                               session_target="", session_profile="", subagent_id="", processing_preference="",
                               declared_effort="")

    cfg.reviewer_slot_execution_rows("commit_gate", [], {})  # warm the lazy imports so both halves reach the read together
    ctx = RacingCtx()
    halves = [("commit_gate", "triad_1"), ("commit_gate", "scope_1")]
    threads = [threading.Thread(target=cfg.record_reviewer_slot_executions, args=(
        surface, [SimpleNamespace(slot_id=seat, status="responded", usage={}, operation_state="settled")],
        {seat: slot(seat)}), kwargs={"keep_on": ctx}) for surface, seat in halves]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert set(ctx._kept) == {"triad_1", "scope_1"}, "both halves of the wave keep their execution rows"


def test_a_post_wave_refusal_still_names_the_record_of_the_completed_wave(candidate, monkeypatch):  # noqa: F811
    from ouroboros.tools import git_review_cycle

    ctx = candidate

    def reviewer(_ctx, message, **kw):
        ctx._last_triad_raw_results = _three()
        ctx._last_scope_raw_result = _scope()
        return None, ScopeReviewResult(blocked=False, status="responded"), "", []

    _wire(ctx, monkeypatch, reviewer)
    # The staged candidate "changes" only once the wave has answered (the reviewer above set its raw
    # results): every earlier revalidation passes, the one after the wave refuses.
    monkeypatch.setattr(git_review_cycle, "_revalidation_outcome", lambda *a, **kw: {
        "status": "blocked", "block_reason": "revalidation_failed", "message": "REVALIDATION FAILED",
    } if ctx._last_triad_raw_results else None)
    import subprocess
    subprocess.run(["git", "checkout", "-q", "-b", ctx.branch_dev], cwd=ctx.repo_dir, check=True, capture_output=True)
    result = _commit_reviewed(ctx, "Review changed candidate", skip_tests=True, skip_advisory_pre_review=True)
    assert result.startswith("REVALIDATION FAILED")
    record_id = result.rsplit("review_record_id: ", 1)[1].strip()
    assert rl.load_record(rl.ledger_root(ctx), record_id)["verdict"]["aggregate"] == "PASS", "the completed wave's record"


def test_a_commit_that_fails_after_a_clean_wave_still_names_its_record(candidate, monkeypatch):  # noqa: F811
    import subprocess

    ctx = candidate

    def reviewer(_ctx, message, **kw):
        ctx._last_triad_raw_results = _three()
        ctx._last_scope_raw_result = _scope()
        return None, ScopeReviewResult(blocked=False, status="responded"), "", []

    _wire(ctx, monkeypatch, reviewer)
    subprocess.run(["git", "checkout", "-q", "-b", ctx.branch_dev], cwd=ctx.repo_dir, check=True, capture_output=True)
    hook = ctx.repo_dir / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'rejected by hook' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    result = _commit_reviewed(ctx, "Review changed candidate", skip_tests=True, skip_advisory_pre_review=True)
    assert result.startswith("⚠️ GIT_ERROR (commit)")
    record_id = result.rsplit("review_record_id: ", 1)[1].strip()
    assert rl.load_record(rl.ledger_root(ctx), record_id)["verdict"]["aggregate"] == "PASS"
    assert _attempt_rows(ctx)[-1].review_record_id == record_id


def test_naming_the_record_keeps_a_typed_result_paired_with_its_text(tmp_path):
    from ouroboros.tools import commit_gate
    from ouroboros.tools.tool_result import (
        ToolResult, _install_tool_result_sidecar, _publish_tool_result, _published_tool_result,
        _restore_tool_result_sidecar,
    )

    ctx = SimpleNamespace(_current_review_record_id="rl-typed-1")
    sentinel = object()
    token = _install_tool_result_sidecar(ctx, sentinel)
    try:
        text = _publish_tool_result(ctx, ToolResult(status="error", code="TOOL_ARG_ERROR", text="⚠️ REFUSED"))
        named = commit_gate.name_review_record(ctx, text)
        published = _published_tool_result(ctx, sentinel)
    finally:
        _restore_tool_result_sidecar(token)
    assert named == "⚠️ REFUSED\nreview_record_id: rl-typed-1"
    assert isinstance(published, ToolResult) and published.text == named and published.code == "TOOL_ARG_ERROR", \
        "the registry still pairs the returned text with its typed result"
    assert commit_gate.name_review_record(SimpleNamespace(_current_review_record_id=""), "plain") == "plain"
    assert commit_gate.name_review_record(ctx, named) == named, "a text that already names the record is unchanged"


def test_the_public_handler_still_refuses_a_call_without_a_commit_message_at_binding():
    """The registry binds arguments to the handler's signature before running it: a call
    without ``commit_message`` stays a typed argument refusal, never a handler crash."""
    import inspect

    handler = next(entry.handler for entry in git.get_tools() if entry.name == "commit_reviewed")
    with pytest.raises(TypeError):
        inspect.signature(handler).bind(object())
    inspect.signature(handler).bind(object(), commit_message="m", skip_tests=True)
