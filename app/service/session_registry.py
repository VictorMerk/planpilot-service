import atexit
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from io import BytesIO
from threading import BoundedSemaphore, Event, RLock, Thread
from typing import Dict, List, Optional
from uuid import uuid4

from .fastdownward_service import run_fastdownward_service
from .planpilot_service import (
    PlanpilotCapacityError,
    PlanpilotNoPlanError,
    PlanpilotService,
)
SUPPORTED_ENCODINGS = {"exact", "bounded"}


class SessionNotFoundError(KeyError):
    pass


class SessionExpiredError(KeyError):
    pass


class SessionSelectionConflictError(RuntimeError):
    def __init__(self, facet_id, expected_state, actual_state):
        super().__init__(
            f"Facet '{facet_id}' was expected to be {expected_state}, "
            f"but is currently {actual_state}. Refresh the session and retry."
        )


class SessionCapacityError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


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
    facet_timesteps: Dict[str, Optional[int]]
    solution: Optional[Dict] = None
    baseline_solution: Optional[Dict] = None
    solution_cache: Dict[int, Dict] = field(default_factory=dict)
    minimum_horizon: Optional[int] = None
    applied_selections: Dict[str, str] = field(default_factory=dict)
    facet_catalog: Dict[str, Dict] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: utc_now())
    last_access_at: datetime = field(default_factory=lambda: utc_now())
    expires_at: datetime = field(default_factory=lambda: utc_now() + session_ttl())
    operation_lock: RLock = field(default_factory=RLock, repr=False)

    def touch(self):
        self.last_access_at = utc_now()
        self.expires_at = self.last_access_at + session_ttl()

    def is_expired(self):
        return utc_now() >= self.expires_at

    def to_response(self):
        solution = normalize_solution(self.solution) if self.solution else None
        return {
            "sessionId": self.session_id,
            "status": "ready",
            "hasPlan": solution is not None and bool(solution["facets"]),
            "solution": solution,
            "minimumHorizon": self.minimum_horizon,
            "configuration": self.configuration.to_response(),
            "createdAt": to_iso(self.created_at),
            "lastAccessAt": to_iso(self.last_access_at),
            "expiresAt": to_iso(self.expires_at),
        }

    def list_facets(self):
        with self.operation_lock:
            try:
                open_facets = self._read_current_facets()
                self.facets = self._compose_facets(open_facets, self.solution)
                return self.facets
            except PlanpilotCapacityError:
                self._restore_solver_process()
                raise

    def select_facet(self, facet_id: str, selection_state: str, previous_state=None):
        selection = {"facetId": facet_id, "selectionState": selection_state}
        if previous_state is not None:
            selection["previousSelectionState"] = previous_state
        return self.apply_selections([selection])

    def apply_selections(self, selections):
        with self.operation_lock:
            for selection in selections:
                facet_id = selection["facetId"]
                if facet_id not in self.facet_timesteps:
                    raise ValueError(f"Facet '{facet_id}' is not part of this session.")
            self._validate_previous_selection_states(selections)
            next_selections = dict(self.applied_selections)
            for selection in selections:
                facet_id = selection["facetId"]
                state = selection["selectionState"]
                if state == "neutral":
                    next_selections.pop(facet_id, None)
                else:
                    if state == "positive":
                        timestep = self.facet_timesteps[facet_id]
                        if timestep is not None:
                            for active_id, active_state in list(next_selections.items()):
                                if (
                                    active_id != facet_id
                                    and active_state == "positive"
                                    and self.facet_timesteps[active_id] == timestep
                                ):
                                    next_selections.pop(active_id)
                    next_selections[facet_id] = state

            previous_selections = dict(self.applied_selections)
            try:
                solution = self._rebuild_with_selections(next_selections)
                self.applied_selections = next_selections
            except Exception:
                self.solution = self._rebuild_with_selections(previous_selections)
                raise

            self.solution = solution
            self.solution_cache = {}
            try:
                self.facets = self._compose_facets(
                    self._read_current_facets(),
                    self.solution,
                )
            except PlanpilotCapacityError:
                self._restore_solver_process()
                raise
            return self.facets

    def _validate_previous_selection_states(self, selections):
        for selection in selections:
            expected_state = selection.get("previousSelectionState")
            if expected_state is None:
                continue
            facet_id = selection["facetId"]
            actual_state = self.applied_selections.get(facet_id, "neutral")
            if expected_state != actual_state:
                raise SessionSelectionConflictError(
                    facet_id, expected_state, actual_state
                )

    def _read_current_facets(self):
        facets = list(self.service.send_command("?") or [])
        if not self.configuration.abstract_time_steps:
            return facets

        known_ids = {facet.get("id") for facet in facets}
        for implied in self.service.send_command("|= %") or []:
            if implied.get("id") in known_ids or not is_abstract_facet(implied):
                continue
            implied_facet = dict(implied)
            implied_facet["selectionState"] = "+"
            implied_facet["_facetType"] = "implied"
            facets.append(implied_facet)
        return facets

    def _compose_facets(self, open_facets, solution):
        """Merge current alternatives with the displayed solution.

        Plan actions are shown in the graph but are not FASB constraints.
        """
        by_id = {}
        for raw_facet in open_facets or []:
            normalized = normalize_facet(
                raw_facet,
                facet_type=raw_facet.get("_facetType", "optional"),
            )
            normalized["selectable"] = raw_facet.get("_facetType") != "implied"
            by_id[normalized["id"]] = normalized
            self._remember_raw_facet(raw_facet)

        previous_plan_id = None
        solution_facets = (solution or {}).get("facets", [])
        for raw_facet in sorted(
            solution_facets,
            key=lambda facet: (facet.get("timestep") or 0, facet.get("id", "")),
        ):
            was_selectable = by_id.get(raw_facet.get("id"), {}).get(
                "selectable", False
            )
            normalized = normalize_facet(
                raw_facet,
                facet_type="plan",
                parent_id=previous_plan_id,
            )
            normalized["selectable"] = was_selectable
            by_id[normalized["id"]] = normalized
            self._remember_raw_facet(raw_facet)
            previous_plan_id = normalized["id"]

        for facet_id, selection_state in self.applied_selections.items():
            raw_facet = self.facet_catalog.get(facet_id)
            if raw_facet is None:
                continue
            selected = by_id.get(facet_id) or normalize_facet(
                raw_facet,
                facet_type="selected",
            )
            selected["selectionState"] = selection_state
            selected["selectable"] = True
            # Preserve explicit includes after they enter the displayed solution.
            selected["facetType"] = "selected"
            if selection_state == "negative":
                selected.pop("parentId", None)
            by_id[facet_id] = selected

        facets = sorted(
            by_id.values(),
            key=lambda facet: (
                facet.get("timestep") is None,
                facet.get("timestep") or 0,
                facet.get("facetType") != "plan",
                facet.get("label", ""),
            ),
        )
        self._remember_facet_timesteps(facets)
        return facets

    def _remember_raw_facet(self, facet):
        facet_id = facet.get("id")
        if facet_id:
            self.facet_catalog[facet_id] = dict(facet)

    def _remember_facet_timesteps(self, facets):
        for facet in facets:
            timestep = facet.get("timestep")
            if facet.get("id") and timestep is not None:
                self.facet_timesteps[facet["id"]] = timestep

    def _rebuild_with_selections(self, selections):
        self.service.restart_FASB()
        for facet_id, selection_state in sorted(selections.items()):
            command = build_selection_command(facet_id, selection_state)
            self.service.send_command(command, no_Output=True)

        if normalize_count(self.service.send_command("#!")) == 0:
            raise PlanpilotNoPlanError(
                "The facet selection leaves no non-empty plan."
            )

        if not selections and self.baseline_solution:
            return self.baseline_solution
        return self.service.get_representative_solution(required=True)

    def _restore_solver_process(self):
        """Restart FASB and restore active selections after a timeout."""
        try:
            self.service.restart_FASB()
            for facet_id, selection_state in sorted(self.applied_selections.items()):
                command = build_selection_command(facet_id, selection_state)
                self.service.send_command(command, no_Output=True)
        except Exception:
            self.service.stop_fasb()

    def query(self, query_type: str, solution_number=None):
        with self.operation_lock:
            if query_type == "facets":
                return {"type": query_type, "facets": self.list_facets()}
            try:
                if query_type == "facetCount":
                    return {
                        "type": query_type,
                        "value": normalize_count(self.service.send_command("#?")),
                    }
                if query_type == "facetReduction":
                    if normalize_count(self.service.send_command("#?")) == 0:
                        return {"type": query_type, "facets": []}
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
                    if normalize_count(self.service.send_command("#?")) == 0:
                        return {"type": query_type, "facets": []}
                    return {
                        "type": query_type,
                        "facets": normalize_facets(self.service.send_command("#!!")),
                    }
                if query_type == "solution":
                    # Solution 1 is the graph snapshot created with the session.
                    if solution_number == 1 and self.solution:
                        return {
                            "type": query_type,
                            "solutions": [normalize_solution(self.solution)],
                        }
                    if solution_number is not None and solution_number in self.solution_cache:
                        return {
                            "type": query_type,
                            "solutions": [self.solution_cache[solution_number]],
                        }
                    command = build_solution_command(solution_number)
                    solutions = normalize_solutions(self.service.send_command(command))
                    if solution_number is not None:
                        expected_label = f"solution {solution_number}"
                        requested = next(
                            (solution for solution in solutions if solution["label"] == expected_label),
                            None,
                        )
                        solutions = [requested] if requested else []
                        if requested:
                            self.solution_cache[solution_number] = requested
                    return {
                        "type": query_type,
                        "solutions": solutions,
                    }
                raise ValueError("Unsupported PlanPilot query type.")
            except PlanpilotCapacityError:
                self._restore_solver_process()
                raise

    def stop(self):
        with self.operation_lock:
            self.service.stop_fasb()


