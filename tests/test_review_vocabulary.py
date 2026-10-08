"""Model- and owner-facing texts name the review panel and the review pool (V-D4-07).

Commit, plan, skill and task-acceptance review all run on one review pool, so "triad + scope", a
"reviewer-slot configuration" or a "configured triad row" describes lanes that no longer exist.
Comments are out of scope; the remaining owners of the old wording are named below.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STALE = re.compile(
    r"triad \+ scope|reviewer-slot (?:configuration|skill review)|configured triad row|scope review runs",
    re.IGNORECASE,
)
# Protected (changes need the owner's approval) and the gate's own messages (the next package).
RESIDUAL = {"ouroboros/runtime_mode_policy.py", "ouroboros/tools/review.py"}
SURFACES = ("ouroboros/**/*.py", "supervisor/**/*.py", "web/modules/**/*.js", "prompts/*.md",
            "docs/CREATING_SKILLS.md")


def _text_lines(path: Path):
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.lstrip().startswith(("#", "//", "*")):
            yield number, line


def test_model_and_owner_texts_carry_no_retired_review_vocabulary():
    found = [
        f"{rel}:{number}: {line.strip()}"
        for pattern in SURFACES
        for path in sorted(REPO.glob(pattern))
        if (rel := path.relative_to(REPO).as_posix()) not in RESIDUAL
        for number, line in _text_lines(path)
        if STALE.search(line)
    ]
    assert found == []


def test_the_skill_review_tool_names_the_review_panel_and_the_pool():
    from ouroboros.tools.skill_exec import _REVIEW_SCHEMA

    assert "Run skill review by the review panel" in _REVIEW_SCHEMA["description"]
    assert "using the review pool configuration" in _REVIEW_SCHEMA["description"]
