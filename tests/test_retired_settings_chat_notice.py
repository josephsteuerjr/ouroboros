"""The owner-facing chat notice about retired settings keys (D-07).

``config.normalize_settings_raw`` drops the keys a release retired and says so on the
module logger — a line an owner who never opens the Logs panel does not see. The
supervisor boot tells the OWNER once, in their chat, from the sets that read seam
recorded, with the same sentence, deduplicated durably per retired-key set in
``state.json``. These tests pin: emitted once for a document carrying a retired key,
not emitted without one, not repeated on a second boot, not sent (and not marked)
before an owner chat is bound, the successor named truthfully (the reviewer comma-lists
are replaced by the review pool), the review-lane keys consumed by the pool migration
never reported as a loss, and the boot wiring itself.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from ouroboros import config as cfg
from ouroboros import review_pool_migration as rpm
from ouroboros import server_maintenance
from supervisor import message_bus, state


@pytest.fixture
def boot_state(tmp_path, monkeypatch):
    """A supervisor state root at ``tmp_path`` with a fresh in-process retirement seam."""
    state.init(tmp_path)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "locks").mkdir(parents=True, exist_ok=True)
    state.save_state({})  # an initialized install: only explicit init creates state (#1307)
    cfg._RETIREMENT_NOTICE_SEEN.clear()
    rpm._MIGRATIONS_SEEN.clear()
    yield tmp_path
    cfg._RETIREMENT_NOTICE_SEEN.clear()
    rpm._MIGRATIONS_SEEN.clear()


@pytest.fixture
def sent(monkeypatch):
    rows: list = []
    monkeypatch.setattr(
        message_bus, "send_with_budget",
        lambda chat_id, text, *args, **kwargs: rows.append((chat_id, text, kwargs)),
    )
    return rows


def _bind_owner(chat_id: int = 1) -> None:
    state.update_state(lambda st: st.__setitem__("owner_chat_id", chat_id))


RETIRED_DOC = {
    "OUROBOROS_REVIEW_MODELS": "a/one,b/two",
    "OUROBOROS_SCOPE_REVIEW_MODEL": "c/three",
    "TOTAL_BUDGET": 10.0,
}


def test_notice_reaches_the_owner_chat_once_per_retired_key_set(boot_state, sent):
    _bind_owner(1)
    loaded = cfg.normalize_settings_raw(dict(RETIRED_DOC))

    server_maintenance._startup_retired_settings_notice(loaded)

    assert len(sent) == 1, sent
    chat_id, text, kwargs = sent[0]
    assert chat_id == 1
    assert kwargs == {"role": "system", "system_type": "retired_settings_notice"}
    for key in ("OUROBOROS_REVIEW_MODELS", "OUROBOROS_SCOPE_REVIEW_MODEL"):
        assert key in text, key
    assert "NOT honored" in text
    assert "OUROBOROS_SUBAGENTS" in text, "the successor surface (the review pool) is named"
    assert "review pool" in text
    assert "TOTAL_BUDGET" not in text

    # The durable marker is keyed by the exact retired-key set.
    marker = state.load_state().get("retired_settings_notified")
    assert isinstance(marker, dict)
    assert list(marker) == ["OUROBOROS_REVIEW_MODELS,OUROBOROS_SCOPE_REVIEW_MODEL"]

    # A second apply (supervisor revival, next boot) does not repeat it.
    server_maintenance._startup_retired_settings_notice(loaded)
    assert len(sent) == 1


def test_no_notice_without_a_retired_key(boot_state, sent):
    _bind_owner(1)
    loaded = cfg.normalize_settings_raw({"TOTAL_BUDGET": 10.0})

    server_maintenance._startup_retired_settings_notice(loaded)

    assert sent == []
    assert "retired_settings_notified" not in state.load_state()


def test_the_durable_marker_survives_a_fresh_process(boot_state, sent):
    """The dedupe is the state file, not the in-process seam: a new process that reads
    the same document again (the seam's own set is empty there) still stays quiet."""
    _bind_owner(1)
    loaded = cfg.normalize_settings_raw(dict(RETIRED_DOC))
    server_maintenance._startup_retired_settings_notice(loaded)
    assert len(sent) == 1

    cfg._RETIREMENT_NOTICE_SEEN.clear()  # "fresh process"
    loaded = cfg.normalize_settings_raw(dict(RETIRED_DOC))
    server_maintenance._startup_retired_settings_notice(loaded)
    assert len(sent) == 1

    # A DIFFERENT retired-key set is its own loss and gets its own line. The key is taken
    # from the successor table, never spelled here: the grep-class retirement gate
    # (tests/test_legacy_timeout_retirement.py) keeps retired names out of live surfaces.
    from ouroboros.settings_defaults import RETIRED_SETTING_SUCCESSORS

    retired_key, successors = next(iter(RETIRED_SETTING_SUCCESSORS.items()))
    cfg.normalize_settings_raw({retired_key: "5"})
    server_maintenance._startup_retired_settings_notice(loaded)
    assert len(sent) == 2
    assert retired_key in sent[1][1]
    assert successors[0] in sent[1][1], "the successor table is read"


def test_nothing_is_sent_or_marked_before_an_owner_chat_is_bound(boot_state, sent):
    loaded = cfg.normalize_settings_raw(dict(RETIRED_DOC))

    server_maintenance._startup_retired_settings_notice(loaded)
    assert sent == []
    assert "retired_settings_notified" not in state.load_state()

    # The first boot that HAS an owner chat delivers it.
    _bind_owner(7)
    server_maintenance._startup_retired_settings_notice(loaded)
    assert [row[0] for row in sent] == [7]


# A structured panel the strict parser ACCEPTS (slot ids, typed routes, both groups).
AUTHORED_SLOTS = (
    '{"triad": [{"slot_id": "t1", "route": {"kind": "api_chat", "target_id": "x/y"}}], '
    '"scope": [{"slot_id": "s1", "route": {"kind": "api_chat", "target_id": "x/y"}}]}'
)


MALFORMED_SLOTS = '{"triad": [{"model": "x/y"}]}'  # a row without slot_id/route: rejected


def test_the_comma_list_clause_names_the_review_pool():
    """The sentence itself: the reviewer comma-lists are replaced by the rows of the
    subagent catalog marked Reviewer — one static fact, because which rows run is the
    review-pool migration's own report, not this notice's."""
    from ouroboros.settings_defaults import retired_setting_keys_notice

    text = retired_setting_keys_notice(("OUROBOROS_REVIEW_MODELS",))
    assert "OUROBOROS_REVIEW_MODELS" in text and "review pool" in text
    assert "OUROBOROS_SUBAGENTS" in text and "Settings → Agents" in text
    for stale in ("SHIPPED", "authored in that setting", "NO reviewer panel", "OUROBOROS_REVIEWER_SLOTS"):
        assert stale not in text, (stale, text)


def test_migrated_review_lane_keys_are_consumed_not_reported_as_a_loss(boot_state, sent, caplog):
    """An authored panel is migrated into the subagent catalog BEFORE the purge: the lane
    key and the surface effort keys leave the document as consumed, so neither the chat
    notice nor the read-seam log line lists them among the dropped keys — only the
    comma-lists, which the migration never read (ABI-10), are a loss to report."""
    import logging

    _bind_owner(1)
    doc = dict(RETIRED_DOC, OUROBOROS_REVIEWER_SLOTS=AUTHORED_SLOTS, OUROBOROS_EFFORT_REVIEW="medium")
    with caplog.at_level(logging.WARNING, logger="ouroboros.config"):
        loaded = cfg.normalize_settings_raw(doc)
    server_maintenance._startup_retired_settings_notice(loaded)

    assert "OUROBOROS_REVIEWER_SLOTS" not in loaded and "OUROBOROS_EFFORT_REVIEW" not in loaded
    catalog = json.loads(loaded["OUROBOROS_SUBAGENTS"])
    # t1 (packet, medium from the surface key) and s1 (reads, high) are two engines: two rows.
    assert [row["subagent_id"] for row in catalog["items"] if row.get("review_eligible")] == ["review-1", "review-2"]
    assert len(sent) == 1
    text = sent[0][1]
    assert "OUROBOROS_REVIEW_MODELS" in text
    assert "OUROBOROS_REVIEWER_SLOTS" not in text and "OUROBOROS_EFFORT_REVIEW" not in text
    log_lines = [r.getMessage() for r in caplog.records if "retired" in r.getMessage()]
    assert len(log_lines) == 1 and "OUROBOROS_REVIEWER_SLOTS" not in log_lines[0]
    assert list(state.load_state()["retired_settings_notified"]) == [
        "OUROBOROS_REVIEW_MODELS,OUROBOROS_SCOPE_REVIEW_MODEL"]


def test_a_malformed_reviewer_slots_setting_is_kept_for_the_owner_not_dropped(boot_state, sent, caplog):
    """A lane value the strict parser rejects cannot be migrated, and a key the owner must
    still repair is not a loss to announce: the migration keeps it in the document (its own
    report names the error), the purge leaves it alone, and the retired-keys notice lists
    only the comma-lists."""
    import logging

    from ouroboros.review_pool_migration import parse_reviewer_slots

    with pytest.raises(ValueError):
        parse_reviewer_slots({}, MALFORMED_SLOTS)

    _bind_owner(1)
    doc = dict(RETIRED_DOC, OUROBOROS_REVIEWER_SLOTS=MALFORMED_SLOTS)
    with caplog.at_level(logging.WARNING, logger="ouroboros.config"):
        loaded = cfg.normalize_settings_raw(doc)
    server_maintenance._startup_retired_settings_notice(loaded)

    assert loaded["OUROBOROS_REVIEWER_SLOTS"] == MALFORMED_SLOTS, "kept until the owner's catalog save"
    assert "OUROBOROS_SUBAGENTS" not in loaded, "no partial migration"
    assert len(sent) == 1 and "OUROBOROS_REVIEWER_SLOTS" not in sent[0][1]
    outcomes = cfg.review_pool_migrations_seen()
    assert len(outcomes) == 1 and outcomes[0].error and outcomes[0].retained_keys == ("OUROBOROS_REVIEWER_SLOTS",)
    not_migrated = [r.getMessage() for r in caplog.records if "not migrated" in r.getMessage()]
    assert len(not_migrated) == 1


def test_the_supervisor_boot_calls_the_notices_after_the_queue_restore():
    """The wiring pin: both notices run in ``server._run_supervisor`` once the message bus
    and the state file are initialised, next to the other boot-time owner notices; the
    review-pool report follows the retired-keys notice."""
    source = (pathlib.Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8")
    body = source.split("def _run_supervisor(settings: dict) -> None:", 1)[1].split("\ndef ", 1)[0]
    assert "_startup_retired_settings_notice(settings)" in body
    assert "_startup_review_pool_notice(settings)" in body
    assert body.index("restore_pending_from_snapshot(") < body.index("_startup_retired_settings_notice(settings)")
    assert body.index("_startup_retired_settings_notice(settings)") < body.index("_startup_review_pool_notice(settings)")