class SessionRegistry:
    def __init__(
        self,
        cleanup_interval_seconds=30,
        max_concurrent_creations=None,
        max_active_sessions=None,
    ):
        self._sessions: Dict[str, SessionContext] = {}
        self._lock = RLock()
        self._max_concurrent_creations = max_concurrent_creations or env_positive_int(
            "PLANPILOT_MAX_CONCURRENT_CREATIONS", 1
        )
        self._max_active_sessions = max_active_sessions or env_positive_int(
            "PLANPILOT_MAX_ACTIVE_SESSIONS", 4
        )
        self._creation_slots = BoundedSemaphore(self._max_concurrent_creations)
        self._active_creations = 0
        self._cleanup_interval_seconds = max(cleanup_interval_seconds, 1)
        self._shutdown_event = Event()
        self._cleanup_thread = Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()

    def create_session(
        self,
        domain_pddl: str,
        problem_pddl: str,
        configuration: SessionConfiguration,
        representative_plan=None,
    ):
        if not self._creation_slots.acquire(blocking=False):
            raise SessionCapacityError(
                "PLANPILOT_BUSY",
                "PlanPilot is already preparing another plan space. Retry after the current preparation finishes.",
            )
        try:
            with self._lock:
                self._active_creations += 1
            self._ensure_active_capacity()
            artifacts = run_fastdownward_service(
                BytesIO(domain_pddl.encode("utf-8")),
                BytesIO(problem_pddl.encode("utf-8")),
                representative_plan=representative_plan,
            )

            service = PlanpilotService()
            try:
                initial_facets = service.run_planpilot_service(
                    artifacts["sasFile"],
                    configuration.horizon,
                    configuration.encoding,
                    configuration.abstract_time_steps,
                )
                solution_count = normalize_count(service.send_command("#!"))
                if solution_count == 0:
                    raise PlanpilotNoPlanError(
                        "PlanPilot found no non-empty plan for the requested horizon and encoding."
                    )
                solver_solution = service.get_representative_solution(required=True)
                initial_solution = (
                    solution_from_representative_plan(representative_plan)
                    if representative_plan
                    else solver_solution
                )
                if configuration.abstract_time_steps:
                    known_ids = {facet.get("id") for facet in initial_facets}
                    for implied in service.send_command("|= %") or []:
                        if implied.get("id") in known_ids or not is_abstract_facet(implied):
                            continue
                        implied_facet = dict(implied)
                        implied_facet["selectionState"] = "+"
                        implied_facet["_facetType"] = "implied"
                        initial_facets.append(implied_facet)
            except Exception:
                service.stop_fasb()
                raise

            session = SessionContext(
                session_id=f"pp_sess_{uuid4().hex}",
                configuration=configuration,
                service=service,
                facets=[],
                facet_timesteps={
                    facet["id"]: (
                        None if is_abstract_facet(facet) else facet.get("timestep")
                    )
                    for facet in initial_facets
                },
                solution=initial_solution,
                baseline_solution=initial_solution,
                minimum_horizon=artifacts["horizon"],
            )
            session.facets = session._compose_facets(initial_facets, initial_solution)
            try:
                self._register_session(session)
            except Exception:
                session.stop()
                raise

            return session
        finally:
            with self._lock:
                self._active_creations -= 1
            self._creation_slots.release()

    def _ensure_active_capacity(self):
        with self._lock:
            self._cleanup_expired_locked()
            if len(self._sessions) >= self._max_active_sessions:
                raise self._session_limit_error()

    def _register_session(self, session):
        """Register a session if capacity is still available."""
        with self._lock:
            self._cleanup_expired_locked()
            if len(self._sessions) >= self._max_active_sessions:
                raise self._session_limit_error()
            self._sessions[session.session_id] = session

    def _session_limit_error(self):
        return SessionCapacityError(
            "PLANPILOT_SESSION_LIMIT",
            f"PlanPilot already has {self._max_active_sessions} active sessions. Stop an existing session or wait for it to expire before retrying.",
        )

    def capacity_snapshot(self):
        with self._lock:
            self._cleanup_expired_locked()
            active_sessions = len(self._sessions)
            active_creations = self._active_creations
            return {
                "activeSessions": active_sessions,
                "maxActiveSessions": self._max_active_sessions,
                "activeCreations": active_creations,
                "maxConcurrentCreations": self._max_concurrent_creations,
                "acceptingNewSessions": (
                    active_sessions < self._max_active_sessions
                    and active_creations < self._max_concurrent_creations
                ),
            }

    def get_session(self, session_id: str):
        with self._lock:
            self._cleanup_expired_locked(exclude_session_id=session_id)
            session = self._sessions.get(session_id)
            if session and session.is_expired():
                self._sessions.pop(session_id, None)
                session.stop()
                raise SessionExpiredError(session_id)
            if session is not None:
                session.touch()

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

    def cleanup_expired(self):
        with self._lock:
            self._cleanup_expired_locked()

    def shutdown(self):
        self._shutdown_event.set()
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.stop()

    def _cleanup_loop(self):
        while not self._shutdown_event.wait(self._cleanup_interval_seconds):
            self.cleanup_expired()

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


