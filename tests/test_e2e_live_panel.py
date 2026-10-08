"""The stand's review panel (owner decision 2026-09-06): three model families at effort low on every reviewer,
task and evolution at medium, written into every paid lane's settings; ``--production-panel`` leaves the tree's
own defaults in place and the stub lane keeps its loopback rows. The stand still writes the panel under the
lane-era key, so the product's read seam (``normalize_settings_raw`` -> ``review_pool_migration``) must turn it
into the review pool every surface runs, or the review organ would fall back silently."""
from __future__ import annotations

import json

from devtools.e2e_live import run_live_lanes, scenarios
from ouroboros.config import normalize_settings_raw
from ouroboros.review_model_routes import adaptive_quorum
from ouroboros.reviewer_slot_config import review_pool_rows
from ouroboros.settings_defaults import OPENROUTER_REVIEW_DEFAULTS

FAKE_KEY = "sk-or-v1-e2e-live-test-key-value-never-printed-0123456789"


def test_the_stand_panel_reads_as_a_pool_of_three_families_through_the_migration():
    document = normalize_settings_raw(dict(scenarios.STAND_PANEL_SETTINGS))
    assert "OUROBOROS_REVIEWER_SLOTS" not in document and "OUROBOROS_EFFORT_REVIEW" not in document
    pool = [(row.target_id, row.effort, row.retrieves) for row in review_pool_rows(document)]
    # The three triad rows pack the brief at effort low; the scope row joins the pool reading natively.
    assert pool == [("google/gemini-3.8-flash", "low", False), ("openai/gpt-5.6-luna", "low", False),
                    ("deepseek/deepseek-v4-pro", "low", False), ("deepseek/deepseek-v4-pro", "low", True)]
    assert {m.split("/")[0] for m, _, _ in pool} == {"google", "openai", "deepseek"}
    # The advisory reviewer is an unmarked catalog row the author may still name for a preflight.
    catalog = json.loads(document["OUROBOROS_SUBAGENTS"])["items"]
    advisory = [row for row in catalog if row["route"]["target_id"] == "anthropic/claude-sonnet-5"]
    assert len(advisory) == 1 and not advisory[0].get("review_eligible") and advisory[0]["effort"] == "low"
    assert document["OUROBOROS_EFFORT_TASK"] == "medium" and document["OUROBOROS_EFFORT_EVOLUTION"] == "medium"


def test_paid_lanes_carry_the_panel_unless_production_panel_or_stub(monkeypatch):
    # parse_args reads tempfile.gettempdir(), which caches the session's temp dir; a TMPDIR
    # env change never reaches it (test_e2e_live_runner.py's _short_tmp patches the same seam).
    monkeypatch.setattr(run_live_lanes.tempfile, "gettempdir", lambda: "/tmp")
    paid = run_live_lanes.effective_settings(run_live_lanes.parse_args(["--out", "/tmp/x"]), FAKE_KEY)
    assert json.loads(paid["OUROBOROS_REVIEWER_SLOTS"]) == scenarios.STAND_REVIEW_PANEL
    assert paid["OUROBOROS_EFFORT_REVIEW"] == "low" and paid["OUROBOROS_EFFORT_TASK"] == "medium"
    production = run_live_lanes.effective_settings(run_live_lanes.parse_args(["--out", "/tmp/x", "--production-panel"]), FAKE_KEY)
    assert not production.get("OUROBOROS_REVIEWER_SLOTS") and "OUROBOROS_EFFORT_REVIEW" not in production
    # The production document names neither lanes nor a catalog: through the read seam it is
    # a never-configured install and runs the factory OpenRouter triad (quorum 2 of 3), so
    # the lane reviews with the tree's own default panel instead of an empty pool.
    assert "OUROBOROS_SUBAGENTS" not in production
    pool = review_pool_rows(normalize_settings_raw(dict(production)))
    assert [row.target_id for row in pool] == list(OPENROUTER_REVIEW_DEFAULTS["triad"]) and adaptive_quorum(len(pool)) == 2
    stub = run_live_lanes.effective_settings(run_live_lanes.parse_args(["--stub", "--out", "/tmp/x"]), "")
    assert stub.get("OUROBOROS_REVIEWER_SLOTS") != scenarios.STAND_PANEL_SETTINGS["OUROBOROS_REVIEWER_SLOTS"]
    assert "OUROBOROS_EFFORT_REVIEW" not in stub
