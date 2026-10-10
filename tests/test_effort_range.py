"""The owner's effort range: the tolerant read, the one effort decision, the model-named
level detector, the retired role keys, and the reviewer rule that rides them.

Every rule is pinned in both directions: a request inside and outside the range, a pin
with and without a request, binding and Cyber Pro, a name beside each of them."""

from __future__ import annotations

import itertools
import json

import pytest

from ouroboros import settings_scales as scales
from ouroboros.settings_scales import (
    EFFORT_SCALE, OWNER_EFFORT_TIERS, choose_effort, clamp_effort_into, effort_fact,
    effort_fact_phrase, effort_range,
)


def _env(monkeypatch, **values):
    for key in ("OUROBOROS_EFFORT_MIN", "OUROBOROS_EFFORT_TASK", "OUROBOROS_EFFORT_MAX"):
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


# --- the tolerant read ----------------------------------------------------------------


def test_fresh_install_reads_low_medium_high(monkeypatch):
    _env(monkeypatch)
    assert effort_range() == {"min": "low", "recommended": "medium", "max": "high"}
    assert OWNER_EFFORT_TIERS == ("none", "low", "medium", "high", "xhigh", "max", "ultra")


def test_a_document_with_only_task_widens_the_bounds_around_it(monkeypatch):
    """The owner's own install: TASK=high, no MIN/MAX -> low / high / high."""
    _env(monkeypatch, OUROBOROS_EFFORT_TASK="high")
    assert effort_range() == {"min": "low", "recommended": "high", "max": "high"}
    _env(monkeypatch, OUROBOROS_EFFORT_TASK="ultra")
    assert effort_range() == {"min": "low", "recommended": "ultra", "max": "ultra"}
    _env(monkeypatch, OUROBOROS_EFFORT_TASK="none")
    assert effort_range() == {"min": "none", "recommended": "none", "max": "high"}


def test_unknown_values_take_their_key_default_and_minimal_round_trips(monkeypatch):
    _env(monkeypatch, OUROBOROS_EFFORT_MIN="bogus", OUROBOROS_EFFORT_TASK="", OUROBOROS_EFFORT_MAX="HIGH")
    assert effort_range() == {"min": "low", "recommended": "medium", "max": "high"}
    _env(monkeypatch, OUROBOROS_EFFORT_MIN="minimal", OUROBOROS_EFFORT_TASK="minimal", OUROBOROS_EFFORT_MAX="none")
    assert effort_range() == {"min": "minimal", "recommended": "minimal", "max": "minimal"}
    # A document read bypasses the environment; a live read opens the owner's current document.
    assert effort_range({"OUROBOROS_EFFORT_MAX": "ultra"}) == {"min": "low", "recommended": "medium", "max": "ultra"}


def test_a_live_read_sees_the_current_document_while_the_task_scope_keeps_its_own(monkeypatch, tmp_path):
    from ouroboros import config
    from ouroboros.settings_integrity import task_settings_scope, task_settings_snapshot

    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"OUROBOROS_EFFORT_TASK": "medium", "OUROBOROS_EFFORT_MAX": "ultra"}), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_PATH", path, raising=True)
    _env(monkeypatch, OUROBOROS_EFFORT_TASK="low", OUROBOROS_EFFORT_MAX="low")
    old = task_settings_snapshot({"OUROBOROS_EFFORT_TASK": "low", "OUROBOROS_EFFORT_MAX": "low"},
                                 {"OUROBOROS_EFFORT_TASK": "low", "OUROBOROS_EFFORT_MAX": "low"})
    from ouroboros.settings_integrity import live_effort_range

    with task_settings_scope(old):
        assert effort_range()["max"] == "low"  # the running task keeps the range it started with
        assert live_effort_range()["max"] == "ultra"  # a participant starting now reads the owner's document


