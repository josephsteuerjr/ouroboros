"""An isolated review checkout outlives the wave exactly while custody is open.

Three layers, one rule (R4): the runtime's ``isolated_checkout`` keeps the
worktree when its ``retain`` answer names open custody (or cannot answer), the
review operation records the retention on its result and ledger record, and the
operator lane of the external review wrapper writes it into the typed outcome.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import ouroboros.review_substrate as substrate
from ouroboros.review_state import AdvisoryRunRecord, compute_snapshot_hash, make_repo_key, update_state
from ouroboros.tools import git
from ouroboros.tools import review_subject
from ouroboros.tools.registry import ToolContext
from ouroboros.tools.review_change import run_review_change
from ouroboros.tools.review_subject import ReviewSubjectSpec, isolated_checkout
from scripts import run_external_review as runner
from tests import _contributor_packet_shared as shared
from tests.test_advisory_inline_freshness import candidate  # noqa: F401


def _checkouts(drive: Path) -> list[Path]:
    root = drive / "state" / "review_checkouts"
    return sorted(root.iterdir()) if root.exists() else []


@pytest.mark.parametrize("answer", ["settled", "open", "unreadable"])
def test_isolated_checkout_lives_exactly_while_custody_is_open(tmp_path, answer):
    fixture = shared.init_installed_body(tmp_path)
    repo, drive = Path(fixture["repo"]), tmp_path / "drive"
    ctx = ToolContext(repo_dir=repo, drive_root=drive)
    spec = ReviewSubjectSpec(root_kind="system_repo", root=str(repo), kind="base..head",
                             base=fixture["base_sha"], head=fixture["head_sha"], surface="change")

    def retain():
        if answer == "unreadable":
            raise OSError("custody ledger unreadable")
        return {"seats": ["t2"]} if answer == "open" else {}

    with isolated_checkout(ctx, spec, retain=retain) as frozen:
        checkout = Path(frozen.checkout)
        assert checkout.is_dir() and frozen.tree_sha == fixture["head_tree_sha"]

    assert checkout.exists() is (answer != "settled")
    assert _checkouts(drive) == ([checkout.parent] if answer != "settled" else [])
    assert review_subject.checkout_retention(retain) == (
        {} if answer == "settled" else {"seats": ["t2"]} if answer == "open"
        else {"custody_unreadable": "OSError: custody ledger unreadable"})


@pytest.mark.parametrize("pending", [False, True], ids=["settled", "seat-open"])
def test_review_change_records_the_retained_checkout(tmp_path, monkeypatch, pending):
    """The operation reads custody INSIDE its wave context (where the wave's own
    review state still sits on ``ctx``) and names the retained checkout on the
    result and the ledger record; a settled wave leaves nothing behind."""
    from ouroboros import review_ledger

    fixture = shared.init_installed_body(tmp_path)
    repo, drive = Path(fixture["repo"]), tmp_path / "drive"
    monkeypatch.setenv("OUROBOROS_REVIEWER_SLOTS", json.dumps(runner._slot_plan_payload(shared.GOLDEN_CONFIG)))
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")
    settled = shared.golden_substrate([])

    def run_review_request(request, *, slots, **kwargs):
        result = settled(request, slots=slots, **kwargs)
        if pending:
            for actor in result.actors:
                if actor["slot_id"] == "t2":
                    actor.update(status="error", raw_text="", error="logical wait expired",
                                 operation_state="in_flight", late_result_pending=True)
        return result

    monkeypatch.setattr(substrate, "run_review_request", run_review_request)
    ctx = ToolContext(repo_dir=repo, drive_root=drive)

    result = run_review_change(ctx, root="system_repo", surface="change", goal="pending custody",
                               subject="base..head", base=fixture["base_sha"], head=fixture["head_sha"])

    subject = result["subject"]
    record = review_ledger.load_record(drive, result["record_id"])
    assert record["subject"]["retained_checkout"] == subject["retained_checkout"]
    assert record["subject"]["retention"] == subject["retention"]
    if pending:
        retained = Path(subject["retained_checkout"])
        assert retained.is_dir() and retained == Path(subject["checkout"])
        assert subject["retention"]["seats"] == ["t2"]
        assert subject["retention"].get("review_custody_pending") is True
        assert result["state"] == "pending" and result["aggregate"] != "PASS"
        assert _checkouts(drive) == [retained.parent]
    else:
        assert (subject["retained_checkout"], subject["retention"]) == ("", {})
        assert result["aggregate"] == "PASS" and _checkouts(drive) == []
    assert shared.git(repo, "worktree", "list", "--porcelain").count("worktree ") == (2 if pending else 1)


@pytest.mark.parametrize("mode", ["preflight", "wave", "exception_pending", "base_exception_pending", "finished",
                                  "exception_empty"])
def test_wrapper_retains_only_unresolved_custody(candidate, monkeypatch, tmp_path, mode):  # noqa: F811
    repo = candidate.repo_dir
    (repo / "VERSION").write_text("1.0.0\n")
    output, drive = tmp_path / "output", tmp_path / "review-data"
    monkeypatch.setattr(runner, "REPO", repo)
    args = SimpleNamespace(contributor=False, commit_message="candidate", goal="", scope="",
                           output=str(output), drive_root=str(drive), no_isolated_checkout=False)
    monkeypatch.setattr(runner, "_parse_args", lambda: args)
    monkeypatch.setattr(runner, "_prepare_review_configuration", lambda args: (None, {}))
    monkeypatch.setattr(runner, "_advisory_unavailability_warning", lambda: "")
    monkeypatch.setattr("ouroboros.tools.claude_advisory_review._handle_advisory_pre_review",
                        lambda *a, **kw: pytest.fail("wrapper must not pay before cycle admission"))
    created: list[Path] = []
    materialize = review_subject.isolated_checkout

    def capture_checkout(ctx, spec, **kwargs):
        import contextlib

        @contextlib.contextmanager
        def observed():
            with materialize(ctx, spec, **kwargs) as frozen:
                created.append(Path(frozen.checkout))
                yield frozen

        return observed()

    monkeypatch.setattr(review_subject, "isolated_checkout", capture_checkout)
    full_source = "complete received review\n" * 100

    class Crash(BaseException):
        pass

    def cycle(ctx, message, **kwargs):
        assert kwargs["skip_advisory_review"] is False
        (ctx.repo_dir / "late-untracked.txt").write_text("preserve without staging\n")
        if mode in {"preflight", "exception_pending", "base_exception_pending"}:
            update_state(ctx.drive_root, lambda state: state.add_run(AdvisoryRunRecord(
                snapshot_hash=compute_snapshot_hash(ctx.repo_dir), commit_message=message,
                repo_key=make_repo_key(ctx.repo_dir), status="pending", ts="2026-09-06T00:00:00Z",
                raw_result=full_source,
                execution={"invocation_id": "inv", "pending_invocation_id": "inv", "operation_state": "in_flight"},
            )))
        if mode == "wave":
            ctx._last_triad_raw_results = [{"slot_id": "s", "operation_id": "op", "operation_state": "in_flight",
                                            "late_result_pending": True}]
        if mode == "base_exception_pending":
            raise Crash("checkpointed before local result")
        if mode.startswith("exception"):
            raise RuntimeError("cycle interrupted")
        return {"status": "blocked", "block_reason": "preflight", "message": "recorded refusal"}

    monkeypatch.setattr(git, "_run_non_committing_review_cycle", cycle)
    try:
        if mode == "base_exception_pending":
            with pytest.raises(Crash):
                runner.main()
        elif mode.startswith("exception"):
            with pytest.raises(RuntimeError, match="cycle interrupted"):
                runner.main()
        else:
            assert runner.main() == 3
        [checkout] = created
        pending = mode in {"preflight", "wave", "exception_pending", "base_exception_pending"}
        assert checkout.exists() == pending
        assert bool(_checkouts(drive)) == pending
        if pending:
            assert "late-untracked.txt" not in runner._git_text(["diff", "--cached", "--name-only"], cwd=checkout)
            result = json.loads((output / "outcome.json").read_text())
            assert result["exit_code"] == 3
            assert result["outcome"]["retained_checkout"] == str(checkout)
            assert result["outcome"]["retained_custody"]
        if mode == "preflight":
            assert json.loads((output / "advisory.txt").read_text())["raw_result"] == full_source
        if mode == "finished":
            assert json.loads((output / "advisory.txt").read_text())["status"] == "not_run"
    finally:
        for checkout in created:
            if checkout.exists():
                review_subject._git_bytes(repo, ["worktree", "remove", "--force", str(checkout)])
