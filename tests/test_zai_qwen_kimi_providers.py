
"""Regressions for the Z.ai / DashScope / Moonshot direct providers."""
import os

import pytest

from ouroboros import provider_models
from ouroboros.provider_models import (
    DASHSCOPE_BASE_URL,
    MOONSHOT_BASE_URL,
    ZAI_DIRECT_DEFAULTS,
    ZAI_PLAN_ENDPOINTS,
    DIRECT_PROVIDER_DEFAULTS,
    DIRECT_PROVIDER_REVIEW_ROLES,
    DIRECT_PROVIDER_SCOPE_DEFAULTS,
    migrate_model_value,
    normalize_model_identity,
    provider_for_model,
    provider_has_credentials,
)

_PROVIDER_ENV_KEYS = (
    "OPENROUTER_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
    "OPENAI_COMPATIBLE_API_KEY", "OPENAI_COMPATIBLE_BASE_URL",
    "ANTHROPIC_API_KEY", "MINIMAX_API_KEY", "DEEPSEEK_API_KEY",
    "ZAI_API_KEY", "ZAI_PLAN", "DASHSCOPE_API_KEY", "MOONSHOT_API_KEY",
    "CLOUDRU_FOUNDATION_MODELS_API_KEY", "GIGACHAT_CREDENTIALS",
    "GIGACHAT_USER", "GIGACHAT_PASSWORD", "USE_LOCAL_MAIN",
)


def _clear_provider_env(monkeypatch):
    for key in _PROVIDER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


class TestRegistry:
    @pytest.mark.parametrize("prefix,provider", [
        ("zai", "zai"), ("qwen", "qwen"), ("kimi", "kimi"),
    ])
    def test_prefix_routes_direct(self, prefix, provider):
        assert provider_for_model(f"{prefix}::some-model") == provider

    @pytest.mark.parametrize("prefix", ["zai", "qwen", "kimi"])
    def test_slash_form_stays_openrouter(self, prefix):
        from ouroboros.pricing import infer_api_key_type
        assert provider_for_model(f"{prefix}/some-model") == "openrouter"
        assert infer_api_key_type(f"{prefix}/some-model") == "openrouter"
        assert infer_api_key_type(f"{prefix}::some-model") == prefix

    @pytest.mark.parametrize("provider,key", [
        ("zai", "ZAI_API_KEY"), ("qwen", "DASHSCOPE_API_KEY"), ("kimi", "MOONSHOT_API_KEY"),
    ])
    def test_credentials_mapping(self, monkeypatch, provider, key):
        _clear_provider_env(monkeypatch)
        assert provider_has_credentials(provider) is False
        monkeypatch.setenv(key, "sk-x")
        assert provider_has_credentials(provider) is True

    def test_direct_defaults_registered(self):
        assert DIRECT_PROVIDER_DEFAULTS["zai"] is ZAI_DIRECT_DEFAULTS
        assert ZAI_DIRECT_DEFAULTS["main"] == "zai::glm-5.3"
        assert ZAI_DIRECT_DEFAULTS["light"] == "zai::glm-5.3-flash"
        assert DIRECT_PROVIDER_REVIEW_ROLES["zai"] == ("main", "main", "main")
        assert DIRECT_PROVIDER_SCOPE_DEFAULTS["zai"] == "zai::glm-5.3"
        assert provider_models.QWEN_DIRECT_DEFAULTS["main"] == "qwen::qwen3-max"
        assert provider_models.KIMI_DIRECT_DEFAULTS["main"] == "kimi::kimi-k2-turbo-preview"

    @pytest.mark.parametrize("provider", ["zai", "qwen", "kimi"])
    def test_migrate_and_normalize_round_trip(self, provider):
        assert migrate_model_value(provider, f"{provider}/model-x") == f"{provider}::model-x"
        assert migrate_model_value(provider, f"{provider}::model-x") == f"{provider}::model-x"
        assert normalize_model_identity(f"{provider}::model-x") == f"{provider}/model-x"


class TestZaiPlanSwitch:
    def test_payg_default_endpoint(self, monkeypatch):
        _clear_provider_env(monkeypatch)
        monkeypatch.setenv("ZAI_API_KEY", "sk-x")
        from ouroboros.provider_models import resolve_zai_base_url
        assert resolve_zai_base_url(None) == ZAI_PLAN_ENDPOINTS["payg"]

    def test_coding_plan_endpoint(self, monkeypatch):
        from ouroboros.provider_models import resolve_zai_base_url
        assert resolve_zai_base_url("coding") == ZAI_PLAN_ENDPOINTS["coding"]

    def test_resolve_target_uses_plan(self, monkeypatch):
        _clear_provider_env(monkeypatch)
        monkeypatch.setenv("ZAI_API_KEY", "sk-x")
        monkeypatch.setenv("ZAI_PLAN", "coding")
        monkeypatch.setattr(
            "ouroboros.llm_routing.runtime_setting",
            lambda key, default="": os.environ.get(key, default),
        )
        from ouroboros.llm import LLMClient
        target = LLMClient()._resolve_remote_target("zai::glm-5.3")
        assert target["provider"] == "zai"
        assert target["base_url"] == ZAI_PLAN_ENDPOINTS["coding"]
        assert target["api_key"] == "sk-x"


