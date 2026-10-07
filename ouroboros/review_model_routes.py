"""Ouroboros — the reviewer-quorum rule and the env-plane reviewer model list.

The review POOL (marked rows of ``OUROBOROS_SUBAGENTS``, read by
``reviewer_slot_config.review_pool_slots``) is the one configuration surface
of every review family. The retired comma ENV key read below survives only as
an operational env-override plane (the external review script's ordering) and
falls back to the shipped ``OPENROUTER_REVIEW_DEFAULTS``; the
install-class adaptations the lane era applied here — Main repeated N times for
a local-only, compatible-only or exclusive-direct-provider install — are minted
into the catalog once by ``subscription_install_presets.factory_review_rows``
and are no longer a read-time multiplication. ``adaptive_quorum`` is shared by
every review family.
"""

from __future__ import annotations

import dataclasses

from ouroboros.model_slots import ResolvedModelTarget, _main_model, _parse_model_list
from ouroboros.provider_models import (
    _NON_COMPATIBLE_REMOTE_KEYS,
    compatible_only_main_model,
    local_only_review_route_env,
    migrate_model_value,
    resolve_model_target,
    review_model_uses_local,
)
from ouroboros.settings_defaults import OPENROUTER_REVIEW_DEFAULTS, SETTINGS_DEFAULTS
from ouroboros.settings_integrity import runtime_setting

_DIRECT_PROVIDER_REVIEW_RUNS = 3


def _exclusive_direct_remote_provider_env() -> str:
    has_openrouter = bool(str(runtime_setting("OPENROUTER_API_KEY", "") or "").strip())
    has_openai = bool(str(runtime_setting("OPENAI_API_KEY", "") or "").strip())
    has_anthropic = bool(str(runtime_setting("ANTHROPIC_API_KEY", "") or "").strip())
    has_minimax = bool(str(runtime_setting("MINIMAX_API_KEY", "") or "").strip())
    has_legacy_base = bool(str(runtime_setting("OPENAI_BASE_URL", "") or "").strip())
    has_compatible = bool(str(runtime_setting("OPENAI_COMPATIBLE_BASE_URL", "") or "").strip())
    has_cloudru = bool(str(runtime_setting("CLOUDRU_FOUNDATION_MODELS_API_KEY", "") or "").strip())
    has_gigachat = bool(str(runtime_setting("GIGACHAT_CREDENTIALS", "") or "").strip()) or (
        bool(str(runtime_setting("GIGACHAT_USER", "") or "").strip())
        and bool(str(runtime_setting("GIGACHAT_PASSWORD", "") or "").strip())
    )
    # OpenRouter / legacy OpenAI base / OpenAI-compatible all route through the
    # OpenRouter-style stack, so their presence means "not an exclusive direct
    # provider". Among the registered direct providers, return one only when
    # exactly one is configured.
    if has_openrouter or has_legacy_base or has_compatible:
        return ""
    direct = [name for name, present in (
        ("openai", has_openai), ("anthropic", has_anthropic), ("minimax", has_minimax),
        ("cloudru", has_cloudru), ("gigachat", has_gigachat),
        ("deepseek", bool(str(runtime_setting("DEEPSEEK_API_KEY", "") or "").strip())),
        ("zai", bool(str(runtime_setting("ZAI_API_KEY", "") or "").strip())),
    ) if present]
    return direct[0] if len(direct) == 1 else ""


# removed by package B together with ``get_scope_review_models``.
def compatible_only_review_model() -> str:
    """Main's route when the OpenAI-compatible endpoint is the only remote provider (#1116)."""
    keys = ("OPENAI_COMPATIBLE_BASE_URL", "OUROBOROS_MODEL", "GIGACHAT_USER", "GIGACHAT_PASSWORD",
            *_NON_COMPATIBLE_REMOTE_KEYS)
    return compatible_only_main_model({key: runtime_setting(key, "") for key in keys})


# removed by package B together with ``get_scope_review_models``.
def _compatible_only_models(models: list[str]) -> list[str]:
    """An unreachable (non-compatible) list becomes Main repeated; an explicit compatible list stays."""
    main = compatible_only_review_model()
    if not main or (models and all(str(m).startswith("openai-compatible::") for m in models)):
        return models
    return [main] * max(1, len(models))


def adaptive_quorum(n_slots: int) -> int:
    """Reviewer-quorum SSOT for an ARBITRARY configured slot count, reused by
    triad/scope/plan/skill/acceptance review. One configured reviewer needs 1 (a loud
    single_reviewer_no_diversity degraded mode), 2 need both, 3+ keep the classic 2-of-N
    majority. DISTINCT from "configured >= quorum but fewer responded", which stays a loud
    infra quorum FAILURE at the call site."""
    return 2 if n_slots >= 3 else max(1, n_slots)


