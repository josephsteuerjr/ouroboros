#!/usr/bin/env python3
"""Decide whom the reference-book growth rule binds in this CI run.

Prints ``reference-book growth mode: <mode>`` and appends ``mode=<mode>`` to ``$GITHUB_OUTPUT``;
the size lane reads it as ``OURO_BOOK_GROWTH_MODE`` (tests/test_reference_book_budgets.py).

The rule binds where a change is proposed: in the official repository a pull request by its
owner, a member or a collaborator blocks a grown book. An outside contributor's pull request,
a push (the merge of a pull request already decided) and any fork's own CI only warn. The
``book-growth`` label approves a pull request when the repository owner applied it last; the
label's events are read live, so a re-run sees a label added later, and a label that cannot be
read approves nothing.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

OFFICIAL_REPOSITORY = "razzant/ouroboros"
LABEL = "book-growth"
BINDING_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


def binds(repo: str, event: str, association: str) -> bool:
    return repo == OFFICIAL_REPOSITORY and event == "pull_request" and association in BINDING_ASSOCIATIONS


def decide(repo: str, owner: str, event: str, association: str, label_events: list | None) -> str:
    """``block``, ``warn`` or ``approved``; ``label_events`` is ``None`` when they could not be read."""
    if not binds(repo, event, association):
        return "warn"
    history = [item for item in label_events or []
               if item.get("event") in ("labeled", "unlabeled") and (item.get("label") or {}).get("name") == LABEL]
    if history and history[-1]["event"] == "labeled" and (history[-1].get("actor") or {}).get("login") == owner:
        return "approved"
    return "block"


def read_label_events(repo: str, number: str, token: str) -> list:
    """Every event of issue/PR ``number``, following the API's pagination."""
    events: list = []
    url = f"https://api.github.com/repos/{repo}/issues/{number}/events?per_page=100"
    while url:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
            events.extend(json.load(response))
            link = re.search(r'<([^>]+)>;\s*rel="next"', response.headers.get("Link") or "")
        url = link.group(1) if link else ""
    return events


def main(environ=os.environ, read=read_label_events) -> str:
    repo, event = environ.get("REPO", ""), environ.get("EVENT", "")
    association, number = environ.get("ASSOCIATION", ""), environ.get("PR_NUMBER", "")
    events = None
    if binds(repo, event, association) and number:
        try:
            events = read(repo, number, environ.get("GH_TOKEN", ""))
        except Exception as exc:  # noqa: BLE001 -- any failure means the label is unknown
            print(f"::warning::could not read the {LABEL} label of #{number} ({exc}); no exception applied")
    mode = decide(repo, environ.get("OWNER", ""), event, association, events)
    print(f"reference-book growth mode: {mode}")
    if environ.get("GITHUB_OUTPUT"):
        with open(environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write(f"mode={mode}\n")
    return mode


if __name__ == "__main__":
    main()
    sys.exit(0)