def test_clamp_moves_to_the_nearest_bound_only(monkeypatch):
    rng = {"min": "low", "recommended": "medium", "max": "high"}
    assert clamp_effort_into("none", rng) == "low"
    assert clamp_effort_into("ultra", rng) == "high"
    assert clamp_effort_into("medium", rng) == "medium"
    assert clamp_effort_into("", rng) == "" and clamp_effort_into("bogus", rng) == "bogus"


# --- the one decision ------------------------------------------------------------------


def _triples():
    """Every ordered min <= recommended <= max over the seven owner tiers."""
    for low, rec, high in itertools.product(OWNER_EFFORT_TIERS, repeat=3):
        if scales.effort_rank(low) <= scales.effort_rank(rec) <= scales.effort_rank(high):
            yield {"min": low, "recommended": rec, "max": high}


@pytest.mark.parametrize("binds", [True, False])
def test_choose_effort_over_every_ordered_triple(binds):
    for rng in _triples():
        lo, rec, hi = scales.effort_rank(rng["min"]), scales.effort_rank(rng["recommended"]), scales.effort_rank(rng["max"])
        # Silence: the role default — recommended, or the top for default_top roles — in every mode.
        assert choose_effort("", binds=binds, rng=rng) == (rng["recommended"], "auto")
        assert choose_effort("", default_top=True, binds=binds, rng=rng) == (rng["max"], "auto")
        for tier in EFFORT_SCALE:
            rank = scales.effort_rank(tier)
            level, source = choose_effort(tier, binds=binds, rng=rng)
            if binds:  # a request clamps to the nearest bound, never refused, source auto
                assert source == "auto" and scales.effort_rank(level) == min(max(rank, lo), hi)
            else:  # Cyber Pro: the request applies as asked
                assert (level, source) == (tier, "cyber")
            # A pin: wins while the range binds; in Cyber Pro the request beats it, silence sits on it.
            assert choose_effort("", pin=tier, binds=binds, rng=rng) == (tier, "pin")
            assert choose_effort("ultra", pin=tier, binds=binds, rng=rng) == ((tier, "pin") if binds else ("ultra", "cyber"))
            # A level in the model name wins over everything, in every mode, even outside the range.
            assert choose_effort("none", pin="max", model_named=tier, binds=binds, rng=rng) == (tier, "model_name")
        assert lo <= rec <= hi


def test_unknown_tiers_read_as_absent_and_the_fact_records_the_request(monkeypatch):
    rng = {"min": "low", "recommended": "medium", "max": "high"}
    assert choose_effort("bogus", binds=True, rng=rng) == ("medium", "auto")
    assert choose_effort("", pin="BOGUS", binds=True, rng=rng) == ("medium", "auto")
    assert choose_effort(" XHIGH ", binds=True, rng=rng) == ("high", "auto")
    assert effort_fact(" Ultra", "high", "auto") == {"requested": "ultra", "applied": "high", "source": "auto"}
    assert effort_fact("", "medium", "auto") == {"requested": "", "applied": "medium", "source": "auto"}


def test_the_decision_binds_by_the_runtime_mode(monkeypatch):
    from ouroboros import config
    from ouroboros.runtime_mode_policy import effort_range_binds

    _env(monkeypatch, OUROBOROS_EFFORT_MAX="high")
    monkeypatch.setattr(config, "_BOOT_RUNTIME_MODE", "pro")
    assert effort_range_binds() is True
    assert choose_effort("ultra") == ("high", "auto")
    monkeypatch.setattr(config, "_BOOT_RUNTIME_MODE", "cyber_pro")
    assert effort_range_binds() is False
    assert choose_effort("ultra") == ("ultra", "cyber")
    # A consciousness-origin task capped to light binds even on a Cyber Pro install.
    assert effort_range_binds({"initiator": "consciousness", "runtime_mode_cap": "light"}) is True


