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
            return {"type": query_type, "value": self.service.send_command("#?")}
        if query_type == "facetReduction":
            return {
                "type": query_type,
                "facets": normalize_facets(self.service.send_command("#??")),
            }
        if query_type == "solutionCount":
            return {"type": query_type, "value": self.service.send_command("#!")}
        if query_type == "solutionReduction":
            return {
                "type": query_type,
                "facets": normalize_facets(self.service.send_command("#!!")),
            }
        if query_type == "solution":
            command = build_solution_command(solution_number)
            return {"type": query_type, "solutions": normalize_solutions(self.service.send_command(command))}
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


session_registry = SessionRegistry()
