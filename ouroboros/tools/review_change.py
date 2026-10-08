"""``review_change`` — one review-only wave over a change in any registered root.

``commit_reviewed`` is the landing in my own body: it reviews the staged system repository and commits.
``review_change`` is the same review as a capability for any subject I choose — a project copy's
``base..head``, a repository's index or worktree, or the system repository itself. It never commits, never
starts on its own and never runs tests. The rules follow the subject: a subject that is my body is read
against the body layer, anything else against the universal core. Enforcement is recorded for every root
and binds only a body subject's record; a verdict on another root locks nothing.

Panel (owner, 07.10): for a body subject outside Cyber Pro the configured panel is the owner's, so
``reviewers`` only ADD seats and a named subset is recorded as ignored. For a body subject in Cyber Pro and
for ANY subject outside the body, ``reviewers`` IS the panel (omitted = every configured seat); a narrowed
panel without a ``reason`` is recorded loudly (``reason_missing``), never refused. ``coupling_only`` seats
answer only the coupling question; like every added seat they stay outside the quorum and their findings
are ``additional_findings``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import pathlib
import re
import subprocess
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from ouroboros.config import (
    adaptive_quorum, get_finalization_grace_sec, get_llm_transport_read_timeout_sec, get_review_enforcement,
    get_runtime_mode, get_task_abs_ceiling_sec, operation_window_sec,
)
from ouroboros.review_body_fact import body_fact, layer_for
from ouroboros.review_ledger import (
    PART_CHANGE, PART_COUPLING, PARTS, QUESTION_NOT_PERFORMED, build_wave_record, ledger_root, new_record_id,
    panel_facts, reduce_verdict, revise_record, row_verdict, seat_parts, write_record,
)
from ouroboros.runtime_mode_policy import runtime_mode_at_least
from ouroboros.settings_scales import EFFORT_SCALE, effort_rank
from ouroboros.tools.arg_feedback import argument_refusal
from ouroboros.tools.parallel_review import run_parallel_review
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.review_change_custody import (
    arm_rejoin, install_paid_stamp, pending_round_attempt, settle_attempt,
)
from ouroboros.tools.review_helpers import checklist_fingerprint
from ouroboros.tools.review_subject import (
    ReviewSubjectSpec, checkout_token, freeze_subject, is_gate_subject, isolated_checkout, reuse_or_none,
    review_retry_key, review_reuse_key, review_round_sha,
)
from ouroboros.utils import run_cmd

log = logging.getLogger(__name__)

TOOL_NAME = "review_change"
SURFACE = "change"
ROOTS = ("active_workspace", "system_repo")
SUBJECT_KINDS = ("index", "worktree", "base..head")
# Wave block reasons the seat rows state themselves (findings, quorum, coupling);
# any other block keeps a verdict recomputed over the assigned seats from PASS.
_ROW_STATED_BLOCKS = frozenset({"", "critical_findings", "review_quorum", "coupling_not_performed", "change_unanswered"})
_GIT_PROBE_TIMEOUT_SEC = 30
_SEAT_PREFIX = "rc-"
_SEAT_ID_MAX = 64


class ReviewChangeArgumentError(ValueError):
    """A deterministic call error, refused before any reviewer is paid."""


# --- Arguments ------------------------------------------------------------------

@dataclass(frozen=True)
class ReviewChangeRequest:
    root: str = "active_workspace"
    workspace_root: str = ""
    subject: str = ""
    base: str = ""
    head: str = ""
    goal: str = ""
    scope: str = ""
    author_questions: Tuple[str, ...] = ()
    reviewers: Tuple[str, ...] = ()
    reason: str = ""
    coupling_only: Tuple[str, ...] = ()
    reviewer_effort: str = ""
    review_rebuttal: str = ""
    treat_as_body: bool = False


_ARG_NAMES = frozenset({*ReviewChangeRequest.__dataclass_fields__, "surface"})
_TEXT_ARGS = ("root", "workspace_root", "subject", "base", "head", "surface", "goal", "scope",
              "reason", "reviewer_effort", "review_rebuttal")


def _text(args: Dict[str, Any], name: str, problems: List[str]) -> str:
    value = args.get(name)
    if value is not None and not isinstance(value, str):
        problems.append(f"{name} must be a string")
    return value if isinstance(value, str) else ""


def _strings(args: Dict[str, Any], name: str, problems: List[str], *, verbatim: bool) -> Tuple[str, ...]:
    value = args.get(name) or []
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        problems.append(f"{name} must be a list of strings")
        return ()
    kept = [item if verbatim else item.strip() for item in value if item.strip()]
    return tuple(kept) if verbatim else tuple(dict.fromkeys(kept))


def parse_request(args: Dict[str, Any]) -> ReviewChangeRequest:
    """The call's arguments as one request; every violated rule is named at once."""
    problems: List[str] = []
    unknown = sorted(set(args) - _ARG_NAMES)
    if unknown:
        problems.append(f"unknown argument(s) {', '.join(unknown)}")
    text = {name: _text(args, name, problems) for name in _TEXT_ARGS}
    root = text["root"].strip() or "active_workspace"
    subject, base, head = text["subject"].strip(), text["base"].strip(), text["head"].strip()
    if root not in ROOTS:
        problems.append(f"root must be one of {', '.join(ROOTS)} (a registered folder goes in workspace_root), got {root!r}")
    if root == "system_repo" and text["workspace_root"].strip():
        problems.append("root=system_repo names the system repository; leave workspace_root empty")
    if subject not in SUBJECT_KINDS:
        problems.append(f"subject must be one of {', '.join(SUBJECT_KINDS)}, got {subject!r}")
    elif subject == "base..head" and not (base and head):
        problems.append("subject=base..head needs both base and head revisions")
    elif subject != "base..head" and head:
        problems.append(f"head belongs to subject=base..head; subject={subject} is reviewed against base (default HEAD)")
    for name, rev in (("base", base), ("head", head)):
        if rev.startswith("-"):
            problems.append(f"{name} must name a revision, not an option ({rev!r})")
    surface = text["surface"].strip() or SURFACE
    if surface != SURFACE:
        problems.append(f"surface must be {SURFACE!r}, got {surface!r}")
    effort = text["reviewer_effort"].strip()
    if effort and effort not in EFFORT_SCALE:
        problems.append(f"reviewer_effort must be one of {', '.join(EFFORT_SCALE)}, got {effort!r}")
    treat = args.get("treat_as_body", False)
    if not isinstance(treat, bool):
        problems.append("treat_as_body must be a boolean")
    lists = {name: _strings(args, name, problems, verbatim=name == "author_questions")
             for name in ("author_questions", "reviewers", "coupling_only")}
    if problems:
        raise ReviewChangeArgumentError("; ".join(problems))
    return ReviewChangeRequest(
        root=root, workspace_root=text["workspace_root"].strip(), subject=subject, base=base, head=head,
        goal=text["goal"], scope=text["scope"], author_questions=lists["author_questions"],
        reviewers=lists["reviewers"], reason=text["reason"], coupling_only=lists["coupling_only"],
        reviewer_effort=effort, review_rebuttal=text["review_rebuttal"], treat_as_body=treat is True)


