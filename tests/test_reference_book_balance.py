"""The reference-book balance: a fact on every local surface, never a gate (BIBLE P3 c5).

The official CI lane (tests/test_reference_book_budgets.py) is what refuses a grown book;
here the same measurement reaches Ouroboros when it writes book text, plans book work,
runs readiness before a paid reviewer and asks for codebase health.
"""
from __future__ import annotations

import pathlib
import subprocess

import pytest

from ouroboros.reference_books import (
    BOOK_ENTRYPOINTS,
    BOOK_GROWTH_RULE,
    book_balance_note,
    book_balances,
    book_plan_fact,
    book_source_sizes,
    compose_book,
    composed_size,
    load_reference_book,
)
from tests.test_plan_review_engine import CLEAN, DECK_SPEC, _call, harness as _engine_harness

REPO = pathlib.Path(__file__).resolve().parents[1]
CHAPTER = "docs/architecture/01-one.md"
ENTRY = "# Architecture\n\nThe map.\n\n## Chapters\n\n- [One](architecture/01-one.md)\n"
BODY = "# One\n\nThe first chapter.\n\nP1 honest and long enough to shorten.\n"
harness = _engine_harness


def _git(root, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, check=True,
                   capture_output=True)


def _book_repo(root: pathlib.Path) -> pathlib.Path:
    for path, text in {BOOK_ENTRYPOINTS["architecture"]: ENTRY, CHAPTER: BODY,
                       "ouroboros/reference_books.py": "# the book contract\n", "ouroboros/loop.py": "x = 1\n",
                       "notes.md": "notes\n"}.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8", newline="\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def test_composed_size_is_the_composed_book_on_the_real_tree():
    for book_id in BOOK_ENTRYPOINTS:
        composed = len(compose_book(load_reference_book(REPO, book_id)).encode("utf-8"))
        assert composed_size(book_source_sizes(REPO, book_id)) == composed


@pytest.mark.serial
def test_balance_against_head_and_upstream(tmp_path):
    origin = _book_repo(tmp_path / "origin")
    _git(tmp_path, "clone", "-q", str(origin), "clone")
    clone = tmp_path / "clone"
    (clone / CHAPTER).write_text(BODY + "Committed growth.\n", encoding="utf-8", newline="\n")
    _git(clone, "commit", "-qam", "grow")
    (clone / CHAPTER).write_text(BODY + "Committed growth.\nMore.\n", encoding="utf-8", newline="\n")
    (balance,) = book_balances(clone, [CHAPTER])
    assert balance.vs_head == len("More.\n")
    assert balance.vs_upstream == len("Committed growth.\nMore.\n") == balance.owed
    assert balance.upstream.endswith("/master") or balance.upstream.endswith("/main")
    assert balance.changed == ((CHAPTER, len("More.\n")),)
    assert book_balances(clone, ["notes.md"]) == []  # no book source touched, nothing measured


@pytest.mark.serial
def test_note_states_the_rule_when_owed_and_one_quiet_line_when_paid(tmp_path):
    repo = _book_repo(tmp_path)
    (repo / CHAPTER).write_text(BODY + "Added.\n", encoding="utf-8", newline="\n")
    grown = book_balance_note(repo, [CHAPTER])
    assert "Architecture book" in grown and "+7 B vs HEAD" in grown and BOOK_GROWTH_RULE in grown
    (repo / CHAPTER).write_text(BODY.replace(" and long enough to shorten", ""), encoding="utf-8", newline="\n")
    paid = book_balance_note(repo, [CHAPTER])
    assert paid.startswith("ℹ️ Reference book:") and "nothing owed" in paid
    assert BOOK_GROWTH_RULE not in paid and "\n" not in paid
    assert book_balance_note(repo, ["notes.md"]) == ""


def _registry(tmp_path, *, external: bool):
    from ouroboros.tools.registry import ToolContext, ToolRegistry

    system = _book_repo(tmp_path / "system")
    drive = tmp_path / "drive"
    drive.mkdir()
    registry = ToolRegistry(repo_dir=system, drive_root=drive)
    if external:  # a user's own project that happens to carry the same docs layout
        project = _book_repo(tmp_path / "project")
        registry.set_context(ToolContext(repo_dir=system, system_repo_dir=system, drive_root=drive,
                                         workspace_root=project, workspace_mode="external"))
    return registry


GROW = {
    "write_file": {"path": CHAPTER, "content": BODY + "Added.\n"},
    "edit_text": {"path": CHAPTER, "old_str": "P1 honest", "new_str": "P1 honest, added"},
    "apply_patch": {"patch": f"*** Update File: {CHAPTER}\n-P1 honest and long enough to shorten.\n"
                             "+P1 honest and long enough to shorten, added.\n"},
    "edit_batch": {"edits": [{"path": CHAPTER, "old_str": "P1 honest", "new_str": "P1 honest, added"}]},
}


