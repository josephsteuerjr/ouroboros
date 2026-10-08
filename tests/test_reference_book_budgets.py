"""A change does not make a reference book larger (the official line's rule).

Each book is measured as the UTF-8 length of its COMPOSED text (the entrypoint and every
listed chapter, ``reference_books.compose_book``) at this change's tip and at its event base.
The rule is pairwise: there is no stored number and no reserve, so text a change adds to a
book is paid by shortening the same book in the same change.

Official-CI ``size_ratchet`` lane only: local runs exclude the marker and every local surface
reports the same balance as a fact (BIBLE P3 c5). The workflow sets ``OURO_BOOK_GROWTH_MODE``:
``block`` for the official repository's owner/member/collaborator pull requests and its
``ouroboros`` pushes, ``warn`` for outside contributors and any fork's own CI, ``approved``
when the repository owner applied the ``book-growth`` label. Unset means ``block``, so an
operator's explicit local run of the lane gets the hard answer.
"""
from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

from ouroboros.reference_books import BOOK_ENTRYPOINTS, compose_book, load_reference_book

REPO = pathlib.Path(__file__).resolve().parents[1]
BASE_REF_ENV = "OURO_SIZE_RATCHET_BASE_REF"
MODE_ENV = "OURO_BOOK_GROWTH_MODE"


def _git(repo: pathlib.Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, check=check, capture_output=True)


def measures_one_change(environ) -> bool:
    """A pull request or a push to ``ouroboros`` is one change; a push to ``main`` or
    ``ouroboros-stable`` spans a whole release, which the pairwise rule cannot attribute."""
    return environ.get("GITHUB_EVENT_NAME") != "push" or environ.get("GITHUB_REF") == "refs/heads/ouroboros"


def growth_base(repo: pathlib.Path, ref: str | None) -> str:
    """The commit this change is measured from; ``""`` when the rule does not apply.

    A run without a resolvable event base skips: a local or manual run has none, a tag push
    carries all zeros, and HEAD's parent is not where a multi-commit change began.
    """
    ref = (ref or "").strip()
    if not ref:
        return ""
    resolved = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    return resolved.stdout.decode("ascii").strip() if resolved.returncode == 0 else ""


def composed_bytes(repo: pathlib.Path, book_id: str, ref: str = "") -> int | None:
    """The composed book's UTF-8 bytes in the checkout, or at ``ref``; ``None`` when absent there."""
    reader = None
    if ref:
        def reader(path: str) -> bytes:
            return _git(repo, "show", f"{ref}:{path}").stdout
    try:
        return len(compose_book(load_reference_book(repo, book_id, reader)).encode("utf-8"))
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError):
        if not ref:
            raise
        return None


def book_growth_verdict(book_id: str, base: int | None, tip: int, mode: str) -> tuple[bool, str]:
    """``(passes, message)`` for one book; the message is empty when the book did not grow."""
    if base is None or tip <= base:
        return True, ""
    name = f"the {book_id.title()} book"
    if mode == "approved":
        return True, f"{name} grows by {tip - base} bytes; the repository owner approved it (book-growth label)."
    if mode == "warn":
        return True, (f"this PR grows {name} by {tip - base} bytes; maintainers will make room at "
                      "integration, nothing is required from you.")
    return False, (f"{name} grew by {tip - base} bytes ({base} -> {tip}); a change ends each book no larger "
                   "than at its base, so shorten the same book in this change (the owner's book-growth "
                   "label on the PR is the only exception).")


@pytest.mark.size_ratchet
def test_a_change_does_not_grow_a_reference_book(capsys):
    if not measures_one_change(os.environ):
        pytest.skip("a push to a release branch spans many changes; the rule measures one")
    base = growth_base(REPO, os.environ.get(BASE_REF_ENV))
    if not base:
        pytest.skip(f"{BASE_REF_ENV} names no base commit this clone holds, so this change cannot be measured")
    mode = (os.environ.get(MODE_ENV) or "block").strip()
    faults: list[str] = []
    for book_id, entrypoint in BOOK_ENTRYPOINTS.items():
        passes, message = book_growth_verdict(book_id, composed_bytes(REPO, book_id, base),
                                              composed_bytes(REPO, book_id), mode)
        if message and passes:  # an annotation in the job log; a captured print would never reach it
            with capsys.disabled():
                print(f"::{'notice' if mode == 'approved' else 'warning'} file={entrypoint}::{message}")
        elif message:
            faults.append(message)
    assert not faults, f"Reference-book growth since {base[:12]}:\n" + "\n".join(faults)


# ---------------------------------------------------------------- the decision, unmarked

@pytest.mark.parametrize("base,tip,mode,passes,said", [
    (1000, 1001, "block", False, "grew by 1 bytes"),
    (1000, 1001, "", False, "grew by 1 bytes"),  # an unknown mode is never a pass
    (1000, 1480, "warn", True, "maintainers will make room"),
    (1000, 1480, "approved", True, "owner approved"),
    (1000, 900, "block", True, ""),
    (1000, 1000, "block", True, ""),
    (None, 5000, "block", True, ""),  # a book new in this change has no base to grow from
])
def test_book_growth_verdict(base, tip, mode, passes, said):
    verdict = book_growth_verdict("architecture", base, tip, mode)
    assert verdict[0] is passes
    assert (said in verdict[1]) if said else verdict[1] == ""


