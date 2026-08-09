from dataclasses import dataclass

import pytest

from app import create_app
from app.rest import sessions as session_routes
from app.service.session_registry import (
    SessionCapacityError,
    SessionConfiguration,
    SessionExpiredError,
    SessionNotFoundError,
    SessionRevisionConflictError,
)


AUTH = {"Authorization": "Bearer test-service-key"}


@dataclass
class FakeSession:
    session_id: str = "pp_sess_test"
    selection_revision: int = 0

    def to_response(self):
        return {
            "sessionId": self.session_id,
            "status": "ready",
            "hasPlan": True,
            "solution": {"facets": []},
            "solutionCount": None,
            "minimumHorizon": 4,
            "selectionRevision": self.selection_revision,
            "cachedSolutions": 0,
            "configuration": {
                "horizon": 8,
                "encoding": "bounded",
                "abstractTimeSteps": False,
                "stateFacets": False,
            },
            "createdAt": "2026-08-09T10:00:00Z",
            "lastAccessAt": "2026-08-09T10:00:00Z",
            "expiresAt": "2026-08-09T11:00:00Z",
        }

    @property
    def solution_count(self):
        return None

    @property
    def facets(self):
        return []

    def select_facet(
        self,
        facet_id,
        selection_state,
        previous_selection_state=None,
        expected_selection_revision=None,
    ):
        del facet_id, selection_state, previous_selection_state
        if expected_selection_revision != self.selection_revision:
            raise SessionRevisionConflictError(
                expected_selection_revision,
                self.selection_revision,
            )
        self.selection_revision += 1
        return self.to_response() | {"facets": []}

    def apply_selections(self, selections, expected_selection_revision=None):
        del selections
        if expected_selection_revision != self.selection_revision:
            raise SessionRevisionConflictError(
                expected_selection_revision,
                self.selection_revision,
            )
        self.selection_revision += 1
        return self.to_response() | {"facets": []}


class FakeRegistry:
    def __init__(self):
        self.session = FakeSession()
        self.create_error = None
        self.get_error = None

    def capacity_snapshot(self):
        return {
            "activeSessions": 0,
            "maxActiveSessions": 4,
            "activeCreations": 0,
            "maxConcurrentCreations": 1,
            "acceptingNewSessions": True,
        }

    def create_session(
        self,
        domain_pddl,
        problem_pddl,
        configuration: SessionConfiguration,
        representative_plan=None,
    ):
        del domain_pddl, problem_pddl, representative_plan
        assert configuration.horizon == 8
        if self.create_error:
            raise self.create_error
        return self.session

    def get_session(self, session_id):
        if self.get_error:
            raise self.get_error
        if session_id != self.session.session_id:
            raise SessionNotFoundError(session_id)
        return self.session

    def stop_session(self, session_id):
        if session_id != self.session.session_id:
            raise SessionNotFoundError(session_id)
        return self.session


@pytest.fixture()
def registry(monkeypatch):
    fake = FakeRegistry()
    monkeypatch.setattr(session_routes, "session_registry", fake)
    return fake


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEY", "test-service-key")
    monkeypatch.setenv(
        "PLANPILOT_DATABASE_URL",
        f"sqlite:///{tmp_path / 'planpilot-test.db'}",
    )
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


def valid_session_request():
    return {
        "task": {
            "domainPddl": "(define (domain demo))",
            "problemPddl": "(define (problem demo))",
        },
        "configuration": {
            "horizon": 8,
            "encoding": "bounded",
            "abstractTimeSteps": False,
            "stateFacets": False,
        },
        "source": {"system": "IPEXCO"},
    }


def test_capabilities_require_the_service_key(client, registry):
    del registry
    response = client.get("/api/capabilities")
    assert response.status_code == 401
    assert response.json["error"]["code"] == "UNAUTHORIZED"


def test_invalid_session_requests_do_not_start_the_solver(client, registry):
    response = client.post(
        "/api/sessions",
        headers=AUTH,
        json={"configuration": {"horizon": 0}},
    )
    assert response.status_code == 400
    assert response.json["error"]["code"] == "INVALID_REQUEST"


def test_create_and_stop_session(client, registry):
    created = client.post(
        "/api/sessions",
        headers=AUTH,
        json=valid_session_request(),
    )
    assert created.status_code == 201
    assert created.json["sessionId"] == registry.session.session_id

    stopped = client.delete(
        f"/api/sessions/{registry.session.session_id}",
        headers=AUTH,
    )
    assert stopped.status_code == 200
    assert stopped.json == {
        "sessionId": registry.session.session_id,
        "status": "stopped",
    }


def test_capacity_errors_have_a_retry_hint(client, registry):
    registry.create_error = SessionCapacityError(
        "PLANPILOT_SESSION_LIMIT",
        "All test slots are occupied.",
    )
    response = client.post(
        "/api/sessions",
        headers=AUTH,
        json=valid_session_request(),
    )
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.json["error"]["code"] == "PLANPILOT_SESSION_LIMIT"


def test_expired_sessions_are_distinct_from_missing_sessions(client, registry):
    registry.get_error = SessionExpiredError(registry.session.session_id)
    expired = client.get(
        f"/api/sessions/{registry.session.session_id}",
        headers=AUTH,
    )
    assert expired.status_code == 410
    assert expired.json["error"]["code"] == "SESSION_EXPIRED"

    registry.get_error = SessionNotFoundError("missing")
    missing = client.get("/api/sessions/missing", headers=AUTH)
    assert missing.status_code == 404
    assert missing.json["error"]["code"] == "SESSION_NOT_FOUND"


def test_select_and_apply_reject_stale_revisions(client, registry):
    selected = client.post(
        f"/api/sessions/{registry.session.session_id}/facets/select",
        headers=AUTH,
        json={
            "facetId": "facet-a",
            "selectionState": "positive",
            "expectedSelectionRevision": 0,
        },
    )
    assert selected.status_code == 200
    assert selected.json["selectionRevision"] == 1

    stale = client.post(
        f"/api/sessions/{registry.session.session_id}/facets/apply",
        headers=AUTH,
        json={
            "selections": [
                {"facetId": "facet-b", "selectionState": "negative"}
            ],
            "expectedSelectionRevision": 0,
        },
    )
    assert stale.status_code == 409
    assert stale.json["error"]["code"] == "SELECTION_CONFLICT"
    assert registry.session.selection_revision == 1
