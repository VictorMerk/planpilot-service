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
    # Ordered fasb literals ('atom' / '~atom') currently activated. Needed to
    # undo arbitrary selections: fasb's '-' only pops the latest activation.
    active_selections: List[str] = field(default_factory=list)
    # Length of the Fast Downward plan the session was built from.
    min_horizon: int = None
    # Incremented on every applied selection change (optimistic concurrency).
    selection_revision: int = 0
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
            "hasPlan": self.min_horizon is not None,
            "minimumHorizon": self.min_horizon,
            "selectionRevision": self.selection_revision,
            "solutionCount": self.solution_count(),
            "solution": self.current_solution(),
        }

    # Shared response fields the IPEXCO backend expects on every reply.
    def envelope(self):
        return {
            "sessionId": self.session_id,
            "expiresAt": to_iso(self.expires_at),
            "selectionRevision": self.selection_revision,
            "solutionCount": self.solution_count(),
        }

    def solution_count(self):
        count = normalize_count(self.service.send_command("#!"))
        return count if count > 0 else None

    # A representative plan consistent with the current selections.
    def current_solution(self):
        solutions = normalize_solutions(self.service.send_command("! 1"))
        return solutions[0] if solutions else None

    def list_facets(self):
        self.facets = normalize_facets(self.service.send_command("?"))
        return self.facets

    def select_facet(self, facet_id: str, selection_state: str, previous_state=None):
        self._apply_selection(facet_id, selection_state, previous_state)
        self.selection_revision += 1
        self.facets = self.list_facets()
        return self.facets

    def apply_selections(self, selections):
        for selection in selections:
            self._apply_selection(
                selection["facetId"],
                selection["selectionState"],
                selection.get("previousSelectionState"),
            )
        self.selection_revision += 1
        self.facets = self.list_facets()
        return self.facets

    def _apply_selection(self, facet_id: str, selection_state: str, previous_state=None):
        if selection_state not in ("positive", "negative", "neutral"):
            raise ValueError("Unsupported facet selection state.")

        previous_literal = build_selection_literal(facet_id, previous_state)
        if previous_literal in self.active_selections:
            # fasb's '-' only pops the most recent activation, so undoing an
            # older selection means clearing the route and replaying the rest.
            self.active_selections.remove(previous_literal)
            self.service.send_command("--", no_Output=True)
            for literal in self.active_selections:
                self.service.send_command(f"+ {literal}", no_Output=True)

        new_literal = build_selection_literal(facet_id, selection_state)
        if new_literal and new_literal not in self.active_selections:
            self.service.send_command(f"+ {new_literal}", no_Output=True)
            self.active_selections.append(new_literal)

    def query(self, query_type: str, solution_number=None, facet_id=None):
        if query_type == "selectionImpact":
            return self._selection_impact(facet_id)
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
        if query_type == "impliedFacets":
            # '|= %' returns the facets entailed by the current decisions, i.e.
            # the landmarks that hold in every remaining solution. The IPEXCO
            # backend requires implied facets to be neutral, read-only markers.
            facets = normalize_facets(self.service.send_command("|= %"))
            for facet in facets:
                facet["facetType"] = "implied"
                facet["selectable"] = False
                facet["selectionState"] = "neutral"
            return {"type": query_type, "facets": facets}
        raise ValueError("Unsupported PlanPilot query type.")

    # How enforcing (require) or forbidding one facet would change the plan
    # set, computed from fasb's '#!!' (answer set counts under each facet).
    def _selection_impact(self, facet_id):
        if not facet_id:
            raise ValueError("facetId is required for selectionImpact queries.")

        reduction_facets = normalize_facets(self.service.send_command("#!!"))
        match = next(
            (facet for facet in reduction_facets if facet["id"] == facet_id), None
        )

        def direction(sign):
            remaining = ((match or {}).get("remaining") or {}).get("solution", {})
            reduction = ((match or {}).get("reduction") or {}).get("solution", {})
            if remaining.get(sign) is None:
                return {
                    "available": False,
                    "plansRemaining": None,
                    "planReduction": None,
                }
            return {
                "available": True,
                "plansRemaining": int(remaining[sign]),
                "planReduction": reduction.get(sign),
            }

        return {
            "type": "selectionImpact",
            "facetId": facet_id,
            "exact": True,
            "comparableToCurrent": True,
            "totalPlans": self.solution_count(),
            "require": direction("positive"),
            "forbid": direction("negative"),
        }

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
            min_horizon=artifacts.get("horizon") or None,
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
        # The IPEXCO backend requires abstractTimeStep to mirror a null timestep.
        "abstractTimeStep": not timestep,
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


def build_selection_literal(facet_id: str, selection_state):
    if selection_state == "positive":
        return facet_id
    if selection_state == "negative":
        return f"~{facet_id}"
    return None


def build_solution_command(solution_number):
    if solution_number is None:
        return "!"
    if type(solution_number) is int and solution_number > 0:
        return f"! {solution_number}"
    raise ValueError("solutionNumber must be a positive integer.")


def normalize_solutions(solutions):
    return [
        chain_solution_facets(
            {
                "label": solution.get("label", ""),
                "facets": normalize_facets(solution.get("facets", [])),
            }
        )
        for solution in solutions
    ]


# The IPEXCO backend expects solution facets ordered by timestep and chained:
# every facet after the first references its predecessor via parentId.
def chain_solution_facets(solution):
    solution["facets"] = sorted(
        solution["facets"], key=lambda facet: facet["timestep"] or 0
    )
    previous_id = None
    for facet in solution["facets"]:
        if previous_id is not None:
            facet["parentId"] = previous_id
        previous_id = facet["id"]
    return solution


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
