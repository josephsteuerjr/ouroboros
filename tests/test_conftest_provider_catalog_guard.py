"""Self-pin of the root conftest's provider-catalog guard: `LLMClient`'s class caches
(`_SUPPORTED_PARAMS_CACHE`, `_CONTEXT_LENGTH_CACHE` and the two fetch flags) are process
state. A test whose window measurement fetched OpenRouter's live `/models` catalog must not
decide what the next test's send strips (`test_llm_no_proxy`'s parameter-rejection retry
had nothing left to drop). The guard lives in tests/conftest.py; this file exercises its
pieces directly and checks that every test runs under it."""
from __future__ import annotations

import pathlib
import sys

conftest = next(
    m for m in list(sys.modules.values())
    if getattr(m, "__file__", "")
    and pathlib.Path(m.__file__).as_posix().endswith("tests/conftest.py")
    and hasattr(m, "_restore_provider_catalog")
)


def _fetched_like_the_live_catalog(cls):
    cls._SUPPORTED_PARAMS_CACHE["probe/model"] = {"max_tokens"}
    cls._CONTEXT_LENGTH_CACHE["probe/model"] = 128_000
    cls._SUPPORTED_PARAMS_FETCHED = True
    cls._CAPABILITIES_FETCH_OK = True


def test_every_test_runs_under_the_provider_catalog_guard(request):
    assert "_provider_catalog_state_returns" in request.fixturenames


def test_a_fetch_during_a_test_is_handed_back_in_place():
    from ouroboros.llm import LLMClient

    params_dict, windows_dict = LLMClient._SUPPORTED_PARAMS_CACHE, LLMClient._CONTEXT_LENGTH_CACHE
    saved = conftest._provider_catalog_snapshot(LLMClient)
    _fetched_like_the_live_catalog(LLMClient)
    assert "probe/model" in LLMClient._SUPPORTED_PARAMS_CACHE and LLMClient._SUPPORTED_PARAMS_FETCHED

    conftest._restore_provider_catalog(LLMClient, saved)

    assert conftest._provider_catalog_snapshot(LLMClient) == saved
    # The mixin's own dict objects come back, not a rebinding on the subclass.
    assert LLMClient._SUPPORTED_PARAMS_CACHE is params_dict and LLMClient._CONTEXT_LENGTH_CACHE is windows_dict
    assert "probe/model" not in params_dict and "probe/model" not in windows_dict


def test_a_class_imported_during_the_test_is_found_pristine():
    from ouroboros.llm import LLMClient

    saved = conftest._provider_catalog_snapshot(LLMClient)
    _fetched_like_the_live_catalog(LLMClient)
    conftest._restore_provider_catalog(LLMClient, conftest._PROVIDER_CATALOG_PRISTINE)
    assert conftest._provider_catalog_snapshot(LLMClient) == {
        "_SUPPORTED_PARAMS_CACHE": {}, "_SUPPORTED_PARAMS_FETCHED": False,
        "_CONTEXT_LENGTH_CACHE": {}, "_CAPABILITIES_FETCH_OK": False,
    }
    assert conftest._PROVIDER_CATALOG_PRISTINE["_SUPPORTED_PARAMS_CACHE"] == {}  # the template stayed empty
    conftest._restore_provider_catalog(LLMClient, saved)
