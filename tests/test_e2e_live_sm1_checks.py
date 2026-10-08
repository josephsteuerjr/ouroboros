"""SM1's clean-worktree check after the reviewed commit: the runtime's own transient scratch under ``.ouroboros/``
(``run_script`` in the active workspace, tools/shell.py) is recorded but does not fail the check — the post-task
evolution cycle starts seconds after the commit in the same clone (rc.15 run3, SM1_a1, issue #701) — while any other
untracked or modified path still does."""
from __future__ import annotations

import pathlib
import subprocess
import types

import pytest

from devtools.e2e_live import scenarios
from devtools.e2e_live.ui_probe import UIProbe


def _repo(root: pathlib.Path) -> pathlib.Path:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for key, value in (("user.name", "t"), ("user.email", "t@example.com")):
        subprocess.run(["git", "-C", str(root), "config", key, value], check=True)
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "base"], check=True)
    return root


def test_the_runtimes_transient_scratch_is_recorded_but_does_not_fail_the_clean_check(tmp_path):
    root = _repo(tmp_path)
    assert scenarios.worktree_after_commit(root) == (True, "", [])
    (root / ".ouroboros" / "tmp_scripts").mkdir(parents=True)
    (root / ".ouroboros" / "tmp_scripts" / "script_deadbeef.py").write_text("print(1)\n", encoding="utf-8")
    clean, porcelain, transient = scenarios.worktree_after_commit(root)
    assert clean is True and porcelain == "?? .ouroboros/" and transient == ["?? .ouroboros/"]


def test_any_other_untracked_or_modified_path_still_fails_the_clean_check(tmp_path):
    root = _repo(tmp_path)
    (root / ".ouroboros" / "tmp_scripts").mkdir(parents=True)
    (root / ".ouroboros" / "tmp_scripts" / "script_deadbeef.py").write_text("print(1)\n", encoding="utf-8")
    (root / "stray.txt").write_text("x\n", encoding="utf-8")
    clean, porcelain, transient = scenarios.worktree_after_commit(root)
    assert clean is False and "?? stray.txt" in porcelain and transient == ["?? .ouroboros/"]
    (root / "stray.txt").unlink()
    (root / "a.txt").write_text("changed\n", encoding="utf-8")
    clean, porcelain, transient = scenarios.worktree_after_commit(root)
    assert clean is False and "M a.txt" in porcelain and transient == ["?? .ouroboros/"]   # ``_git`` strips the leading column


def test_the_landing_commit_call_records_its_skip_flags():
    """rc.15 run2/run3: two SM1 lanes landed through review_rebuttal + skip_advisory_review=True; the documented
    path has no skip flags, so the landing call's truthy ``skip_*`` arguments are a recorded fact and a check."""
    rows = [{"tool": "commit_reviewed", "result_preview": "⚠️ REVIEW_BLOCKED (attempt 1)", "args": {"skip_advisory_review": True}},
            {"tool": "commit_reviewed", "result_preview": "OK: committed to ouroboros: x",
             "args": {"skip_advisory_review": True, "skip_tests": False, "paths": ["a"]}},
            {"tool": "read_file", "result_preview": "OK", "args": {"skip_advisory_review": True}}]
    facts = scenarios.commit_refusal_facts({}, rows, {})
    assert facts["landing_skip_flags"] == ["skip_advisory_review"]
    clean = [{"tool": "commit_reviewed", "result_preview": "OK: committed", "args": {"paths": ["a"]}}]
    assert scenarios.commit_refusal_facts({}, clean, {})["landing_skip_flags"] == []
    assert scenarios.commit_refusal_facts({}, [], {})["landing_skip_flags"] == []


