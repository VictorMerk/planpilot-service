import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from io import BytesIO
from threading import RLock
from typing import Dict, List
from uuid import uuid4

from .fastdownward_service import run_fastdownward_service
from .planpilot_service import PlanpilotService


SUPPORTED_ENCODINGS = {"exact", "bounded"}


class SessionNotFoundError(KeyError):
    pass


class SessionExpiredError(KeyError):
    pass


@dataclass(frozen=True)
class SessionConfiguration:
    horizon: int
    encoding: str
    abstract_time_steps: bool

    def to_response(self):
        return {
            "horizon": self.horizon,
            "encoding": self.encoding,
            "abstractTimeSteps": self.abstract_time_steps,
        }


@dataclass
class SessionContext:
    session_id: str
    configuration: SessionConfiguration
    service: PlanpilotService
    facets: List[Dict]
    created_at: datetime = field(default_factory=lambda: utc_now())
    last_access_at: datetime = field(default_factory=lambda: utc_now())
    expires_at: datetime = field(default_factory=lambda: utc_now() + session_ttl())

    def touch(self):
        self.last_access_at = utc_now()
        self.expires_at = self.last_access_at + session_ttl()

    def is_expired(self):
        return utc_now() >= self.expires_at

    def to_response(self):
        return {
            "sessionId": self.session_id,
            "status": "ready",
            "configuration": self.configuration.to_response(),
            "createdAt": to_iso(self.created_at),
            "lastAccessAt": to_iso(self.last_access_at),
            "expiresAt": to_iso(self.expires_at),
        }

    def list_facets(self):
        self.facets = normalize_facets(self.service.send_command("?"))
        return self.facets

    def select_facet(self, facet_id: str, selection_state: str, previous_state=None):
        command = build_selection_command(facet_id, selection_state, previous_state)
        if command:
            self.service.send_command(command, no_Output=True)
        self.facets = self.list_facets()
        return self.facets

    def query(self, query_type: str, solution_number=None):
        if query_type == "facets":
            return {"type": query_type, "facets": self.list_facets()}
        if query_type == "facetCount":
            return {
                "type": query_type,
                "value": normalize_count(self.service.send_command("#?")),
            }
        if query_type == "facetReduction":
            return {
                "type": query_type,
                "facets": normalize_facets(self.service.send_command("#??")),
            }
        if query_type == "solutionCount":
            return {
                "type": query_type,
                "value": normalize_count(self.service.send_command("#!")),
            }
        if query_type == "solutionReduction":
            return {
                "type": query_type,
                "facets": normalize_facets(self.service.send_command("#!!")),
            }
        if query_type == "solution":
            command = build_solution_command(solution_number)
            return {
                "type": query_type,
                "solutions": normalize_solutions(self.service.send_command(command)),
            }
        raise ValueError("Unsupported PlanPilot query type.")

    def stop(self):
        self.service.stop_fasb()


class SessionRegistry:
    def __init__(self):
        self._sessions: Dict[str, SessionContext] = {}
        self._lock = RLock()

    def create_session(
        self,
        domain_pddl: str,
        problem_pddl: str,
        configuration: SessionConfiguration,
    ):
        artifacts = run_fastdownward_service(
            BytesIO(domain_pddl.encode("utf-8")),
            BytesIO(problem_pddl.encode("utf-8")),
        )

        service = PlanpilotService()
        try:
            facets = service.run_planpilot_service(
                artifacts["sasFile"],
                configuration.horizon,
                configuration.encoding,
                configuration.abstract_time_steps,
            )
        except Exception:
            service.stop_fasb()
            raise

        session = SessionContext(
            session_id=f"pp_sess_{uuid4().hex}",
            configuration=configuration,
            service=service,
            facets=normalize_facets(facets),
        )

        with self._lock:
            self._cleanup_expired_locked()
            self._sessions[session.session_id] = session

        return session

    def get_session(self, session_id: str):
        with self._lock:
            self._cleanup_expired_locked(exclude_session_id=session_id)
            session = self._sessions.get(session_id)
            if session and session.is_expired():
                self._sessions.pop(session_id, None)
                session.stop()
                raise SessionExpiredError(session_id)

        if session is None:
            raise SessionNotFoundError(session_id)

        session.touch()
        return session

    def stop_session(self, session_id: str):
        with self._lock:
            session = self._sessions.pop(session_id, None)

        if session is None:
            raise SessionNotFoundError(session_id)

        session.stop()
        return session

    def _cleanup_expired_locked(self, exclude_session_id=None):
        expired_ids = [
            session_id
            for session_id, session in self._sessions.items()
            if session_id != exclude_session_id and session.is_expired()
        ]
        for session_id in expired_ids:
            session = self._sessions.pop(session_id)
            session.stop()


def normalize_facets(facets):
    return [normalize_facet(facet) for facet in facets]


def normalize_facet(facet):
    constants = [
        constant
        for constant in [facet.get("constant1"), facet.get("constant2")]
        if constant
    ]
    label = " ".join([facet.get("action", ""), *constants]).strip()
    timestep = facet.get("timestep")

    normalized = {
        "id": facet["id"],
        "label": label or facet["id"],
        "timestep": timestep if timestep else None,
        "selectionState": normalize_selection_state(facet.get("selectionState")),
    }

    if facet.get("reduction") is not None:
        normalized["reduction"] = facet["reduction"]
    if facet.get("remaining") is not None:
        normalized["remaining"] = facet["remaining"]

    return normalized


def normalize_count(value):
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return 0
    stripped = value.strip()
    if stripped.startswith("::"):
        stripped = stripped[2:].strip()
    try:
        return int(stripped)
    except ValueError:
        return 0


def normalize_selection_state(selection_state):
    if selection_state == "+":
        return "positive"
    if selection_state == "-":
        return "negative"
    return "neutral"


def build_selection_command(facet_id: str, selection_state: str, previous_state=None):
    if selection_state == "positive":
        return f"+ {facet_id}"
    if selection_state == "negative":
        return f"+ ~{facet_id}"
    if selection_state == "neutral":
        if previous_state == "positive":
            return f"- {facet_id}"
        if previous_state == "negative":
            return f"- ~{facet_id}"
        return None
    raise ValueError("Unsupported facet selection state.")


def build_solution_command(solution_number):
    if solution_number is None:
        return "!"
    if type(solution_number) is int and solution_number > 0:
        return f"! {solution_number}"
    raise ValueError("solutionNumber must be a positive integer.")


def normalize_solutions(solutions):
    return [
        {
            "label": solution.get("label", ""),
            "facets": normalize_facets(solution.get("facets", [])),
        }
        for solution in solutions
    ]


def session_ttl():
    raw_value = os.environ.get("PLANPILOT_SESSION_TTL_SECONDS", "3600")
    try:
        seconds = int(raw_value)
    except ValueError:
        seconds = 3600
    return timedelta(seconds=max(seconds, 1))


def utc_now():
    return datetime.now(timezone.utc)


def to_iso(value):
    return value.isoformat().replace("+00:00", "Z")


session_registry = SessionRegistry()