@pytest.mark.serial
def test_composed_bytes_measure_the_book_at_the_base_commit(tmp_path):
    entry = "# Architecture\n\nIntro café.\n\n## Chapters\n\n- [One](architecture/01-one.md)\n"
    chapter = "# One\n\nThe first chapter, naïve.\n"
    for path, text in {BOOK_ENTRYPOINTS["architecture"]: entry, "docs/architecture/01-one.md": chapter}.items():
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(text, encoding="utf-8", newline="\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    expected = len((entry + "\n\n" + chapter).encode("utf-8"))
    assert composed_bytes(tmp_path, "architecture", "HEAD") == expected == composed_bytes(tmp_path, "architecture")
    (tmp_path / "docs/architecture/01-one.md").write_text(chapter + "More.\n", encoding="utf-8", newline="\n")
    assert composed_bytes(tmp_path, "architecture", "HEAD") == expected  # the base does not move with the checkout
    assert composed_bytes(tmp_path, "architecture") == expected + len("More.\n")
    assert composed_bytes(tmp_path, "development", "HEAD") is None  # absent at the base


@pytest.mark.serial
def test_growth_base_is_the_event_base_and_nothing_else(tmp_path):
    _git(tmp_path, "init", "-q")
    shas = []
    for text in ("one\n", "one\ntwo\n"):
        (tmp_path / "f.txt").write_text(text, encoding="utf-8")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", text)
        shas.append(_git(tmp_path, "rev-parse", "HEAD").stdout.decode().strip())
    assert growth_base(tmp_path, shas[0]) == shas[0]
    assert growth_base(tmp_path, f" {shas[0][:12]} ") == shas[0]
    for absent in (None, "", "   ", "0" * 40, "f" * 40):
        assert growth_base(tmp_path, absent) == "", absent


@pytest.mark.parametrize(("environ", "applies"), [
    ({}, True),  # a local run with an explicit base
    ({"GITHUB_EVENT_NAME": "pull_request", "GITHUB_REF": "refs/pull/7/merge"}, True),
    ({"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/ouroboros"}, True),
    ({"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/ouroboros-stable"}, False),
    ({"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main"}, False),
])
def test_the_rule_measures_one_change_not_a_release_range(environ, applies):
    assert measures_one_change(environ) is applies


# ------------------------------------------------- the workflow's mode step, run for real

OWNER_LABEL = "labeled razzant"


@pytest.mark.parametrize("job", ["quick-test", "full-test"])
@pytest.mark.parametrize("repo,event,association,events,mode", [
    ("razzant/ouroboros", "pull_request", "COLLABORATOR", "", "block"),
    ("razzant/ouroboros", "pull_request", "OWNER", "", "block"),
    ("razzant/ouroboros", "pull_request", "CONTRIBUTOR", "", "warn"),
    ("razzant/ouroboros", "pull_request", "FIRST_TIME_CONTRIBUTOR", "", "warn"),
    ("someone/fork", "pull_request", "OWNER", "", "warn"),  # a fork's own CI never blocks
    ("someone/fork", "push", "", "", "warn"),
    ("razzant/ouroboros", "push", "", "", "block"),
    ("razzant/ouroboros", "push", "", OWNER_LABEL, "approved"),
    ("razzant/ouroboros", "pull_request", "COLLABORATOR", OWNER_LABEL, "approved"),
    # Ouroboros's own token can apply the label, but only the repository owner's approves.
    ("razzant/ouroboros", "pull_request", "COLLABORATOR", "labeled ouroboros-agent", "block"),
    ("razzant/ouroboros", "pull_request", "COLLABORATOR", OWNER_LABEL + "\nunlabeled razzant", "block"),
    ("razzant/ouroboros", "pull_request", "COLLABORATOR", "FAIL", "block"),  # unreadable label
])
def test_the_workflow_decides_the_growth_mode(tmp_path, job, repo, event, association, events, mode):
    import shutil

    import yaml

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("workflow shell is unavailable on this host")
    workflow = yaml.safe_load((REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    step = next(s for s in workflow["jobs"][job]["steps"] if s.get("id") == "book_mode")
    size = next(s for s in workflow["jobs"][job]["steps"] if s.get("id") == "tests_size")
    assert size["env"][MODE_ENV] == "${{ steps.book_mode.outputs.mode }}"
    stub = ('gh() { case "$2" in *"/pulls") echo 7 ;; *) [ "$EVENTS" = FAIL ] && return 1; '
            '[ -n "$EVENTS" ] && printf "%s\\n" "$EVENTS"; return 0 ;; esac; }\n')
    output = tmp_path / "output"
    env = {**os.environ, "REPO": repo, "OWNER": "razzant", "EVENT": event, "ASSOCIATION": association,
           "PR_NUMBER": "7", "SHA": "abc", "EVENTS": events, "GITHUB_OUTPUT": str(output)}
    done = subprocess.run([bash, "-e", "-o", "pipefail", "-c", stub + step["run"]], env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert done.returncode == 0, done.stderr
    assert output.read_text(encoding="utf-8").strip() == f"mode={mode}"
    assert ("could not read the book-growth label" in done.stdout) is (events == "FAIL")