def test_the_phrase_speaks_only_when_a_decision_is_worth_saying():
    rng = {"min": "low", "recommended": "medium", "max": "high"}
    assert effort_fact_phrase(effort_fact("", "medium", "auto"), rng) == ""
    assert effort_fact_phrase(effort_fact("high", "high", "auto"), rng) == ""
    assert effort_fact_phrase(effort_fact("ultra", "high", "auto"), rng) == (
        "effort high: your request ultra moved into my human's range low..high")
    assert effort_fact_phrase(effort_fact("none", "low", "auto"), rng) == (
        "effort low: your request none moved into my human's range low..high")
    assert effort_fact_phrase(effort_fact("low", "xhigh", "pin"), rng) == (
        "effort xhigh: pinned by my human; requested low not applied")
    assert effort_fact_phrase(effort_fact("", "xhigh", "pin"), rng) == "effort xhigh: pinned by my human"
    assert effort_fact_phrase(effort_fact("low", "max", "model_name"), rng) == (
        "effort max: the level in the model name; requested low not applied")
    assert effort_fact_phrase(effort_fact("ultra", "ultra", "cyber"), rng) == (
        "effort ultra: your request, unclamped under Cyber Pro")


def test_resolve_effort_keeps_its_signature_over_the_range(monkeypatch):
    _env(monkeypatch, OUROBOROS_EFFORT_TASK="medium", OUROBOROS_EFFORT_MAX="ultra")
    assert scales.resolve_effort("task") == scales.resolve_effort("presence") == scales.resolve_effort("") == "medium"
    assert scales.resolve_effort("evolution") == scales.resolve_effort("consciousness") == "ultra"


# --- the model-named level detector -----------------------------------------------------


@pytest.mark.parametrize("target, expected", [
    ("cursor=grok-4.7-xhigh-fast", "xhigh"), ("cursor=cursor-grok-4.6-high", "high"), ("agy=gemini-3.1-pro-max-fast", "max"),
    ("agy=gemini-3.8-flash-high", "high"), ("codex=gpt-5.6-sol", ""), ("claude=claude-opus-5", ""),
    ("cursor=composer-2.5", ""), ("cursor=auto", ""), ("codex=gpt-5.6-sol-high", ""), ("cursor", ""),
])
def test_session_targets_name_their_level_only_on_slug_harnesses(target, expected):
    from ouroboros.route_spec import ROUTE_KIND_AGENT_SESSION, RouteSpec, compound_session_effort, model_named_effort

    route = RouteSpec(ROUTE_KIND_AGENT_SESSION, target)
    assert compound_session_effort(route) == model_named_effort(route) == expected


@pytest.mark.parametrize("model, expected", [
    ("claudexor::cursor=grok-4.7-xhigh-fast", "xhigh"), ("claudexor::agy=gemini-3.1-pro-low", "low"),
    ("claudexor::codex=gpt-5.6-sol-high", ""), ("claudexor::claude=claude-opus-5", ""), ("claudexor::cursor", ""),
    ("openai/gpt-5.5-high", ""), ("openai::gpt-5.5", ""), ("cursor-grok-4.6-high", ""), ("", ""),
])
def test_api_wrapped_claudexor_models_name_their_level_the_same_way(model, expected):
    from ouroboros.route_spec import ROUTE_KIND_API_MODEL, RouteSpec, api_model_named_effort, model_named_effort

    assert api_model_named_effort(model) == expected
    if model:
        assert model_named_effort(RouteSpec(ROUTE_KIND_API_MODEL, model)) == expected


def test_a_stored_api_row_beside_a_named_model_still_loads_and_the_name_wins_at_execution(monkeypatch):
    """The shared parser keeps the session-only conflict rule: a stored API row pinned beside a
    named model never turns the catalog SOURCE_INVALID; the name decides at execution."""
    from ouroboros.configured_subagents import parse_configured_subagents
    from ouroboros.route_spec import ROUTE_KIND_API_MODEL, RouteSpec, validate_compound_session_effort

    rows = parse_configured_subagents(json.dumps({"enabled": True, "items": [
        {"subagent_id": "named", "recommended_use": "x", "effort": "low",
         "route": {"kind": "api_model", "target_id": "claudexor::cursor=grok-4.7-xhigh-fast"}},
    ]}))
    assert rows.items[0].effort == "low"
    validate_compound_session_effort(RouteSpec(ROUTE_KIND_API_MODEL, "claudexor::cursor=grok-4.7-xhigh-fast"), "low",
                                     setting="s", where="w")  # never an error for an API row
    with pytest.raises(ValueError, match="conflicts with compound route effort"):
        validate_compound_session_effort(RouteSpec("agent_session", "cursor=grok-4.7-xhigh-fast"), "low",
                                         setting="s", where="w")
    assert choose_effort("medium", pin="low", model_named="xhigh", binds=True) == ("xhigh", "model_name")


