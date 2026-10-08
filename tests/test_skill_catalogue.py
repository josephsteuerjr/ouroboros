"""Registered catalogue -> ordinary result cap -> captured SDK request, no network."""
import copy
import json

import httpx
import openai
import pytest

from ouroboros import skill_catalogue
from ouroboros.llm import LLMClient
from ouroboros.loop_tool_execution import process_tool_results
from ouroboros.skill_loader import summarize_skills
from ouroboros.tool_capabilities import tool_result_limit
from ouroboros.tools.registry import ToolContext, ToolRegistry


@pytest.fixture
def registry(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    repo.mkdir()
    data.mkdir()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(data))
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", "")
    monkeypatch.setattr("ouroboros.safety.check_safety", lambda *_a, **_kw: (True, ""))
    reg = ToolRegistry(repo_dir=repo, drive_root=data)
    reg._ctx.task_id = "catalogue-consumer"
    return reg


def _seed(registry, name, *, description=None, body="Instructions: select this skill."):
    folder = registry._ctx.drive_root / "skills" / "external" / name
    folder.mkdir(parents=True)
    header = {"name": name, "type": "instruction", "version": "1.0.0",
              "description": description or ("Purpose " + "雪\\\"\n" * 120),
              "model_experience": "Full model experience", "when_to_use": "When requested"}
    # JSON manifest carries no body, so use real YAML frontmatter for instructions.
    import yaml
    (folder / "SKILL.md").write_text("---\n" + yaml.safe_dump(header) + "---\n" + body,
                                   encoding="utf-8")
    return folder


def _consumer(registry, args):
    typed = registry.execute_result("list_skills", args)
    text = typed.text
    messages = []
    trace = {"tool_calls": []}
    process_tool_results([{"fn_name": "list_skills", "tool_call_id": "catalogue-call",
        "result": text, "tool_result": typed, "is_error": typed.status == "error",
        "tool_args": args, "args_for_log": args,
        "result_meta": {"status": typed.status}}], messages, trace,
        emit_progress=lambda _message, **_kw: None, tools=registry)
    assert len(messages[0]["content"]) < tool_result_limit("list_skills")
    assert "FULL_RESULT_SOURCE_UNAVAILABLE" not in messages[0]["content"]
    assert trace["tool_calls"][0].get("result_partial") is not True
    # The same remote builder + SDK serializer used by Main, captured at HTTP
    # dispatch, rather than claiming a pure helper result is a physical request.
    original = [{"role": "user", "content": "Choose a skill"},
        {"role": "assistant", "tool_calls": [{"id": "catalogue-call", "type": "function",
         "function": {"name": "list_skills", "arguments": json.dumps(args)}}]}, *messages]
    client = LLMClient(api_key="fixture")
    target = {"provider": "openai-compatible", "resolved_model": "fixture",
              "usage_model": "fixture", "supports_openrouter_extensions": False}
    payload = client._build_remote_kwargs(target, original, "none", 128, "auto", None, None,
                                         skip_capability_fetch=True)
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "fixture", "object": "chat.completion",
            "created": 0, "model": "fixture", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "done"}}]})

    with openai.OpenAI(api_key="fixture", base_url="https://fixture.invalid/v1", max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond))) as sdk:
        sdk.chat.completions.create(**payload)
    wire_text = captured[0]["messages"][-1]["content"]
    assert wire_text == messages[0]["content"] == text
    return json.loads(wire_text)