# --- Root -------------------------------------------------------------------------

def _git_line(cwd: pathlib.Path, *args: str) -> str:
    try:
        return run_cmd(["git", *args], cwd=cwd, timeout=_GIT_PROBE_TIMEOUT_SEC).strip()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return ""


def _system_repo(ctx: ToolContext) -> pathlib.Path:
    """The context's body root: the system repository, or the bound body candidate
    (``body_candidate.bind``) that this task authors — what ``root=system_repo`` names."""
    from ouroboros.tools.tool_resolution import system_repo_dir_for

    return pathlib.Path(system_repo_dir_for(ctx)).resolve()


def _governance_repo(ctx: ToolContext) -> pathlib.Path:
    """The INSTALLED body whose rules execute — governance for every subject and the
    body identity the predicate compares against. For a bound body candidate this is
    the SERVING checkout, never the candidate (``review_substrate.review_repo_dirs_for``'s
    rule: the candidate is the subject; the body that is running is the authority)."""
    from ouroboros.body_candidate import serving_repo_dir_for

    return pathlib.Path(serving_repo_dir_for(ctx)).resolve()


def _admitted_workspace_root(ctx: ToolContext, value: str) -> pathlib.Path:
    """``schedule_subagent``'s ``workspace_root`` policy: an existing folder the
    caller can already read, relative paths against its available workspace."""
    from ouroboros.tool_access_reads import admit_child_start_folder, capture_parent_workspace

    try:
        path = pathlib.Path(value).expanduser()
        if not path.is_absolute():
            parent = capture_parent_workspace(ctx)
            if not parent.get("root") or parent.get("availability") == "unavailable":
                raise ValueError("a relative workspace_root needs an available workspace; name an absolute folder")
            path = pathlib.Path(parent["root"]) / path
        return pathlib.Path(admit_child_start_folder(ctx, path, {}))
    except (OSError, ValueError, RuntimeError) as exc:
        raise ReviewChangeArgumentError(f"workspace_root {value!r} is not a registered folder this task can read: {exc}") from exc


def resolve_review_root(ctx: ToolContext, binding: Any, request: ReviewChangeRequest) -> Tuple[str, pathlib.Path]:
    """(root_kind, top level) of the repository this call reviews."""
    from ouroboros.tools import git as git_mod

    if request.workspace_root:
        start = _admitted_workspace_root(ctx, request.workspace_root)
    else:
        try:
            start = pathlib.Path(git_mod._vcs_binding(ctx, binding, root=request.root).base_path)
        except (OSError, ValueError, RuntimeError) as exc:
            raise ReviewChangeArgumentError(f"root={request.root} does not resolve here: {exc}") from exc
    top = _git_line(start, "rev-parse", "--show-toplevel")
    if not top:
        raise ReviewChangeArgumentError(
            f"{start} is not inside a git repository; name a registered repository with workspace_root, or root=system_repo")
    root = pathlib.Path(top).resolve()
    return ("system_repo" if root == _system_repo(ctx) else "active_workspace"), root