def get_review_models() -> list[str]:
    """The env-plane triad model list: the retired comma key (else the shipped
    OpenRouter defaults), spelled in an exclusive direct provider's own
    form. No multiplication: the lane-era ``[main]×N`` fallbacks for a
    local-only, compatible-only or direct-provider install are catalog rows now
    (``factory_review_rows``), so this reader returns the list as configured."""
    default_str = ",".join(OPENROUTER_REVIEW_DEFAULTS["triad"])
    models_str = runtime_setting("OUROBOROS_REVIEW_MODELS", default_str) or default_str
    models = _parse_model_list(models_str)
    provider = _exclusive_direct_remote_provider_env()
    if not provider:
        return models
    return [migrate_model_value(provider, model) for model in models]


def resolved_review_model_target(model: str, *, effort: str = "") -> ResolvedModelTarget:
    """Construct the ABI-4 typed target for ONE resolved reviewer model.

    The review seam's transport predicate is ``review_model_uses_local`` (a
    local-only Main route pins EVERY review slot to the local lane), so the
    typed ``provider_route`` says ``"local"`` exactly when that predicate
    does — downstream slot builders read the dataclass instead of re-asking
    the predicate per model string. Purely a typed view: the model lists
    themselves stay ``get_review_models``/``get_scope_review_models``.
    """
    target = resolve_model_target(model, effort=effort)
    if target.provider_route != "local" and review_model_uses_local(target.model_id):
        target = dataclasses.replace(target, provider_route="local")
    return target


def get_review_targets() -> tuple[ResolvedModelTarget, ...]:
    """The effective triad list as typed targets (ABI-4), same order/membership.

    TYPED VIEW FOR FUTURE CONSUMERS — no production caller yet: today's review
    lanes consume ``get_review_models`` plus ``resolved_review_model_target``
    per slot (reviewer_slot_config); wiring a whole-list consumer is review-
    surface work outside the ABI-4 sweep's byte-identical contract."""
    return tuple(resolved_review_model_target(model) for model in get_review_models())


# removed by package B (the scope lane's typed view; ``get_scope_review_models`` goes with it).
def get_scope_review_targets() -> tuple[ResolvedModelTarget, ...]:
    """The effective scope list as typed targets (ABI-4), duplicates preserved.

    TYPED VIEW FOR FUTURE CONSUMERS — no production caller yet (see
    ``get_review_targets``)."""
    return tuple(resolved_review_model_target(model) for model in get_scope_review_models())


def get_review_enforcement() -> str:
    """Return the configured pre-commit review enforcement mode."""
    default_val = str(SETTINGS_DEFAULTS["OUROBOROS_REVIEW_ENFORCEMENT"])
    raw = (runtime_setting("OUROBOROS_REVIEW_ENFORCEMENT", default_val) or default_val).strip().lower()
    return raw if raw in {"advisory", "blocking"} else default_val


# removed by package B (the scope lane's model list; its last readers are
# ``review_substrate.scope_reviewer_slots`` and ``tools/scope_review.py``).
def get_scope_review_models() -> list[str]:
    """Return effective scope reviewer models, preserving duplicate model IDs."""
    default_str = ",".join(OPENROUTER_REVIEW_DEFAULTS["scope"])
    raw = runtime_setting("OUROBOROS_SCOPE_REVIEW_MODELS", "") or ""
    if not raw.strip():
        raw = runtime_setting("OUROBOROS_SCOPE_REVIEW_MODEL", default_str) or default_str
    models = _parse_model_list(raw)
    singular = str(runtime_setting("OUROBOROS_SCOPE_REVIEW_MODEL", OPENROUTER_REVIEW_DEFAULTS["scope"][0]) or "").strip()
    if not models and singular:
        models = [singular]
    if not models:
        models = _parse_model_list(default_str)
    models = [_main_model()] * max(1, len(models)) if local_only_review_route_env() else models
    provider = _exclusive_direct_remote_provider_env()
    if not provider:
        return _compatible_only_models(models)
    migrated = [migrate_model_value(provider, model) for model in models]
    provider_prefix = f"{provider}::"
    if migrated and all(model.startswith(provider_prefix) for model in migrated):
        return migrated
    migrated_singular = migrate_model_value(provider, singular or OPENROUTER_REVIEW_DEFAULTS["scope"][0])
    if migrated_singular.startswith(provider_prefix):
        return [migrated_singular]
    return migrated