def test_the_claudexor_model_transport_never_sends_a_contradicting_effort():
    from ouroboros.llm_claudexor import _request

    named = {"source": "cursor", "resolved_model": "grok-4.7-xhigh-fast", "processing_preferences": []}
    body = _request(named, [{"role": "user", "content": "hi"}], None, {"reasoning_effort": "low"})
    assert body["options"]["reasoningEffort"] == "xhigh" and named["requested_reasoning_effort"] == "low"
    plain = {"source": "codex", "resolved_model": "gpt-5.6-sol", "processing_preferences": []}
    body = _request(plain, [{"role": "user", "content": "hi"}], None, {"reasoning_effort": "low"})
    assert body["options"]["reasoningEffort"] == "low"


# --- the retired role keys ---------------------------------------------------------------


def test_the_role_keys_are_dropped_with_the_notice_naming_the_range_top(monkeypatch, tmp_path):
    from ouroboros import config
    from ouroboros.settings_defaults import retired_setting_keys_notice

    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"OUROBOROS_EFFORT_EVOLUTION": "xhigh", "OUROBOROS_EFFORT_CONSCIOUSNESS": "low",
                                "OUROBOROS_EFFORT_TASK": "high"}), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_PATH", path, raising=True)
    loaded = config.load_settings()
    assert "OUROBOROS_EFFORT_EVOLUTION" not in loaded and "OUROBOROS_EFFORT_CONSCIOUSNESS" not in loaded
    assert loaded["OUROBOROS_EFFORT_TASK"] == "high" and loaded["OUROBOROS_EFFORT_MAX"] == "high"
    notice = retired_setting_keys_notice(("OUROBOROS_EFFORT_EVOLUTION", "OUROBOROS_EFFORT_CONSCIOUSNESS"))
    assert "NOT honored" in notice and "OUROBOROS_EFFORT_EVOLUTION -> OUROBOROS_EFFORT_MAX" in notice


def test_the_rc_auditor_reports_a_stored_role_key_as_a_note_naming_the_range_top(tmp_path):
    import os
    import pathlib

    from tests.test_rc_audit_fixture_suite import _build_clean_70_install, _load_module, _run

    module = _load_module()
    checks = {c["key"]: c for c in module.build_scope()["checks"] if c["id"] == "retired-setting"}
    for key in ("OUROBOROS_EFFORT_EVOLUTION", "OUROBOROS_EFFORT_CONSCIOUSNESS"):
        assert "OUROBOROS_EFFORT_MAX" in checks[key]["migration"] and "effort range" in checks[key]["behavior"]
    data = _build_clean_70_install(tmp_path / "install")
    document = json.loads((data / "settings.json").read_text(encoding="utf-8"))
    document.update({"OUROBOROS_EFFORT_EVOLUTION": "xhigh", "OUROBOROS_EFFORT_CONSCIOUSNESS": "high"})
    (data / "settings.json").write_text(json.dumps(document, indent=2), encoding="utf-8")
    result = _run(data, "--json", str(tmp_path / "report.json"), isolated_root=tmp_path / "isol")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(pathlib.Path(tmp_path / "report.json").read_text(encoding="utf-8"))
    notes = [f for f in report["findings"] if f["check_id"] == "retired-setting"]
    assert {f["subject"] for f in notes} == {"settings.json:OUROBOROS_EFFORT_EVOLUTION",
                                            "settings.json:OUROBOROS_EFFORT_CONSCIOUSNESS"}
    assert all(f["severity"] == "note" and "OUROBOROS_EFFORT_MAX" in f["detail"] for f in notes)
    assert report["summary"]["incompatible"] == 0
    assert os.environ.get("OUROBOROS_EFFORT_EVOLUTION") is None


