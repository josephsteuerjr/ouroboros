"""A failed pre-send checkpoint keeps its primary cause and authority attribution."""

import pytest

from ouroboros.review_execution import ReviewRouteUnavailable
from ouroboros.review_projection import _panel_transport, _review_actor_projection, _transport_error_status
from ouroboros.review_session_custody import checkpoint_pending_invocation
from tests._review_session_route_shared import (
    FakeLLM, _agent_request, _agent_slot,
    _owned_gateway_uses_each_test_transport as _transport_fixture,
    fake_route as _route_fixture,
)

_owned_gateway_uses_each_test_transport = _transport_fixture
fake_route = _route_fixture
pytestmark = pytest.mark.serial  # the session fixture mutates its shared transport registry


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_checkpoint_cause_survives_cleanup_and_clears_unsent_token(cleanup_fails):
    state = {"pending_invocation_id": "invocation"}
    primary = TimeoutError("review state unavailable (reason=permission, errno=13)")
    primary.reported_cause = '{"reason":"permission","errno":13}'
    cleanup_calls = []

    def checkpoint(_invocation):
        raise primary

    def cleanup():
        cleanup_calls.append(True)
        if cleanup_fails:
            raise RuntimeError("cleanup transport unavailable")

    with pytest.raises(ReviewRouteUnavailable) as raised:
        checkpoint_pending_invocation(checkpoint=checkpoint, invocation_id="invocation",
                                      state=state, on_failure=cleanup)
    assert raised.value.code == "review_custody_checkpoint_unwritable"
    assert raised.value.__cause__ is primary
    assert '"reason":"permission"' in raised.value.reported_cause
    assert ("cleanup transport unavailable" in raised.value.reported_cause) is cleanup_fails
    assert state == {} and cleanup_calls == [True]


def test_successful_checkpoint_does_not_cleanup_or_drop_binding():
    state = {"pending_invocation_id": "invocation"}
    recorded = []
    checkpoint_pending_invocation(checkpoint=recorded.append, invocation_id="invocation", state=state,
                                  on_failure=lambda: pytest.fail("cleanup on a successful binding"))
    assert recorded == ["invocation"] and state["pending_invocation_id"] == "invocation"


def test_local_failure_phase_controls_actor_and_panel_word_not_error_prose():
    error = RuntimeError("a timeout mentioned in local checkpoint prose")
    assert _transport_error_status(error, failure_phase="authority") == "authority_error"
    assert _transport_error_status(error, failure_phase="delivery") == "timeout"
    assert _transport_error_status(RuntimeError("provider unavailable"), failure_phase="delivery") == "provider_transport_error"
    actor = _review_actor_projection({
        "status": "error", "operation_state": "settled", "error": str(error),
        "usage": {"review_failure_phase": "authority"},
        "failure_code": "review_custody_checkpoint_unwritable",
    }, "multi_model_review")
    assert actor["transport_status"] == "authority_error"
    assert _panel_transport([actor["transport_status"]]) == "authority_error"


@pytest.mark.parametrize("refused", [False, True])
def test_real_session_checkpoint_preserves_lock_cause_before_any_post(tmp_path, fake_route, refused):
    from dataclasses import asdict
    from ouroboros.review_state import ReviewStateLockError
    from ouroboros.review_substrate import ReviewCoordinator
    from ouroboros.tools.review_response import parse_model_response
    from ouroboros.triad_review import parse_model_review_results

    def checkpoint(_invocation_id):
        if refused:
            raise ReviewStateLockError(tmp_path / "locks/advisory_review.lock", {
                "reason": "contention", "errno": None, "elapsed_sec": 4.0, "timeout_sec": 4.0,
            })

    actor = ReviewCoordinator(llm=FakeLLM(), drive_root=tmp_path)._run_slot(
        _agent_request(), _agent_slot(), pending_invocation_checkpoint=checkpoint,
    )
    gateway = fake_route.instances[-1]
    if refused:
        assert gateway.start_requests == [] and actor.status == "error"
        assert actor.failure_code == "review_custody_checkpoint_unwritable"
        assert actor.usage["review_failure_phase"] == "authority"
        assert actor.transport_status == "authority_error"
        assert '"reason":"contention"' in actor.reported_cause
        envelope = parse_model_response(actor.model, asdict(actor), {})
        durable = parse_model_review_results({"results": [envelope]}).actor_records[0]
        assert durable.reported_cause == actor.reported_cause
        assert durable.transport_status == "authority_error"
        assert actor.response_ref, "the physical failure keeps its durable source"
    else:
        assert len(gateway.start_requests) == 1 and actor.status == "ok"
        assert actor.raw_text == "[]" and not actor.reported_cause