def _verify_revisions(root: pathlib.Path, request: ReviewChangeRequest) -> None:
    for name in ("base", "head"):
        rev = getattr(request, name)
        if rev and not _git_line(root, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"):
            raise ReviewChangeArgumentError(f"{name} {rev!r} is not a commit in {root}")


# --- Panel ------------------------------------------------------------------------

@dataclass(frozen=True)
class ComposedPanel:
    triad: Tuple[Any, ...]
    scope: Tuple[Any, ...]
    additional: Tuple[str, ...]
    override: bool
    facts: Dict[str, Any]

    @property
    def assigned(self) -> Tuple[Tuple[str, str, str], ...]:
        """The composition identity: every seat, its part, and whether it counts."""
        extra = set(self.additional)
        seats = [(row.slot_id, part) for row in self.triad for part in seat_parts(row)]
        seats += [(row.slot_id, part) for row in self.scope for part in seat_parts(row, coupling_only=True)]
        return tuple(sorted((seat, part, "additional" if seat in extra else "assigned") for seat, part in seats))


def _configured_seats(config: Any) -> Dict[str, Tuple[str, Any]]:
    seats: Dict[str, Tuple[str, Any]] = {}
    for part, rows in ((PART_CHANGE, config.triad), (PART_COUPLING, config.scope)):
        for row in rows:
            for key in (row.slot_id, row.subagent_id):
                if key:
                    seats.setdefault(str(key), (part, row))
    return seats


def _mint_seat_id(name: str, taken: set) -> str:
    stem = _SEAT_PREFIX + (re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "seat")
    stem = stem[:_SEAT_ID_MAX - 4]
    seat, index = stem, 2
    while seat in taken:
        seat, index = f"{stem}-{index}", index + 1
    taken.add(seat)
    return seat


def _apply_effort(order: str, triad: List[Any], scope: List[Any]) -> Tuple[List[Any], List[Any], Dict[str, Any]]:
    """``reviewer_effort`` as this wave's ORDER (``plan_task``'s rule): it outranks
    each row's own effort; a compound route keeps the effort it encodes."""
    from ouroboros import reviewer_slot_config as slots

    applied, not_applied, weaker = [], [], []

    def ordered(row: Any, surface: str) -> Any:
        new = slots.row_at_effort_order(row, order)
        if new is None:
            not_applied.append(row.slot_id)
            return row
        applied.append(row.slot_id)
        if effort_rank(order) < effort_rank(slots.row_effort(row, surface)):
            weaker.append(row.slot_id)
        return new

    if order:
        triad = [ordered(row, "review") for row in triad]
        scope = [ordered(row, "scope_review") for row in scope]
    return triad, scope, {"order": order, "applied": applied, "not_applied": not_applied,
                          "weaker_than_configured": weaker}


def compose_panel(request: ReviewChangeRequest, *, adds_only: bool) -> ComposedPanel:
    """The wave's seats. ``adds_only`` (a body subject outside Cyber Pro): the
    configured panel plus any named roster subagent. Otherwise the named seats ARE
    the panel, every configured seat when none is named. A malformed configured
    panel yields no seats and its error; the wave then reports the typed block."""
    from ouroboros import reviewer_slot_config as slots

    try:
        config = slots.load_reviewer_slot_config()
    except ValueError as exc:
        return ComposedPanel((), (), (), False, {"composition": "full_pool", "composition_error": str(exc)})
    by_name = _configured_seats(config)
    configured = {row.slot_id for row in (*config.triad, *config.scope)}
    taken = set(configured)

    def seat(name: str) -> Tuple[str, Any]:
        if name in by_name:
            return by_name[name]
        try:
            return PART_CHANGE, slots.roster_review_row(_mint_seat_id(name, taken), name)
        except ValueError as exc:
            raise ReviewChangeArgumentError(
                f"reviewer {name!r} is neither a configured review seat nor an enabled subagent ({exc})") from exc

    triad, scope, additional = [], [], []
    named = [seat(name) for name in request.reviewers]
    if adds_only:
        triad, scope = list(config.triad), list(config.scope)
        named_configured = {row.slot_id for _part, row in named if row.slot_id in configured}
        for _part, row in named:
            if row.slot_id not in configured and row not in triad:
                triad.append(row)
                additional.append(row.slot_id)
        facts: Dict[str, Any] = {"composition": "full_pool", "chosen_by": "owner",
                                 "reviewers_subset_ignored": bool(named_configured) and named_configured != configured}
    elif named:
        for part, row in named:
            if row not in triad and row not in scope:
                (scope if part == PART_COUPLING else triad).append(row)
        facts = {"composition": "composed", "chosen_by": "author"}
    else:
        triad, scope = list(config.triad), list(config.scope)
        facts = {"composition": "full_pool", "chosen_by": "owner"}
    narrowed = facts["composition"] == "composed" and not configured <= {row.slot_id for row in (*triad, *scope)}
    ignored: List[str] = []
    for name in request.coupling_only:
        _part, row = seat(name)
        if any(row.slot_id == seated.slot_id for seated in (*triad, *scope)):
            ignored.append(name)
            continue
        scope.append(row)
        additional.append(row.slot_id)
    triad, scope, effort = _apply_effort(request.reviewer_effort, triad, scope)
    if not triad:
        raise ReviewChangeArgumentError("the panel has no seat for the change question; name at least one reviewer")
    if len(triad) > slots.TRIAD_SLOT_LIMIT or len(scope) > slots.SCOPE_SLOT_LIMIT:
        raise ReviewChangeArgumentError(
            f"a wave seats at most {slots.TRIAD_SLOT_LIMIT} change and {slots.SCOPE_SLOT_LIMIT} coupling reviewers "
            f"(this call asks {len(triad)} and {len(scope)})")
    facts.update(reason=request.reason, reason_missing=narrowed and not request.reason.strip(),
                 reviewers_requested=list(request.reviewers), coupling_only=list(request.coupling_only),
                 coupling_only_ignored=ignored, additional=list(additional), reviewer_effort=effort)
    facts.setdefault("reviewers_subset_ignored", False)
    override = (tuple(triad), tuple(scope)) != (tuple(config.triad), tuple(config.scope))
    return ComposedPanel(tuple(triad), tuple(scope), tuple(additional), override, facts)


@contextlib.contextmanager
def _panel_in_force(panel: ComposedPanel) -> Iterator[None]:
    if not panel.override:
        yield
        return
    from ouroboros.reviewer_slot_config import composed_review_panel

    with composed_review_panel(panel.triad, panel.scope):
        yield


# --- The wave ---------------------------------------------------------------------

@dataclass(frozen=True)
class _Wave:
    request: ReviewChangeRequest
    frozen: Any
    panel: ComposedPanel
    root: pathlib.Path
    fact: Any
    layer: str
    rules: Dict[str, Any]
    enforcement: str
    contract_fp: str
    rebuttal_sha: str
    retry_key: str
    reuse_key: str
    root_task_id: str
    record_id: str
    label: str
    # The open attempt row of this same round on this task that the wave collects
    # instead of paying again (review_change_custody.pending_round_attempt); None = new.
    rejoin: Any = None


def _round_sha(request: ReviewChangeRequest, frozen: Any) -> str:
    """Identity (b) of this request over this frozen subject."""
    from ouroboros.tools.commit_gate import compute_rebuttal_sha256

    return review_round_sha(frozen, rebuttal_sha=str(compute_rebuttal_sha256(request.review_rebuttal) or ""),
                            questions=request.author_questions, goal=request.goal, scope=request.scope)


def _wave_label(request: ReviewChangeRequest, root: pathlib.Path) -> str:
    span = {"base..head": f"{request.base}..{request.head}", "index": "staged index", "worktree": "worktree"}
    against = f" against {request.base}" if request.base and request.subject != "base..head" else ""
    return f"review_change: {span[request.subject]}{against} of {root.name} (review only; nothing is committed)"


def _prepare_wave(ctx: ToolContext, request: ReviewChangeRequest, frozen: Any, panel: ComposedPanel, *,
                  root: pathlib.Path, fact: Any, layer: str) -> _Wave:
    """Every identity of this wave, computed under its own panel."""
    from ouroboros.tools.commit_gate import commit_review_contract_fingerprint, compute_rebuttal_sha256, resolve_root_task_id

    rules = checklist_fingerprint(layer)
    enforcement = str(get_review_enforcement() or "")
    contract_fp = str(commit_review_contract_fingerprint() or "")
    rebuttal_sha = str(compute_rebuttal_sha256(request.review_rebuttal) or "")
    # Identity (b), the logical round, enters both the reuse key (a) and the custody
    # retry key (c): a new rebuttal/question/brief/revision pair is a new wave and a
    # new physical operation; a retry of the same round rejoins the old one.
    round_sha = _round_sha(request, frozen)
    reuse_key = review_reuse_key(
        frozen, rules_sha=str((rules.get("rules_source") or {}).get("sha") or ""), layer=layer,
        assigned=panel.assigned, enforcement=enforcement, contract_fp=contract_fp, round_sha=round_sha)
    return _Wave(request=request, frozen=frozen, panel=panel, root=root, fact=fact, layer=layer, rules=rules,
                 enforcement=enforcement, contract_fp=contract_fp, rebuttal_sha=rebuttal_sha,
                 retry_key=review_retry_key(frozen, round_sha=round_sha), reuse_key=reuse_key,
                 root_task_id=str(resolve_root_task_id(ctx) or ""), record_id=new_record_id(),
                 label=_wave_label(request, root))


# The shared task context's review state; the wave runs with this call's
# identities and a fresh history, and every field is restored afterwards.
_CTX_FIELDS = (
    "last_push_succeeded", "_review_advisory", "_last_triad_models", "_last_coupling_result",
    "_last_triad_raw_results", "_last_review_critical_findings", "_last_review_block_reason",
    "_last_review_advisory_findings", "_last_review_verdict", "_last_scope_raw_result",
    "_last_review_structured", "_review_degraded_reasons", "_current_review_tool_name",
    "_current_review_retry_key", "_current_review_record_id", "_review_reconcile_only",
    "_review_frozen_rows", "_review_custody_lost", "_current_review_attempt_number",
    "_author_commit_source", "_author_commit_decision", "_author_commit_record", "_commit_review_status",
    "_current_review_rebuttal_sha256", "_current_review_contract_fingerprint", "_review_history",
    "_review_iteration_count", "_coupling_review_history", "_coupling_review_history_rounds",
    "_triad_withheld_seat_records",
    "_review_paid_stamp", "_review_reserved_roster", "_review_reserved_operations",
    "_review_pending_invocation_checkpoint", "_last_review_slot_executions", "_pending_review_attempt",
)


@contextlib.contextmanager
def _wave_context(ctx: ToolContext, wave: _Wave) -> Iterator[None]:
    from ouroboros.tools import git as git_mod

    missing = object()
    saved = {name: getattr(ctx, name, missing) for name in _CTX_FIELDS}
    try:
        git_mod._reset_commit_review_state(ctx)
        ctx._current_review_tool_name = TOOL_NAME
        ctx._current_review_retry_key = wave.retry_key
        ctx._current_review_record_id = wave.record_id
        ctx._current_review_rebuttal_sha256 = wave.rebuttal_sha
        ctx._current_review_contract_fingerprint = wave.contract_fp
        ctx._review_history, ctx._review_iteration_count, ctx._coupling_review_history = [], 0, {}
        yield
    finally:
        for name, value in saved.items():
            if value is missing:
                with contextlib.suppress(AttributeError):
                    delattr(ctx, name)
            else:
                setattr(ctx, name, value)


def _dispatch(ctx: ToolContext, wave: _Wave) -> Dict[str, Any]:
    """Run the one wave; the paid stamp records it at its first physical dispatch.
    A rejoin runs the same wave on the gate's reconcile-only path (nothing new is
    sent; the open operation is collected under the attempt row it opened)."""
    from ouroboros.tools import git as git_mod

    holder = install_paid_stamp(ctx, wave)
    arm_rejoin(ctx, wave)
    asked = "".join(f"\n{number}. {question}" for number, question in enumerate(wave.request.author_questions, 1))
    goal = f"{wave.request.goal}\n\nAuthor questions (answer each as asked):{asked}".lstrip("\n") if asked else wave.request.goal
    try:
        review_err, _coupling, block_reason, _advisory = run_parallel_review(
            ctx, wave.label, goal=goal, scope=wave.request.scope,
            review_rebuttal=wave.request.review_rebuttal,
            review_binding_fingerprint=str(wave.frozen.diff_sha), subject=wave.frozen)
    except Exception as exc:  # the wave's own failure is a typed infra block, never a verdict
        log.warning("review_change wave failed", exc_info=True)
        return {"blocked": True, "block_reason": "infra_failure", "crash": f"{type(exc).__name__}: {exc}",
                "attempt": holder}
    finally:
        git_mod._reconcile_and_clear_review_roster(ctx)
    # The one wave decides once (review_ledger.reduce_verdict): a coupling FAIL is
    # a critical finding of the same verdict, never a second block.
    if review_err:
        return {"blocked": True, "block_reason": str(block_reason or ""), "attempt": holder}
    return {"blocked": False, "block_reason": "", "attempt": holder}


def _forensic(ctx: ToolContext) -> Dict[str, Any]:
    return {
        "structured": dict(getattr(ctx, "_last_review_structured", {}) or {}),
        "triad_raw": [row for row in (getattr(ctx, "_last_triad_raw_results", []) or []) if isinstance(row, dict)],
        "degraded_reasons": [str(item) for item in (getattr(ctx, "_review_degraded_reasons", []) or [])],
        "block_reason": str(getattr(ctx, "_last_review_block_reason", "") or ""),
        "custody_lost": bool(getattr(ctx, "_review_custody_lost", False)),
        # This wave's seat executions (``reviewer_slot_config.record_reviewer_slot_execution``
        # keeps them on the wave's ctx), never the process-wide last-execution projection.
        "slot_executions": dict(getattr(ctx, "_last_review_slot_executions", {}) or {}),
    }


def _open_seats(forensic: Dict[str, Any]) -> List[str]:
    rows = list(forensic.get("triad_raw", []))
    return [str(row.get("slot_id") or "") for row in rows
            if bool(row.get("late_result_pending")) or str(row.get("operation_state") or "") in {"in_flight", "custody_lost"}]


def _seats_open(forensic: Dict[str, Any]) -> bool:
    return bool(forensic.get("custody_lost")) or bool(_open_seats(forensic))


def _checkout_custody(ctx: ToolContext, forensic: Dict[str, Any]) -> Dict[str, Any]:
    """What keeps the isolated checkout alive after the wave, ``{}`` when nothing:
    a seat whose answer is still owed (late, in flight, custody lost) or an open
    preflight run on this context — read INSIDE the wave context, where the wave's
    own review state is still on ``ctx`` (``git_review_cycle._review_custody_pending``)."""
    from ouroboros.tools import git as git_mod

    facts: Dict[str, Any] = {}
    seats = _open_seats(forensic)
    if seats:
        facts["seats"] = seats
    if forensic.get("custody_lost"):
        facts["custody_lost"] = True
    if git_mod._review_custody_pending(ctx):
        facts["review_custody_pending"] = True
    return facts


def seat_findings(triad_raw: Sequence[Dict[str, Any]],
                  additional: set) -> Tuple[List[dict], List[dict], List[dict]]:
    """(critical, advisory, additional) FAIL items, each attributed to its seat
    and the PART it answered (``answers`` by part; a record without ``answers``
    is a packet seat whose ``parsed_items`` are the change's)."""
    critical, advisory, extra = [], [], []

    def _severity(item: Dict[str, Any]) -> str:
        return "critical" if str(item.get("severity") or "").lower() == "critical" else "advisory"

    for raw in triad_raw:
        seat = str(raw.get("slot_id") or "")
        answers = raw.get("answers") if isinstance(raw.get("answers"), dict) else None
        if answers:
            items = [(part, item) for part in PARTS for item in (answers.get(part) or {}).get("findings") or []
                     if isinstance(item, dict)]
        else:
            items = [(PART_CHANGE, item) for item in raw.get("parsed_items") or []
                     if isinstance(item, dict) and str(item.get("verdict") or "FAIL").upper() == "FAIL"]
        for part, item in items:
            severity = _severity(item)
            finding = {**item, "seat_id": seat, "part": part, "severity": severity}
            (extra if seat in additional else critical if severity == "critical" else advisory).append(finding)
    return critical, advisory, extra


def wave_facts(ctx: ToolContext, wave: _Wave, *, outcome: Dict[str, Any], forensic: Dict[str, Any]) -> Dict[str, Any]:
    """The ledger facts of this wave, in the commit gate's vocabulary."""
    from ouroboros.review_records import ReviewRequest, resolve_review_wave
    from ouroboros.tools import git as git_mod
    from ouroboros.tools.review_helpers import review_enforcement_blocks

    task_id = str(getattr(ctx, "task_id", "") or "")
    structured = dict(forensic.get("structured") or {})
    triad_raw = list(forensic.get("triad_raw") or [])
    refusal = outcome.get("dispatch_refusal")
    if refusal is None and forensic.get("block_reason") == "review_wave_budget_insufficient":
        refusal = {"kind": "review_wave_budget_insufficient", "message": str(structured.get("wave_refusal") or "")}
    degraded = list(forensic.get("degraded_reasons") or [])
    if outcome.get("crash"):
        degraded.append(f"review_change_wave_crashed: {outcome['crash']}")
    critical, advisory, additional = seat_findings(triad_raw, set(wave.panel.additional))
    executions = dict(forensic.get("slot_executions") or {})
    mode = ""
    with contextlib.suppress(Exception):
        mode = str(git_mod._current_runtime_mode() or "")
    return {
        "task_id": task_id, "root_task_id": wave.root_task_id,
        "review_wave_id": resolve_review_wave(ReviewRequest(
            surface=SURFACE, goal=wave.request.goal or wave.label, task_id=task_id, retry_key=wave.retry_key), {}, ""),
        "subject": wave.frozen, "repo_dir": str(wave.root), "commit_message": wave.label,
        "governance_root": str(wave.frozen.spec.governance_root or ""), "layer": wave.layer,
        "body_fact": str(wave.fact.body), "body_how": str(wave.fact.how),
        "goal": wave.request.goal, "scope": wave.request.scope, "author_questions": list(wave.request.author_questions),
        "binding_fingerprint": str(wave.frozen.diff_sha), "review_contract_fingerprint": wave.contract_fp,
        "rebuttal_sha256": wave.rebuttal_sha, "enforcement": wave.enforcement, "mode": mode,
        "enforcement_blocks": wave.layer == "body" and bool(review_enforcement_blocks(wave.enforcement)),
        "structured": structured, "slot_executions": executions, "triad_raw": triad_raw,
        "blocked": bool(outcome.get("blocked")), "block_reason": str(outcome.get("block_reason") or ""),
        "dispatch_refusal": refusal, "pending": _seats_open(forensic), "degraded_reasons": degraded,
        "critical_findings": critical, "advisory_findings": advisory, "additional_findings": additional,
        "tests": {"policy": "NOT_RUN", "result": "unknown"},
        "preflight": {"status": "not_performed", "record_id": ""},
        "reuse_key": wave.reuse_key if refusal is None else "", "retry_key": wave.retry_key,
        "composition": str(wave.panel.facts.get("composition") or "full_pool"),
        "composition_reason": str(wave.panel.facts.get("reason") or ""),
        "chosen_by": str(wave.panel.facts.get("chosen_by") or "owner"),
    }


def subject_facts(frozen: Any, retention: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The record's subject; ``retained_checkout`` names the isolated checkout kept
    alive by open custody (``retention`` says what), ``""`` when it was removed."""
    spec = frozen.spec
    checkout = str(getattr(frozen, "checkout", "") or "")
    return {"root_kind": str(spec.root_kind), "root": str(spec.root), "kind": str(spec.kind),
            "base": str(spec.base or getattr(frozen, "parent_sha", "") or ""), "head": str(spec.head or ""),
            "governance_root": str(getattr(spec, "governance_root", "") or ""),
            "tree_sha": str(getattr(frozen, "tree_sha", "") or ""), "diff_sha": str(getattr(frozen, "diff_sha", "") or ""),
            "checkout": checkout, "retained_checkout": checkout if retention else "",
            "retention": dict(retention or {})}


def _assigned_verdict(record: Any, rows: List[Dict[str, Any]], outcome: Dict[str, Any]) -> Dict[str, Any]:
    """The decision over the ASSIGNED seats only; added seats never count. The
    quorum is ``adaptive_quorum`` of the assigned seats — one number, as the gate's."""
    reason = str(outcome.get("block_reason") or "")
    verdict = reduce_verdict(
        rows, quorum_required=adaptive_quorum(len(rows)) if rows else 0,
        gate_blocked=bool(outcome.get("blocked")) and reason not in _ROW_STATED_BLOCKS, gate_reason=reason,
        dispatch_refusal=record.dispatch_refusal, pending=record.state == "pending")
    verdict["per_row"] = {**{str(s.get("seat_id") or ""): row_verdict(s) for s in record.rows}, **verdict["per_row"]}
    return verdict


def _finish_record(record: Any, wave: _Wave, outcome: Dict[str, Any], *, reuse_key: str,
                   retention: Optional[Dict[str, Any]] = None) -> None:
    record.subject = {**dict(record.subject or {}), **subject_facts(wave.frozen, retention)}
    record.brief = {**dict(record.brief or {}), "author_questions": list(wave.request.author_questions),
                    "checklist": {"layer": wave.layer, "body_fact": str(wave.fact.body), "how": str(wave.fact.how),
                                  "treat_as_body": wave.request.treat_as_body,
                                  "checklist_hash": str(wave.rules.get("checklist_hash") or ""),
                                  "rules_source": dict(wave.rules.get("rules_source") or {})}}
    record.fingerprints = {**dict(record.fingerprints or {}), "reuse_key": reuse_key, "retry_key": wave.retry_key}
    additional = set(wave.panel.additional)
    for seat in record.rows:
        seat["additional"] = str(seat.get("seat_id") or "") in additional
    panel = dict(record.panel or {})
    assigned_rows = [seat for seat in record.rows if not seat["additional"]]
    if additional and record.rows:
        verdict = _assigned_verdict(record, assigned_rows, outcome)
        record.verdict = {**dict(record.verdict or {}), **verdict}
        record.brief["parts"] = [part for part in PARTS if verdict["per_question"].get(part) != QUESTION_NOT_PERFORMED]
        panel = panel_facts(assigned_rows, composition=str(wave.panel.facts.get("composition") or "full_pool"),
                            reason=str(wave.panel.facts.get("reason") or ""),
                            chosen_by=str(wave.panel.facts.get("chosen_by") or "owner"))
    record.panel = {**panel, **wave.panel.facts}


def _settle(ctx: ToolContext, wave: _Wave, facts: Dict[str, Any], outcome: Dict[str, Any],
            retention: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build, finish and write the one record; the result is read back from it."""
    from ouroboros.reviewer_slot_config import bind_reviewer_slot_record_id

    drive = ledger_root(ctx)
    record = build_wave_record(facts, surface=SURFACE, record_id=wave.record_id, drive_root=drive)
    _finish_record(record, wave, outcome, reuse_key=str(facts.get("reuse_key") or ""), retention=retention)
    durable = True
    try:
        # A rejoin settles the SAME record the open operation left pending: the gate's
        # rule (commit_gate: a later settlement raises ``revision``, never a second record).
        payload = (revise_record(drive, wave.record_id, lambda _prior: record.to_dict())
                   if wave.rejoin is not None else None) or write_record(drive, record)
    except (OSError, ValueError) as exc:
        log.warning("review_change ledger record was not written", exc_info=True)
        payload, durable = (record.to_dict() if hasattr(record, "to_dict") else dict(record)), False
        verdict = dict(payload.get("verdict") or {})
        verdict["degraded_reasons"] = [*(verdict.get("degraded_reasons") or []), f"review_ledger_unwritten: {exc}"]
        payload["verdict"] = verdict
    # The gate's binding: this wave's OWN execution rows (keyed by seat, stamped
    # with the wave's timestamps) take the record id; a later run of a seat by
    # another surface keeps its own.
    with contextlib.suppress(Exception):
        bind_reviewer_slot_record_id(dict(facts.get("slot_executions") or {}), str(payload.get("record_id") or ""))
    settle_attempt(ctx, wave, outcome, payload, facts)
    result = review_result(payload, reused=False)
    result["durable"] = durable
    return result


def review_result(record: Dict[str, Any], *, reused: bool) -> Dict[str, Any]:
    """The tool's answer (contract item 3), read from one ledger record."""
    verdict = dict(record.get("verdict") or {})
    checklist = dict((record.get("brief") or {}).get("checklist") or {})
    return {
        "record_id": str(record.get("record_id") or ""), "state": str(record.get("state") or ""),
        "aggregate": str(verdict.get("aggregate") or ""), "per_question": dict(verdict.get("per_question") or {}),
        "quorum": dict(verdict.get("quorum") or {}), "panel": dict(record.get("panel") or {}),
        "rows": list(record.get("rows") or []), "subject": dict(record.get("subject") or {}),
        "checklist": {"layer": str(checklist.get("layer") or "core"),
                      "body_fact": str(checklist.get("body_fact") or "unknown"),
                      "how": str(checklist.get("how") or "unknown"),
                      "treat_as_body": bool(checklist.get("treat_as_body"))},
        "tests": dict(record.get("tests") or {"policy": "NOT_RUN", "result": "unknown"}),
        "cost": {"usd": 0.0, "unknown": False} if reused else dict(record.get("cost") or {}),
        "reused": reused,
        "enforcement": str(record.get("enforcement") or ""),
        "enforcement_blocks": bool(record.get("enforcement_blocks")),
        "findings": {name: list(verdict.get(name) or [])
                     for name in ("critical_findings", "advisory_findings", "additional_findings")},
        "degraded_reasons": list(verdict.get("degraded_reasons") or []),
        "dispatch_refusal": record.get("dispatch_refusal"),
    }


def _cycles_exhausted(ctx: ToolContext, wave: _Wave) -> Optional[Dict[str, Any]]:
    """The shared per-task-tree ceiling of (this root, ``review_change``)."""
    from ouroboros.tools.commit_gate import check_review_cycles_ceiling

    probe = SimpleNamespace(drive_root=ctx.drive_root, repo_dir=str(wave.root), _current_review_tool_name=TOOL_NAME)
    return check_review_cycles_ceiling(probe, root_task_id=wave.root_task_id)


def _refuse_exhausted(ctx: ToolContext, wave: _Wave, exhausted: Dict[str, Any]) -> Dict[str, Any]:
    from ouroboros.review_cycles import emit_review_cycles_exhausted

    emit_review_cycles_exhausted(
        getattr(ctx, "event_queue", None), ctx.drive_root, surface=TOOL_NAME,
        task_id=str(getattr(ctx, "task_id", "") or ""), cycles_paid=int(exhausted["cycles_paid"]),
        cap=int(exhausted["cap"]), enforcement=wave.enforcement, root_task_id=wave.root_task_id,
        fingerprint=str(wave.frozen.diff_sha), root=str(wave.root))
    outcome = {"blocked": True, "block_reason": "review_cycles_exhausted",
               "dispatch_refusal": {"kind": "review_cycles_exhausted", "message": str(exhausted["message"])}}
    result = _settle(ctx, wave, wave_facts(ctx, wave, outcome=outcome, forensic={}), outcome)
    result["message"] = str(exhausted["message"])
    return result


def run_review_change(ctx: ToolContext, **args: Any) -> Dict[str, Any]:
    """The tool's runtime entry, shared with the operator script: one subject, at
    most one paid wave, one ledger record. A deterministic call error raises
    ``ReviewChangeArgumentError`` before any reviewer is paid."""
    binding = args.pop("_resolved_binding", None)
    request = parse_request(args)
    root_kind, root = resolve_review_root(ctx, binding, request)
    _verify_revisions(root, request)
    governance = _governance_repo(ctx)
    fact = body_fact(root, system_repo=governance, data_dir=ledger_root(ctx), treat_as_body=request.treat_as_body)
    layer = layer_for(fact)
    panel = compose_panel(request, adds_only=layer == "body"
                          and not runtime_mode_at_least(get_runtime_mode(), "cyber_pro"))
    spec = ReviewSubjectSpec(root_kind=root_kind, root=str(root), kind=request.subject, base=request.base,
                             head=request.head, governance_root=str(governance), surface=SURFACE,
                             body_fact=str(fact.body), body_how=str(fact.how), layer=layer)
    # The gate's own subject (the body's staged index against HEAD) is read on its live
    # root exactly as the gate reads it; every other subject is materialized in an
    # isolated checkout, where every delivery reads the frozen tree, at the path of this
    # TASK's custody of its ROUND (identity c plus the task that keys attempt rows): a
    # rerun of the round reads the checkout its open operation still may, another
    # task's wave of the round never removes it, and open custody after the wave keeps
    # it (review_subject.isolated_checkout).
    retention: Dict[str, Any] = {}
    task_id = str(getattr(ctx, "task_id", "") or "")
    frozen_subject = (contextlib.nullcontext(freeze_subject(ctx, spec)) if is_gate_subject(spec)
                      else isolated_checkout(ctx, spec, retain=lambda: retention, token=lambda identity: checkout_token(
                          review_retry_key(identity, round_sha=_round_sha(request, identity)), task_id=task_id)))
    with frozen_subject as frozen, _panel_in_force(panel):
        if not str(getattr(frozen, "diff_text", "") or "").strip():
            raise ReviewChangeArgumentError(f"subject={request.subject} of {root} has no change to review")
        wave = _prepare_wave(ctx, request, frozen, panel, root=root, fact=fact, layer=layer)
        # This task's open operation of the round is collected first, not paid for again
        # and not answered by another task's settled record of the same round: its paid
        # seats settle into its own record. Only then may a settled record be reused, and
        # the cycle ceiling meets only a NEW paid wave, so it never strands an answer.
        rejoin = pending_round_attempt(ctx, root=wave.root, retry_key=wave.retry_key)
        if rejoin is not None:
            wave = dataclasses.replace(wave, rejoin=rejoin, record_id=str(rejoin.review_record_id or wave.record_id))
        else:
            prior = reuse_or_none(ledger_root(ctx), wave.reuse_key, questions=request.author_questions)
            if prior is not None:
                return review_result(prior["record"], reused=True)
            exhausted = _cycles_exhausted(ctx, wave)
            if exhausted is not None:
                return _refuse_exhausted(ctx, wave, exhausted)
        with _wave_context(ctx, wave):
            outcome = _dispatch(ctx, wave)
            forensic = _forensic(ctx)
            retention.update(_checkout_custody(ctx, forensic))
        return _settle(ctx, wave, wave_facts(ctx, wave, outcome=outcome, forensic=forensic), outcome, retention)


def _handle_review_change(
    ctx: ToolContext, root: str = "active_workspace", workspace_root: str = "", subject: str = "",
    base: str = "", head: str = "", surface: str = SURFACE, goal: str = "", scope: str = "",
    author_questions: Optional[List[str]] = None, reviewers: Optional[List[str]] = None, reason: str = "",
    coupling_only: Optional[List[str]] = None, reviewer_effort: str = "", review_rebuttal: str = "",
    treat_as_body: bool = False, _resolved_binding: Any = None,
) -> str:
    try:
        result = run_review_change(
            ctx, root=root, workspace_root=workspace_root, subject=subject, base=base, head=head, surface=surface,
            goal=goal, scope=scope, author_questions=author_questions, reviewers=reviewers, reason=reason,
            coupling_only=coupling_only, reviewer_effort=reviewer_effort, review_rebuttal=review_rebuttal,
            treat_as_body=treat_as_body, _resolved_binding=_resolved_binding)
    except ReviewChangeArgumentError as exc:
        return argument_refusal(ctx, "TOOL_ARG_ERROR (review_change)", [str(exc)], effect="No reviewer was dispatched.")
    text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    return f"{result['message']}\n\n{text}" if result.get("message") else text


def _review_change_tool_timeout_sec() -> float:
    # The plan_task envelope: a settlement bound covering an API transport read or
    # a delegated session's operation window, never a cognition cutoff.
    return (max(float(get_llm_transport_read_timeout_sec() + get_finalization_grace_sec()),
                operation_window_sec(get_task_abs_ceiling_sec()))
            + get_finalization_grace_sec())


_DESCRIPTION = (
    "Review a change WITHOUT committing it: one paid reviewer wave over a subject in any registered repository, "
    "written to the review ledger. commit_reviewed stays the only landing in the system repository; use "
    "review_change by judgment for any other root (it never starts on its own and never runs tests: "
    "tests.policy=NOT_RUN). Subjects: base..head (commits), index (staged) or worktree (live) against base "
    "(default HEAD); every subject but the body's own staged index is frozen and read in an isolated checkout of "
    "its tree. Rules follow the subject: the body is read against the body "
    "layer, anything else against the universal core checklist. Panel: for a body subject outside Cyber Pro the "
    "configured panel stands and reviewers only add seats; otherwise reviewers IS the panel (omitted = every "
    "configured seat) and reason records why. The same subject, rules and panel return the settled record free "
    "(reused=true); enforcement is recorded and on another root never locks anything. Returns JSON {record_id, "
    "aggregate, per_question, panel, rows, subject, checklist, tests, cost, reused, findings}."
)


def _string(description: str, **extra: Any) -> Dict[str, Any]:
    return {"type": "string", "default": "", "description": description, **extra}


def _names(description: str) -> Dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "default": [], "description": description}


def get_tools() -> List[ToolEntry]:
    properties = {
        "root": _string("The bound repository: the active workspace or the system repository.",
                        enum=list(ROOTS), default="active_workspace"),
        "workspace_root": _string("A registered folder to review instead (schedule_subagent's workspace_root policy)."),
        "subject": {"type": "string", "enum": list(SUBJECT_KINDS),
                    "description": "base..head = commits between two revisions; index = staged; worktree = live."},
        "base": _string("Base revision (required for base..head; index/worktree default HEAD)."),
        "head": _string("Head revision (base..head only)."),
        "surface": _string("The review surface.", enum=[SURFACE], default=SURFACE),
        "goal": _string("What the change is meant to achieve."),
        "scope": _string("What the change deliberately covers."),
        "author_questions": _names("Your own questions to the reviewers, passed verbatim."),
        "reviewers": _names("Configured seat or subagent ids (see the panel rule)."),
        "reason": _string("Why this panel; recorded with it."),
        "coupling_only": _names("Extra critics for the coupling question only; outside the quorum."),
        "reviewer_effort": {"type": "string", "enum": list(EFFORT_SCALE), "description": (
            "Panel strength for THIS wave; outranks each seat's effort (a compound route keeps its own). "
            "Omitted = the owner's settings.")},
        "review_rebuttal": _string("Your answer to the previous wave's findings; buys a new wave."),
        "treat_as_body": {"type": "boolean", "default": False, "description": (
            "Raise a root whose body fact is unknown (git cannot place it: no remote, no copy binding) to the "
            "body layer. A recognized body and a recognized foreign root are unchanged; the record notes the raise.")},
    }
    schema = {"name": "review_change", "description": _DESCRIPTION,
              "parameters": {"type": "object", "properties": properties, "required": ["subject"]}}
    return [ToolEntry("review_change", schema, _handle_review_change, timeout_sec=_review_change_tool_timeout_sec())]