@pytest.mark.serial
@pytest.mark.parametrize("tool", sorted(GROW))
def test_a_body_book_edit_carries_the_balance(tmp_path, monkeypatch, tool):
    import ouroboros.safety as safety

    monkeypatch.setattr(safety, "check_safety", lambda *args, **kwargs: (True, ""))
    result = str(_registry(tmp_path, external=False).execute(tool, GROW[tool]))
    assert result.startswith("✅"), result[:300]
    assert "ℹ️ Reference books:" in result and BOOK_GROWTH_RULE in result


@pytest.mark.serial
@pytest.mark.parametrize("tool", sorted(GROW))
def test_a_foreign_project_or_a_non_book_path_gets_no_balance(tmp_path, monkeypatch, tool):
    import ouroboros.safety as safety

    monkeypatch.setattr(safety, "check_safety", lambda *args, **kwargs: (True, ""))
    foreign = str(_registry(tmp_path / "a", external=True).execute(tool, GROW[tool]))
    assert foreign.startswith("✅") and "Reference book" not in foreign, foreign[:300]
    plain = {"write_file": {"path": "notes.md", "content": "notes, more\n"},
             "edit_text": {"path": "notes.md", "old_str": "notes", "new_str": "notes, more"},
             "apply_patch": {"patch": "*** Update File: notes.md\n-notes\n+notes, more\n"},
             "edit_batch": {"edits": [{"path": "notes.md", "old_str": "notes", "new_str": "notes, more"}]}}[tool]
    body = str(_registry(tmp_path / "b", external=False).execute(tool, plain))
    assert body.startswith("✅") and "Reference book" not in body, body[:300]


def test_plan_fact_names_book_sources_and_new_modules_only(tmp_path):
    repo = _book_repo(tmp_path)
    chapter = book_plan_fact(repo, [CHAPTER])
    assert chapter.startswith("FACT:") and "docs/ARCHITECTURE.md" in chapter and BOOK_GROWTH_RULE in chapter
    module = book_plan_fact(repo, ["ouroboros/new_module.py"])
    assert "new module(s) ouroboros/new_module.py" in module and "Architecture-book source" in module
    assert book_plan_fact(repo, ["ouroboros/loop.py", "notes.md", "tests/test_new.py"]) == ""


def test_plan_task_carries_the_book_fact_for_a_system_repo_plan_only(harness):
    harness.install({"s1": CLEAN, "s2": CLEAN, "s3": CLEAN})
    chapter = {**DECK_SPEC, "affected_paths": [str(harness.system / "docs" / "architecture" / "06-agent-core.md")]}
    assert "FACT: affected_paths name book sources of docs/ARCHITECTURE.md" in _call(harness.make_ctx(), spec=chapter)
    module = {**DECK_SPEC, "affected_paths": [str(harness.system / "ouroboros" / "brand_new.py")]}
    assert "new module(s) ouroboros/brand_new.py" in _call(harness.make_ctx(task_id="task-2"), spec=module)
    existing = {**DECK_SPEC, "affected_paths": [str(harness.system / "ouroboros" / "loop.py")]}
    assert "FACT: affected_paths" not in _call(harness.make_ctx(task_id="task-3"), spec=existing)
    workspace = {**DECK_SPEC, "affected_paths": [str(harness.workspace / "docs" / "architecture" / "01-x.md")]}
    assert "FACT: affected_paths" not in _call(harness.make_ctx(task_id="task-4"), spec=workspace)


@pytest.mark.serial
def test_readiness_warns_on_a_grown_touched_book_only(tmp_path):
    from ouroboros.tools.review_helpers import check_worktree_readiness

    repo = _book_repo(tmp_path / "body")
    (repo / CHAPTER).write_text(BODY + "Added.\n", encoding="utf-8", newline="\n")
    grown = [w for w in check_worktree_readiness(repo) if "Architecture book" in w]
    assert len(grown) == 1 and grown[0].startswith("official CI will enforce:") and BOOK_GROWTH_RULE in grown[0]
    (repo / CHAPTER).write_text(BODY.replace(" and long enough to shorten", ""), encoding="utf-8", newline="\n")
    assert not [w for w in check_worktree_readiness(repo) if "book" in w]
    foreign = _book_repo(tmp_path / "foreign")
    (foreign / "ouroboros" / "reference_books.py").unlink()
    _git(foreign, "commit", "-qam", "no book contract")
    (foreign / CHAPTER).write_text(BODY + "Added.\n", encoding="utf-8", newline="\n")
    assert not [w for w in check_worktree_readiness(foreign) if "book" in w]