class TestBaseUrls:
    def test_constants(self):
        assert ZAI_PLAN_ENDPOINTS["payg"].startswith("https://api.z.ai/")
        assert "coding" in ZAI_PLAN_ENDPOINTS["coding"]
        assert DASHSCOPE_BASE_URL.startswith("https://dashscope.")
        assert MOONSHOT_BASE_URL.startswith("https://api.moonshot.")


class TestSingleProviderIndependence:
    @pytest.mark.parametrize("provider,key", [
        ("zai", "ZAI_API_KEY"), ("qwen", "DASHSCOPE_API_KEY"), ("kimi", "MOONSHOT_API_KEY"),
    ])
    def test_exclusive_direct_env_detection(self, monkeypatch, provider, key):
        _clear_provider_env(monkeypatch)
        monkeypatch.setenv(key, "sk-x")
        from ouroboros.config import _exclusive_direct_remote_provider_env
        assert _exclusive_direct_remote_provider_env() == provider

    def test_startup_gate_accepts_zai_only(self):
        from ouroboros.server_runtime import (
            _exclusive_direct_remote_provider,
            has_remote_provider,
            has_startup_ready_provider,
        )
        settings = {"ZAI_API_KEY": "sk-x"}
        assert has_remote_provider(settings) is True
        assert has_startup_ready_provider(settings) is True
        assert _exclusive_direct_remote_provider(settings) == "zai"

    def test_review_fallback_compiles_for_zai(self, monkeypatch):
        _clear_provider_env(monkeypatch)
        monkeypatch.setenv("ZAI_API_KEY", "sk-x")
        monkeypatch.setenv("OUROBOROS_MODEL", "zai::glm-5.3")
        monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "zai::glm-5.3-flash")
        monkeypatch.setattr(
            "ouroboros.review_model_routes.runtime_setting",
            lambda key, default="": os.environ.get(key, default),
        )
        from ouroboros.config import get_review_models
        assert get_review_models() == ["zai::glm-5.3"] * 3

    def test_local_only_review_route_sees_zai(self, monkeypatch):
        _clear_provider_env(monkeypatch)
        monkeypatch.setenv("USE_LOCAL_MAIN", "1")
        monkeypatch.setenv("ZAI_API_KEY", "sk-x")
        assert provider_models.local_only_review_route_env() is False


class TestSecretSurfaces:
    @pytest.mark.parametrize("key", ["ZAI_API_KEY", "DASHSCOPE_API_KEY", "MOONSHOT_API_KEY"])
    def test_forbidden_and_masked(self, key):
        from ouroboros.contracts.plugin_api import FORBIDDEN_SKILL_SETTINGS
        from ouroboros.secret_masking import MASKED_SECRET_SETTING_KEYS
        assert key in FORBIDDEN_SKILL_SETTINGS
        assert key in MASKED_SECRET_SETTING_KEYS

    @pytest.mark.parametrize("key", ["ZAI_API_KEY", "DASHSCOPE_API_KEY", "MOONSHOT_API_KEY"])
    def test_settings_defaults(self, key):
        from ouroboros.config import SETTINGS_DEFAULTS
        assert SETTINGS_DEFAULTS[key] == ""


class TestSafetyRouting:
    @pytest.mark.parametrize("key", ["ZAI_API_KEY", "DASHSCOPE_API_KEY", "MOONSHOT_API_KEY"])
    def test_new_provider_key_counts_as_remote_safety_backend(self, monkeypatch, key):
        """A new-provider-only install must reach the real safety check, not fail open."""
        from ouroboros import safety
        _clear_provider_env(monkeypatch)
        assert safety._any_remote_provider_configured() is False
        monkeypatch.setenv(key, "sk-x")
        assert safety._any_remote_provider_configured() is True
        assert key in safety._REMOTE_PROVIDER_KEYS
        assert safety._PROVIDER_KEY_ENV["zai"] == "ZAI_API_KEY"
        assert safety._PROVIDER_KEY_ENV["qwen"] == "DASHSCOPE_API_KEY"
        assert safety._PROVIDER_KEY_ENV["kimi"] == "MOONSHOT_API_KEY"

    @pytest.mark.parametrize("model,provider", [
        ("zai::glm-5.3-flash", "zai"),
        ("qwen::qwen3-max", "qwen"),
        ("kimi::kimi-k2-turbo-preview", "kimi"),
    ])
    def test_light_model_reaches_its_provider_key(self, monkeypatch, model, provider):
        from ouroboros import safety
        _clear_provider_env(monkeypatch)
        key = {"zai": "ZAI_API_KEY", "qwen": "DASHSCOPE_API_KEY", "kimi": "MOONSHOT_API_KEY"}[provider]
        monkeypatch.setenv(key, "sk-x")
        # The routing resolver must classify the light model onto the configured
        # new provider instead of the no-backend fail-open path.
        from ouroboros.pricing import infer_api_key_type
        provider_key = safety._PROVIDER_KEY_ENV.get(infer_api_key_type(model))
        assert provider_key == key
