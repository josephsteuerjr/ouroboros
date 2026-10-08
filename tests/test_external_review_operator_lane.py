"""The operator lane of the external review wrapper: the commit gate's own
review-only cycle, run in the runtime's isolated checkout of the staged index
(R3), with its advisory record, drift artifact and custody-bound retention."""

import json
from pathlib import Path

from tests import _contributor_packet_shared as shared


def test_external_review_advisory_warning_uses_safe_canonical_reason(monkeypatch):
    import scripts.run_external_review as module
    from ouroboros.tools import claude_advisory_review as advisory

    monkeypatch.setattr(advisory, "advisory_gate_unavailability_reason", lambda: "agent_session_route_unavailable")
    warning = module._advisory_unavailability_warning()
    assert "agent_session_route_unavailable" in warning
    assert advisory.ADVISORY_REVIEW_CHOICE_GUIDANCE in warning
    assert "ANTHROPIC_API_KEY" not in warning

    secret_error = "secret-setting-value-must-not-leak"

    def _malformed():
        raise ValueError(secret_error)

    monkeypatch.setattr(advisory, "advisory_gate_unavailability_reason", _malformed)
    warning = module._advisory_unavailability_warning()
    assert "invalid_advisory_configuration" in warning
    assert secret_error not in warning


def test_external_review_checks_advisory_after_settings_load_without_key_heuristic():
    """The operator lane asks the advisory gate's own availability answer only after
    the settings landed in the environment, and never guesses from a key's presence."""
    import inspect
    import scripts.run_external_review as module

    main_source = inspect.getsource(module.main)
    prepare_source = inspect.getsource(module._prepare_review_configuration)
    operator_source = inspect.getsource(module._operator_lane)
    assert "_load_settings_into_env()" in prepare_source
    assert main_source.index("_prepare_review_configuration(args)") < main_source.index("_operator_lane(")
    assert "_advisory_unavailability_warning()" in operator_source
    assert operator_source.index("_advisory_unavailability_warning()") < operator_source.index(
        "_run_non_committing_review_cycle(")
    for source in (main_source, operator_source):
        assert 'os.environ.get("ANTHROPIC_API_KEY"' not in source


def _operator_fixture(tmp_path: Path, monkeypatch) -> tuple[Path, list[dict]]:
    """An installed body with a staged README edit; the operator lane's paid seam
    (the review substrate) and hermetic test runner are the golden stand-ins, and
    the gate reads the golden panel from the frozen slot plan."""
    import ouroboros.review_substrate as substrate
    import scripts.run_external_review as module
    from ouroboros.tools import git as git_mod
    from ouroboros.tools import review_helpers

    fixture = shared.init_installed_body(tmp_path)
    repo = Path(fixture["repo"])
    (repo / "README.md").write_text("staged edit\n", encoding="utf-8")
    shared.git(repo, "add", "README.md")
    monkeypatch.setattr(module, "REPO", repo)
    monkeypatch.setattr(module, "_load_settings_into_env", lambda: None)
    monkeypatch.setattr(module, "_resolved_review_config",
                        lambda *, profile="production_commit_gate": json.loads(json.dumps(shared.GOLDEN_CONFIG)))
    monkeypatch.setattr(module, "_select_healthy_openrouter_key", lambda **_kwargs: False)
    monkeypatch.setenv("OUROBOROS_REVIEWER_SLOTS", json.dumps(module._slot_plan_payload(shared.GOLDEN_CONFIG)))
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")
    monkeypatch.setenv("OUROBOROS_PRE_PUSH_TESTS", "1")
    briefs: list[dict] = []
    monkeypatch.setattr(substrate, "run_review_request", shared.golden_substrate(briefs))
    monkeypatch.setattr(review_helpers, "_run_review_preflight_tests", shared.passing_test_runner)
    monkeypatch.setattr(git_mod, "_run_review_preflight_tests", shared.passing_test_runner)
    return repo, briefs


def _run_operator_lane(module, monkeypatch, tmp_path: Path, *extra: str) -> tuple[int, Path]:
    output = tmp_path / f"out-{len(list(tmp_path.glob('out-*')))}"
    monkeypatch.setattr(module.sys, "argv", [
        "run_external_review.py", f"--output={output}", f"--drive-root={tmp_path / 'drive'}", *extra, "fix: one"])
    return module.main(), output


