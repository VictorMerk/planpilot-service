from datetime import datetime, timedelta, timezone

import pytest

from app.service.session_registry import (
    SessionCapacityError,
    SessionConfiguration,
    SessionContext,
    SessionExpiredError,
    SessionRegistry,
    SessionRevisionConflictError,
    SessionSelectionConflictError,
)


FACET_A = {
    "id": 'occurs(action(("unstack","a","b")),1)',
    "label": "unstack a b",
    "timestep": 1,
    "selectionState": "neutral",
}
FACET_B = {
    "id": 'occurs(action(("put-down","a")),1)',
    "label": "put down a",
    "timestep": 1,
    "selectionState": "neutral",
}


class FakeSolver:
    def __init__(self):
        self.commands = []
        self.stopped = False

    def restart_FASB(self, **kwargs):
        del kwargs
        self.commands.append("restart")

    def send_command(self, command, **kwargs):
        del kwargs
        self.commands.append(command)
        if command == "?":
            return [FACET_A, FACET_B]
        return None

    def get_representative_solution(self, **kwargs):
        del kwargs
        return {"facets": [FACET_A]}

    def stop_fasb(self):
        self.stopped = True


def make_session(session_id="pp_sess_test"):
    solver = FakeSolver()
    session = SessionContext(
        session_id=session_id,
        configuration=SessionConfiguration(8, "bounded", False, False),
        service=solver,
        facets=[FACET_A, FACET_B],
        facet_timesteps={FACET_A["id"]: 1, FACET_B["id"]: 1},
        solution={"facets": [FACET_A]},
        baseline_solution={"facets": [FACET_A]},
        minimum_horizon=4,
    )
    session.facets = session._compose_facets([FACET_A, FACET_B], session.solution)
    return session, solver


def test_apply_is_atomic_and_increments_the_revision():
    session, solver = make_session()

    result = session.apply_selections(
        [{"facetId": FACET_A["id"], "selectionState": "positive"}],
        expected_revision=0,
    )

    assert result["selectionRevision"] == 1
    assert session.applied_selections == {FACET_A["id"]: "positive"}
    assert "restart" in solver.commands

    with pytest.raises(SessionRevisionConflictError):
        session.apply_selections(
            [{"facetId": FACET_B["id"], "selectionState": "negative"}],
            expected_revision=0,
        )
    assert session.applied_selections == {FACET_A["id"]: "positive"}
    assert session.selection_revision == 1


def test_previous_state_conflicts_do_not_change_the_session():
    session, _solver = make_session()
    with pytest.raises(SessionSelectionConflictError):
        session.select_facet(
            FACET_A["id"],
            "negative",
            previous_state="positive",
            expected_revision=0,
        )
    assert session.applied_selections == {}
    assert session.selection_revision == 0


def test_only_one_positive_action_can_occupy_a_fixed_timestep():
    session, _solver = make_session()
    session.apply_selections(
        [{"facetId": FACET_A["id"], "selectionState": "positive"}],
        expected_revision=0,
    )
    session.apply_selections(
        [{"facetId": FACET_B["id"], "selectionState": "positive"}],
        expected_revision=1,
    )
    assert session.applied_selections == {FACET_B["id"]: "positive"}


def test_neutral_clears_an_explicit_selection():
    session, _solver = make_session()
    session.apply_selections(
        [{"facetId": FACET_A["id"], "selectionState": "negative"}],
        expected_revision=0,
    )
    session.apply_selections(
        [{"facetId": FACET_A["id"], "selectionState": "neutral"}],
        expected_revision=1,
    )
    assert session.applied_selections == {}


def test_registry_enforces_capacity_and_expires_sessions():
    registry = SessionRegistry(
        cleanup_interval_seconds=3600,
        max_concurrent_creations=1,
        max_active_sessions=1,
    )
    first, first_solver = make_session("first")
    second, _second_solver = make_session("second")
    try:
        registry._register_session(first)
        with pytest.raises(SessionCapacityError) as error:
            registry._register_session(second)
        assert error.value.code == "PLANPILOT_SESSION_LIMIT"

        first.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        with pytest.raises(SessionExpiredError):
            registry.get_session("first")
        assert first_solver.stopped is True

        registry._register_session(second)
        assert registry.capacity_snapshot()["activeSessions"] == 1
    finally:
        registry.shutdown()