def normalize_facet(
    facet,
    facet_type=None,
    parent_id=None,
):
    action_name = facet.get("action", "")
    arguments = facet.get("arguments")
    if arguments is None:
        arguments = [
            constant
            for constant in [facet.get("constant1"), facet.get("constant2")]
            if constant
        ]
    label = " ".join([action_name, *arguments]).strip()
    abstract_time_step = is_abstract_facet(facet)
    facet_timestep = None if abstract_time_step else facet.get("timestep")

    normalized = {
        "id": facet["id"],
        "label": label or facet["id"],
        "timestep": facet_timestep if facet_timestep else None,
        "selectionState": normalize_selection_state(facet.get("selectionState")),
        "action": {"name": action_name, "arguments": list(arguments)},
    }

    if facet_type:
        normalized["facetType"] = facet_type
    if abstract_time_step:
        normalized["abstractTimeStep"] = True
    if parent_id:
        normalized["parentId"] = parent_id
    if facet.get("reduction") is not None:
        normalized["reduction"] = facet["reduction"]
    if facet.get("remaining") is not None:
        normalized["remaining"] = facet["remaining"]

    return normalized


def is_abstract_facet(facet):
    return facet.get("id", "").startswith("occurs_sometime(")


def normalize_count(value):
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        raise ValueError("FASB returned no numeric count.")
    for line in reversed(value.splitlines()):
        stripped = line.strip()
        while stripped.startswith("::"):
            stripped = stripped[2:].strip()
        if stripped.isdecimal():
            return int(stripped)
    if not value.strip():
        raise ValueError("FASB returned no numeric count.")
    raise ValueError(f"FASB returned an invalid count: {value!r}")