def test_operator_lane_is_the_commit_gate_dry_run_in_an_isolated_checkout(tmp_path, monkeypatch):
    """R3: the operator lane runs the commit gate's own non-committing cycle over the
    staged index, in the runtime's isolated checkout of the staged patch, and records
    the advisory pre-review in full; the primary worktree is never touched."""
    import scripts.run_external_review as module

    repo, briefs = _operator_fixture(tmp_path, monkeypatch)
    staged_before = shared.git(repo, "diff", "--cached")
    checkouts = tmp_path / "drive" / "state" / "review_checkouts"

    exit_code, output = _run_operator_lane(module, monkeypatch, tmp_path)

    assert exit_code == 0, (output / "outcome.json").read_text(encoding="utf-8")
    outcome = json.loads((output / "outcome.json").read_text(encoding="utf-8"))
    assert outcome["exit_code"] == 0 and outcome["outcome"]["status"] == "passed"
    assert outcome["outcome"]["review_record_id"].startswith("rl-")
    assert "retained_checkout" not in outcome["outcome"]
    # The gate's advisory pre-review is recorded in full, whatever its availability.
    advisory = json.loads((output / "advisory.txt").read_text(encoding="utf-8"))
    assert advisory.get("status")
    sections = shared.full_output_sections((output / "full-output.txt").read_text(encoding="utf-8"))
    seats = json.loads(sections["REVIEW SEAT RECORDS (ledger rows with retained answers, full, untruncated)"])
    assert [(seat["seat_id"], seat["answer"]) for seat in seats] == [
        (slot, shared.ANSWERS[slot]) for slot in ("t1", "t2", "s1")]
    verdict = json.loads(sections["AGGREGATE VERDICT"])
    assert verdict["review_record"]["record_id"] == outcome["outcome"]["review_record_id"]
    assert (verdict["review_record"]["aggregate"], verdict["review_record"]["surface"]) == ("PASS", "commit_gate")
    assert verdict["cost_report"]["unreported_or_unknown_cost_slots"] == ["t2"]
    # Every seat read the isolated checkout of the staged patch, never this worktree.
    assert sorted(brief["slot_id"] for brief in briefs) == ["s1", "t1", "t2"]
    for brief in briefs:
        if brief["session_root"]:
            assert Path(brief["session_root"]).parent.parent == checkouts
            assert Path(brief["session_root"]) != repo
    # Custody settled: the checkout is gone; the primary worktree still holds the staged edit.
    assert not checkouts.exists() or not any(checkouts.iterdir())
    assert shared.git(repo, "diff", "--cached") == staged_before
    assert shared.git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    # The cycle's own hygiene (``_ensure_gitignore``) added a file the operator never
    # staged: the untracked-safe comparison surfaces exactly that, loudly, as drift.
    drift = (output / "reviewed-tree-drift.diff").read_text(encoding="utf-8")
    assert "+++ b/.gitignore" in drift and "+staged edit" in drift
    assert not (repo / ".gitignore").exists()


def test_operator_lane_retains_the_checkout_while_a_seat_is_open(tmp_path, monkeypatch):
    """R4 (operator lane): a reviewer seat still owed after the cycle keeps the
    isolated checkout alive for reconciliation, named in the typed outcome."""
    import ouroboros.review_substrate as substrate
    import scripts.run_external_review as module

    repo, _briefs = _operator_fixture(tmp_path, monkeypatch)
    settled = substrate.run_review_request

    def one_seat_open(request, *, slots, **kwargs):
        result = settled(request, slots=slots, **kwargs)
        for actor in result.actors:
            if actor["slot_id"] == "t2":
                actor.update(status="error", raw_text="", error="logical wait expired",
                             operation_state="in_flight", late_result_pending=True)
        return result

    monkeypatch.setattr(substrate, "run_review_request", one_seat_open)

    exit_code, output = _run_operator_lane(module, monkeypatch, tmp_path)

    outcome = json.loads((output / "outcome.json").read_text(encoding="utf-8"))
    assert (exit_code, outcome["exit_code"], outcome["outcome"]["status"]) == (3, 3, "blocked")
    retained = Path(outcome["outcome"]["retained_checkout"])
    assert retained.is_dir() and retained.parent.parent == tmp_path / "drive" / "state" / "review_checkouts"
    reviewers = outcome["outcome"]["retained_custody"]["reviewers"]
    assert {row["slot_id"] for _surface, row in reviewers if row.get("late_result_pending")} == {"t2"}
    assert outcome["outcome"]["retention_reason"]
    assert shared.git(repo, "worktree", "list", "--porcelain").count("worktree ") == 2


def test_operator_lane_without_isolation_reviews_this_worktree(tmp_path, monkeypatch):
    import scripts.run_external_review as module

    repo, briefs = _operator_fixture(tmp_path, monkeypatch)

    exit_code, output = _run_operator_lane(module, monkeypatch, tmp_path, "--no-isolated-checkout")

    assert exit_code == 0, (output / "outcome.json").read_text(encoding="utf-8")
    assert {Path(brief["session_root"]) for brief in briefs if brief["session_root"]} == {repo}
    assert not (tmp_path / "drive" / "state" / "review_checkouts").exists()