# --- reviewers ---------------------------------------------------------------------------


def _reviewer(target="openai/gpt-5.6-terra", *, effort="", kind="api_chat", session_target=""):
    from ouroboros.reviewer_slot_config import ConfiguredReviewerSlot

    return ConfiguredReviewerSlot(slot_id="r", kind=kind, target_id=target, effort=effort, session_target=session_target)


def test_reviewer_rule_name_then_pin_then_order_clamped_then_the_range_top(monkeypatch):
    from ouroboros import config
    from ouroboros.reviewer_slot_config import row_at_effort_order, row_effort, row_effort_source

    _env(monkeypatch, OUROBOROS_EFFORT_MIN="low", OUROBOROS_EFFORT_TASK="medium", OUROBOROS_EFFORT_MAX="high")
    monkeypatch.setattr(config, "_BOOT_RUNTIME_MODE", "pro")
    auto, pinned = _reviewer(), _reviewer(effort="xhigh")
    named = _reviewer("cursor=grok-4.7-max-fast", kind="agent_session", session_target="cursor=grok-4.7-max-fast")
    named_api = _reviewer("claudexor::agy=gemini-3.1-pro-low")
    assert row_effort(auto) == "high" and row_effort_source(auto) == "auto"
    assert row_effort(auto, default="ultra") == "high" and row_effort(auto, default="none") == "low"
    assert row_effort(pinned) == "xhigh" and row_effort(pinned, default="low") == "xhigh"
    assert row_effort_source(pinned) == "pin"
    assert row_effort(named, default="low") == "max" and row_effort(named_api, default="ultra") == "low"
    assert row_effort_source(named) == row_effort_source(named_api) == "model_name"
    assert row_at_effort_order(auto, "ultra").effort == "high"
    assert row_at_effort_order(pinned, "low") is None and row_at_effort_order(named, "low") is None
    # The deep review's Main row: Auto -> the range's top; a named Main -> its level.
    monkeypatch.setenv("OUROBOROS_MODEL", "openai/gpt-5.6-sol")
    from ouroboros.deep_self_review import main_review_row

    assert row_effort(main_review_row()) == "high"
    monkeypatch.setenv("OUROBOROS_EFFORT_MAX", "ultra")
    assert row_effort(main_review_row()) == "ultra"
    monkeypatch.setenv("OUROBOROS_MODEL", "claudexor::cursor=grok-4.7-xhigh-fast")
    assert row_effort(main_review_row()) == "xhigh"
    # Cyber Pro: the order beats the pin, unclamped, never the name.
    monkeypatch.setattr(config, "_BOOT_RUNTIME_MODE", "cyber_pro")
    assert row_effort(pinned, default="low") == "low" and row_effort(auto, default="ultra") == "ultra"
    assert row_effort(named, default="low") == "max" and row_at_effort_order(pinned, "low").effort == "low"
    assert row_effort(pinned) == "xhigh" and row_effort(auto) == "ultra"


def test_a_wave_order_discloses_the_effective_weaker_level_not_the_raw_order(monkeypatch):
    from types import SimpleNamespace

    from ouroboros.tools.review_change import _effort_facts

    seats = [(SimpleNamespace(slot_id="auto", effort="low", declared_effort="none"), (), False),
             (SimpleNamespace(slot_id="pinned", effort="xhigh", declared_effort=""), (), False)]
    facts = _effort_facts("none", seats, {"auto": "high", "pinned": "xhigh"})
    assert facts == {"order": "none", "applied": ["auto"], "not_applied": ["pinned"], "weaker_than_configured": ["auto"]}
    seats = [(SimpleNamespace(slot_id="auto", effort="high", declared_effort="ultra"), (), False)]
    assert _effort_facts("ultra", seats, {"auto": "high"})["weaker_than_configured"] == []