def test_t2_the_wave_fact_is_the_commit_gates_record_of_this_task_with_both_questions_answered():
    """FIX3 T2: the oracle reads what the wave really writes — the commit gate's review-ledger record
    (built here by the ledger itself, never a hand-shaped dict) — and ignores a dispatch-less
    refusal record, another task's record and another surface's; the newest answering record wins."""
    from ouroboros import review_ledger as rl
    from tests.test_review_ledger import BOTH, _facts, _raw, _three

    raws = [_raw("s1", "openai/gpt-5"), _raw("s2", "anthropic/claude-x", parts=BOTH), _raw("s3", "google/gemini")]
    landed = rl.build_commit_gate_record(_facts(raws, task_id="sm1-task")).to_dict()
    assert landed["verdict"]["per_question"] == {"change": "PASS", "coupling": "PASS"}, landed["verdict"]
    refused = rl.build_commit_gate_record(_facts([], task_id="sm1-task", dispatch_refusal={
        "kind": "pool_empty", "message": "the review pool is empty"})).to_dict()
    other_task = rl.build_commit_gate_record(_facts(_three(), task_id="other-task")).to_dict()
    other_surface = {**landed, "surface": "plan_review", "ts": "2099-01-01T00:00:00+00:00"}

    fact = scenarios.commit_wave_fact([refused, other_task, other_surface, landed], "sm1-task")
    assert fact == {"record_id": landed["record_id"], "aggregate": "PASS",
                    "per_question": {"change": "PASS", "coupling": "PASS"}, "seats": 3}
    assert scenarios.commit_wave_fact([refused, other_task, other_surface], "sm1-task") == {}, (
        "a record that dispatched nothing, another task's or another surface's is not the wave")
    assert scenarios.commit_wave_fact([], "sm1-task") == {} and scenarios.commit_wave_fact([None, 3], "sm1-task") == {}
    # Several attempts: the NEWEST record that answered both questions is the fact, whatever its verdict
    # (the landing itself is commit_landed's business), never an older PASS over a newer answer.
    newer_fail = {**landed, "record_id": "rv-newer", "ts": "2099-01-01T00:00:00+00:00",
                  "verdict": {**landed["verdict"], "aggregate": "FAIL", "per_question": {"change": "FAIL", "coupling": "PASS"}}}
    assert scenarios.commit_wave_fact([landed, newer_fail], "sm1-task")["record_id"] == "rv-newer"


def test_ui_probe_waits_for_the_requested_document():
    calls = []
    probe = UIProbe("http://lane.test")
    probe.page = types.SimpleNamespace(
        goto=lambda url, **kw: calls.append(("goto", url)),
        wait_for_selector=lambda selector, **kw: calls.append(("ready", selector)),
    )
    probe.goto()
    probe.goto("/onboarding", ready_selector=".wizard-shell")
    assert calls == [("goto", "http://lane.test/"), ("ready", "#chat-input"),
                     ("goto", "http://lane.test/onboarding"), ("ready", ".wizard-shell")]


@pytest.mark.parametrize("problem", ["none", "missing-sheet", "stale-accent", "stale-focus", "no-commit"])
def test_sm1_oracle_requires_both_effective_palettes_and_the_landed_commit(tmp_path, problem):
    calls = []

    class PaletteUI:
        path = ""

        def goto(self, path, *, ready_selector):
            self.path = path
            calls.append((path, ready_selector))

        def computed_property(self, selector, name):
            assert selector == ":root"
            if self.path == "/onboarding":
                if problem == "missing-sheet":
                    return ""
                if (problem == "stale-accent" and name == "--accent") or (
                        problem == "stale-focus" and name == "--focus-accent-border"):
                    return "#c93545"
            return scenarios.SM1_NEW_ACCENT

        screenshot = close = lambda self, *args: None

    server = types.SimpleNamespace(base_url="http://lane.test")
    ctx = scenarios.LaneContext(server=server, clone=tmp_path, data_root=tmp_path, oracle=None,
                                harness=None, ui_resolver=lambda _: (PaletteUI(), ""), ui_reason="",
                                shots=tmp_path, log=lambda _: None, task_timeout=1,
                                restart=lambda: (calls.append("restart") or server))
    ctx.check("commit_landed", problem != "no-commit")
    scenarios.check_sm1_rendered_palette(ctx, scenarios.SM1_REQUIRED_PALETTE)
    assert calls == ["restart", ("/", "#chat-input"), ("/onboarding", ".wizard-shell")]
    assert ctx.checks["ui_computed_style"] is (problem == "none")
    assert ctx.checks["ui_app_palette"] is (problem != "no-commit")
    assert ctx.checks["ui_onboarding_palette"] is (problem in {"none", "stale-focus"})
    assert len(ctx.facts["palette_computed"]["app"]) == len(scenarios.SM1_REQUIRED_PALETTE)
    ctx.close_ui()