def test_registered_pages_reach_all_names_and_tail_detail_in_actual_request(registry):
    for i in range(36):
        _seed(registry, f"skill-{i:02}")
    args = {}
    seen = []
    for _ in range(36):
        page = _consumer(registry, args)
        assert page["count"] == page["total"] == 36
        assert page["returned"] == len(page["skills"]) > 0
        seen.extend(r["name"] for r in page["skills"])
        assert all("manifest" not in r and "tool_surfaces" not in r for r in page["skills"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > page["offset"]
        args = {"offset": page["next_offset"], "inventory": page["inventory"]}
    assert seen == [f"skill-{i:02}" for i in range(36)]
    detail = _consumer(registry, {"name": "skill-35", "detail": True})
    row = detail["skills"][0]
    assert row["manifest"]["body"] == "Instructions: select this skill."
    assert row["model_experience"]["what_model_sees"] == "Full model experience"
    assert row["enabled"] is False and row["review_status"] == "pending"
    assert row["available_for_execution"] is False
    assert (registry._ctx.drive_root / "state" / "skills").exists() is False


def test_oversized_detail_source_is_readable_by_same_actor(registry):
    _seed(registry, "tail", body="x" * 24000 + "\nEXACT_INSTRUCTIONS_END")
    page = _consumer(registry, {"name": "tail", "detail": True})
    row = page["skills"][0]
    assert row["oversized"] and row["source_status"] == "available"
    ref = row["source_ref"]
    read = registry.execute(ref["read"]["tool"], {**ref["read"]["arguments"], "start_char": 22000})
    assert "EXACT_INSTRUCTIONS_END" in read
    from ouroboros.artifacts import read_actor_source_bytes
    exact = json.loads(read_actor_source_bytes(registry._ctx.drive_root,
                                               "catalogue-consumer", ref))
    assert exact["manifest"]["body"].endswith("EXACT_INSTRUCTIONS_END")


def test_source_failure_stays_valid_gap_and_advances(registry, monkeypatch):
    _seed(registry, "tail", body="x" * 30000)
    def unavailable(*_a, **_kw):
        raise OSError("fixture storage unavailable")
    monkeypatch.setattr(skill_catalogue, "store_actor_source_bytes", unavailable)
    page = _consumer(registry, {"name": "tail", "detail": True})
    row = page["skills"][0]
    assert row["source_status"] == "unavailable" and "source_ref" not in row
    assert page["returned"] == 1 and page["next_offset"] is None


def test_empty_missing_and_invalid_public_arguments(registry):
    assert _consumer(registry, {})["skills"] == []
    assert _consumer(registry, {"name": "missing"})["error"] == "skill_not_found"
    for args in ({"detail": True}, {"offset": -1}, {"limit": 0}, {"limit": 101}, {"other": 1}):
        assert "TOOL_ARG_ERROR" in registry.execute("list_skills", args)


def test_membership_drift_and_mutable_state_have_distinct_identities(registry):
    _seed(registry, "a")
    first = _consumer(registry, {})
    _seed(registry, "b")
    changed = _consumer(registry, {"offset": 1, "inventory": first["inventory"]})
    assert changed["error"] == "inventory_changed" and changed["restart_offset"] == 0
    assert not changed["skills"]
    from ouroboros.skill_loader import save_enabled
    save_enabled(registry._ctx.drive_root, "a", True)
    after = _consumer(registry, {"inventory": changed["inventory"]})
    assert "error" not in after and after["skills"][0]["enabled"] is True
    assert after["inventory"] == changed["inventory"]


def test_collision_detail_does_not_choose_arbitrary_payload(registry):
    _seed(registry, "same", body="External body")
    root = registry._ctx.drive_root / "skills" / "clawhub" / "same"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: same\ntype: instruction\nversion: '1'\n---\nOther body")
    page = _consumer(registry, {"name": "same", "detail": True})
    assert all(r["detail_error"] == "identity_collision_or_unavailable" for r in page["skills"])
    assert all("manifest" not in r for r in page["skills"])


def test_huge_compact_identity_error_and_escaping_do_not_cut_records(tmp_path):
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path, task_id="huge-row")
    rows = [{"name": "N" * 20000, "type": "instruction", "load_error": "E" * 80000},
            {"name": "z", "type": "extension", "available_for_execution": False,
             "live_loaded": True, "desired_live": True, "process": "server",
             "description": "\u0000\\\"" * 2000,
             "readiness": {"blockers": ["B" * 10000] * 10}}]
    summary = {"count": 2, "skills": rows}
    before = copy.deepcopy(summary)
    page = json.loads(skill_catalogue.render_skill_catalogue(ctx, summary, tmp_path, limit=1))
    assert page["returned"] == 1 and page["next_offset"] == 1
    assert page["skills"][0]["source_status"] == "available"
    assert "name" not in page["skills"][0]  # identity in source, not truncated identity
    second = json.loads(skill_catalogue.render_skill_catalogue(ctx, summary, tmp_path, offset=1))
    assert second["skills"][0]["live_loaded"] is True
    assert second["skills"][0]["available_for_execution"] is False
    assert second["skills"][0]["blockers_omitted"] == 7
    assert second["skills"][0]["omitted_chars"]["description"] > 0
    assert summary == before


def test_shared_summary_still_contains_full_diagnostics(registry):
    _seed(registry, "demo")
    summary = summarize_skills(registry._ctx.drive_root)
    assert summary["skills"][0]["model_experience"] is not None
    assert "review_gate" in summary["skills"][0] and "tool_surfaces" in summary["skills"][0]
    compact = _consumer(registry, {})
    assert "review_gate" not in compact["skills"][0]


def test_refusals_are_failures_through_typed_registry(registry):
    _seed(registry, "a")
    first = _consumer(registry, {})
    _seed(registry, "b")
    for args in ({"name": "missing"}, {"offset": 1, "inventory": first["inventory"]}):
        result = registry.execute_result("list_skills", args)
        assert result.status != "ok"
        assert json.loads(result.text)["ok"] is False


@pytest.mark.serial
def test_oversized_tool_metadata_retains_full_record(registry, monkeypatch):
    from ouroboros import extension_loader
    _seed(registry, "demo", body="Small body")
    # A short instruction body must not bypass bounds on another detail field.
    monkeypatch.setitem(extension_loader._tools, "ext_catalogue_fixture", {
        "skill": "demo", "name": "ext_catalogue_fixture", "description": "d" * 24000,
        "schema": {"type": "object", "properties": {"x": {"type": "string"}}},
    })
    # Metadata projection itself is independent of live readiness and enablement.
    page = _consumer(registry, {"name": "demo", "detail": True})
    ref = page["skills"][0]["source_ref"]
    read = registry.execute(ref["read"]["tool"], {**ref["read"]["arguments"], "start_char": 20000})
    assert "tool_schemas" in read or '"description"' in read
    from ouroboros.artifacts import read_actor_source_bytes
    exact = json.loads(read_actor_source_bytes(registry._ctx.drive_root, "catalogue-consumer", ref))
    assert exact["manifest"]["body"] == "Small body"
    assert exact["tool_schemas"][0]["description"] == "d" * 24000


def test_inventory_detects_delete_and_payload_edit(registry):
    a = _seed(registry, "a")
    b = _seed(registry, "b")
    first = _consumer(registry, {})
    (b / "SKILL.md").unlink()
    deleted = _consumer(registry, {"inventory": first["inventory"], "offset": 1})
    assert deleted["error"] == "inventory_changed" and deleted["returned"] == 0
    old = _consumer(registry, {})
    with (a / "SKILL.md").open("a") as stream:
        stream.write("\nNew instructions")
    edited = _consumer(registry, {"inventory": old["inventory"]})
    assert edited["error"] == "inventory_changed" and edited["returned"] == 0


@pytest.mark.parametrize("padding", ["", "x" * 24000])
def test_yaml_extra_values_do_not_break_named_detail(registry, padding):
    import datetime
    import yaml
    body = padding + "Preserve these instructions exactly."
    folder = _seed(registry, "yaml-extra", body=body)
    manifest = folder / "SKILL.md"
    text = manifest.read_text()
    manifest.write_text(text.replace("\n---\n", "\npublished: 2026-10-08\nlabels: !!set {alpha: null, beta: null}\nindexes: {1: alpha, text: beta}\nnumeric: {1: alpha, 2: beta}\nfloats: [.nan, .inf]\nshared: &self {back: *self}\n---\n", 1))
    row = _consumer(registry, {"name": "yaml-extra", "detail": True})["skills"][0]
    if padding:
        ref = row["source_ref"]
        read = registry.execute(ref["read"]["tool"], {**ref["read"]["arguments"], "start_char": 20000})
        assert "Preserve these instructions exactly." in read
        from ouroboros.artifacts import read_actor_source_bytes
        row = json.loads(read_actor_source_bytes(registry._ctx.drive_root, "catalogue-consumer", ref))
    detail = row["manifest"]
    assert detail["body"] == body
    assert detail["representation"] == "yaml"
    complete = yaml.safe_load(detail["yaml"])
    assert complete["raw_extra"]["published"] == datetime.date(2026, 10, 8)
    assert complete["raw_extra"]["labels"] == {"alpha", "beta"}
    assert complete["raw_extra"]["indexes"] == {1: "alpha", "text": "beta"}
    assert complete["raw_extra"]["numeric"] == {1: "alpha", 2: "beta"}
    import math
    assert math.isnan(complete["raw_extra"]["floats"][0])
    assert complete["raw_extra"]["floats"][1] == math.inf
    shared = complete["raw_extra"]["shared"]
    assert shared["back"] is shared


def test_oversized_source_survives_forked_execution_store_retirement(registry, tmp_path):
    _seed(registry, "tail", body="x" * 24000 + "\nCANONICAL_INSTRUCTIONS_END")
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (registry._ctx.drive_root / "skills").rename(canonical / "skills")
    registry._ctx.task_metadata = {"budget_drive_root": str(canonical)}
    page = _consumer(registry, {"name": "tail", "detail": True})
    ref = page["skills"][0]["source_ref"]
    # The current fork can read its execution mirror before retirement too.
    mirror = registry.execute(ref["read"]["tool"],
        {**ref["read"]["arguments"], "start_char": 22000})
    assert "CANONICAL_INSTRUCTIONS_END" in mirror
    # The JSON ref is producer data, not a trusted observability closure carrier.
    # Retire the execution store without pretending generic copyback adopts it.
    artifact_dir = registry._ctx.drive_root / "task_results" / "artifacts" / "catalogue-consumer"
    artifact_dir.rename(tmp_path / "retired-execution-artifacts")
    canonical_actor = ToolRegistry(repo_dir=registry._ctx.repo_dir, drive_root=canonical)
    canonical_actor._ctx.task_id = "catalogue-consumer"
    read = canonical_actor.execute(ref["read"]["tool"],
        {**ref["read"]["arguments"], "start_char": 22000})
    assert "CANONICAL_INSTRUCTIONS_END" in read
    from ouroboros.artifacts import read_actor_source_bytes
    exact = json.loads(read_actor_source_bytes(canonical, "catalogue-consumer", ref))
    assert exact["manifest"]["body"].endswith("CANONICAL_INSTRUCTIONS_END")


def test_named_detail_uses_the_same_manifest_inventory_as_index(registry):
    _seed(registry, "unique", body="Real instructions")
    ghost = registry._ctx.drive_root / "skills" / "clawhub" / "unique"
    ghost.mkdir(parents=True)  # not a manifest-bearing skill in passive discovery
    page = _consumer(registry, {"name": "unique", "detail": True})
    assert page["total"] == 1
    assert page["skills"][0]["manifest"]["body"] == "Real instructions"


@pytest.mark.parametrize("bad_bytes", [b"\xff", b"---\nname: [unterminated\n---\n"])
def test_broken_manifest_detail_never_presents_placeholder_as_instructions(registry, bad_bytes):
    folder = _seed(registry, "broken")
    (folder / "SKILL.md").write_bytes(bad_bytes)
    page = _consumer(registry, {"name": "broken", "detail": True})
    assert page["ok"] is False
    row = page["skills"][0]
    assert row["detail_error"] == "manifest_unreadable"
    assert "manifest" not in row and row["load_error"]
