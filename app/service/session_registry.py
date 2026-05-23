from dataclasses import dataclass
from io import BytesIO
from threading import RLock
from typing import Dict, List
from uuid import uuid4

from .fastdownward_service import run_fastdownward_service
from .planpilot_service import PlanpilotService


SUPPORTED_ENCODINGS = {"exact", "bounded"}


class SessionNotFoundError(KeyError):
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

    def list_facets(self):
        self.facets = normalize_facets(self.service.send_command("?"))
        return self.facets

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
            self._sessions[session.session_id] = session

        return session

    def get_session(self, session_id: str):
        with self._lock:
            session = self._sessions.get(session_id)

        if session is None:
            raise SessionNotFoundError(session_id)

        return session

    def stop_session(self, session_id: str):
        with self._lock:
            session = self._sessions.pop(session_id, None)

        if session is None:
            raise SessionNotFoundError(session_id)

        session.stop()
        return session


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

    return {
        "id": facet["id"],
        "label": label or facet["id"],
        "timestep": timestep if timestep else None,
        "selectionState": normalize_selection_state(facet.get("selectionState")),
    }


def normalize_selection_state(selection_state):
    if selection_state == "+":
        return "positive"
    if selection_state == "-":
        return "negative"
    return "neutral"


session_registry = SessionRegistry()