def normalize_selection_state(selection_state):
    if selection_state == "+":
        return "positive"
    if selection_state == "-":
        return "negative"
    return "neutral"


def build_selection_command(facet_id: str, selection_state: str):
    if selection_state == "positive":
        return f"+ {facet_id}"
    if selection_state == "negative":
        return f"+ ~{facet_id}"
    raise ValueError("Unsupported facet selection state.")


def build_solution_command(solution_number):
    if solution_number is None:
        return "!"
    if type(solution_number) is int and solution_number > 0:
        return f"! {solution_number}"
    raise ValueError("solutionNumber must be a positive integer.")


def normalize_solutions(solutions):
    return [normalize_solution(solution) for solution in solutions]


def solution_from_representative_plan(actions):
    facets = []
    for timestep, action in enumerate(actions, start=1):
        name = action["name"]
        arguments = list(action.get("params", []))
        atom_arguments = ",".join(f'"{token}"' for token in [name, *arguments])
        facets.append(
            {
                "id": f"occurs(action(({atom_arguments})),{timestep})",
                "action": name,
                "arguments": arguments,
                "timestep": timestep,
                "selectionState": "Not selected",
            }
        )
    return {"label": "solution 1", "facets": facets}


def normalize_solution(solution):
    raw_facets = [
        facet
        for facet in solution.get("facets", [])
        if not is_abstract_facet(facet)
    ]
    raw_facets.sort(
        key=lambda facet: (
            facet.get("timestep") is None,
            facet.get("timestep") or 0,
            facet.get("id", ""),
        )
    )

    facets = []
    previous_facet_id = None
    for raw_facet in raw_facets:
        normalized = normalize_facet(
            raw_facet,
            facet_type="plan",
            parent_id=previous_facet_id,
        )
        facets.append(normalized)
        previous_facet_id = normalized["id"]

    return {
        "label": solution.get("label", ""),
        "facets": facets,
    }


def session_ttl():
    raw_value = os.environ.get("PLANPILOT_SESSION_TTL_SECONDS", "3600")
    try:
        seconds = int(raw_value)
    except ValueError:
        seconds = 3600
    return timedelta(seconds=max(seconds, 1))


def env_positive_int(name, default):
    raw_value = os.environ.get(name, str(default))
    try:
        value = int(raw_value)
    except ValueError:
        value = default
    return max(value, 1)


def utc_now():
    return datetime.now(timezone.utc)


def to_iso(value):
    return value.isoformat().replace("+00:00", "Z")


session_registry = SessionRegistry()
atexit.register(session_registry.shutdown)
