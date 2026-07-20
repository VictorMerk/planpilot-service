import atexit
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from io import BytesIO
from threading import BoundedSemaphore, Event, RLock, Thread
from time import monotonic
from typing import Dict, List, Optional
from uuid import uuid4

from .fastdownward_service import run_fastdownward_service
from .session_contract import (
    build_selection_command,
    build_solution_command,
    is_abstract_facet,
    normalize_count,
    normalize_facet,
    normalize_facets,
    normalize_implied_facets,
    normalize_solution,
    normalize_solutions,
)
from .planpilot_service import (
    PlanpilotCapacityError,
    PlanpilotNoPlanError,
    PlanpilotService,
    fasb_impact_timeout_seconds,
    planpilot_max_horizon,
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


class SessionRevisionConflictError(RuntimeError):
    def __init__(self, expected_revision, actual_revision):
        super().__init__(
            f"Session revision {expected_revision} is stale; "
            f"the current revision is {actual_revision}. Refresh and retry."
        )


class SessionCapacityError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class SessionHorizonError(ValueError):
    def __init__(self, minimum_horizon, maximum_horizon):
        super().__init__(
            f"The shortest plan has {minimum_horizon} actions, but PlanPilot supports at most {maximum_horizon}."
        )
        self.minimum_horizon = minimum_horizon
        self.maximum_horizon = maximum_horizon


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
    solution_count: Optional[int] = None
    solution_cache: Dict[int, Dict] = field(default_factory=dict)
    minimum_horizon: Optional[int] = None
    applied_selections: Dict[str, str] = field(default_factory=dict)
    facet_catalog: Dict[str, Dict] = field(default_factory=dict)
    selection_revision: int = 0
    solver_available: bool = True
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
            "solutionCount": self.solution_count,
            "minimumHorizon": self.minimum_horizon,
            "selectionRevision": self.selection_revision,
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

    def list_facets_response(self):
        with self.operation_lock:
            facets = self.list_facets()
            return {**self.to_response(), "facets": facets}

    def select_facet(
        self,
        facet_id: str,
        selection_state: str,
        previous_state=None,
        expected_revision=None,
    ):
        selection = {"facetId": facet_id, "selectionState": selection_state}
        if previous_state is not None:
            selection["previousSelectionState"] = previous_state
        return self.apply_selections([selection], expected_revision)

    def apply_selections(self, selections, expected_revision=None):
        with self.operation_lock:
            if (
                expected_revision is not None
                and expected_revision != self.selection_revision
            ):
                raise SessionRevisionConflictError(
                    expected_revision,
                    self.selection_revision,
                )
            for selection in selections:
                facet_id = selection["facetId"]
                if facet_id not in self.facet_timesteps:
                    raise ValueError(f"Facet '{facet_id}' is not part of this session.")
                visible_facet = next(
                    (facet for facet in self.facets if facet.get("id") == facet_id),
                    None,
                )
                if visible_facet is not None and visible_facet.get("selectable") is False:
                    raise ValueError(f"Facet '{facet_id}' is not selectable.")
            self._validate_previous_selection_states(selections)
            next_selections = self._updated_selections(
                self.applied_selections,
                selections,
            )

            previous_selections = dict(self.applied_selections)
            previous_solution = self.solution
            previous_solution_count = self.solution_count
            previous_facets = self.facets
            previous_solution_cache = dict(self.solution_cache)
            previous_facet_catalog = dict(self.facet_catalog)
            previous_facet_timesteps = dict(self.facet_timesteps)
            previous_selection_revision = self.selection_revision
            try:
                solution = self._rebuild_with_selections(next_selections)
                self.applied_selections = next_selections
                facets = self._compose_facets(
                    self._read_current_facets(),
                    solution,
                )
                self.solution = solution
                self.solution_count = None
                self.solution_cache = {}
                self.facets = facets
                self.selection_revision += 1
                response = {
                    **self.to_response(),
                    "solutionCount": None,
                    "facets": self.facets,
                }
            except Exception:
                self.applied_selections = previous_selections
                self.solution = previous_solution
                self.solution_count = previous_solution_count
                self.facets = previous_facets
                self.solution_cache = previous_solution_cache
                self.facet_catalog = previous_facet_catalog
                self.facet_timesteps = previous_facet_timesteps
                self.selection_revision = previous_selection_revision
                self._restore_solver_process()
                raise

            return response

    def _updated_selections(self, current, selections):
        updated = dict(current)
        for selection in selections:
            facet_id = selection["facetId"]
            state = selection["selectionState"]
            if state == "neutral":
                updated.pop(facet_id, None)
                continue
            if state == "positive":
                timestep = self.facet_timesteps[facet_id]
                if timestep is not None:
                    for active_id, active_state in list(updated.items()):
                        if (
                            active_id != facet_id
                            and active_state == "positive"
                            and self.facet_timesteps[active_id] == timestep
                        ):
                            updated.pop(active_id)
            updated[facet_id] = state
        return updated

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
            implied_facet["selectionState"] = "neutral"
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
            if facet.get("id"):
                self.facet_timesteps[facet["id"]] = facet.get("timestep")

    def _rebuild_with_selections(self, selections):
        self.service.restart_FASB()
        for facet_id, selection_state in sorted(selections.items()):
            command = build_selection_command(facet_id, selection_state)
            self.service.send_command(command, no_Output=True)

        if not selections and self.baseline_solution:
            return self.baseline_solution
        return self.service.get_representative_solution(required=True)

    def _restore_solver_process(self, timeout_seconds=None, raise_on_failure=False):
        """Restart FASB and restore active selections after a timeout."""
        try:
            deadline = (
                monotonic() + timeout_seconds
                if timeout_seconds is not None
                else None
            )
            self._restart_solver(deadline)
            for facet_id, selection_state in sorted(self.applied_selections.items()):
                command = build_selection_command(facet_id, selection_state)
                if deadline is None:
                    self.service.send_command(command, no_Output=True)
                else:
                    self.service.send_command(
                        command,
                        no_Output=True,
                        timeout_seconds=self._remaining_time(deadline),
                    )
            self.solver_available = True
        except Exception:
            self.solver_available = False
            self.service.stop_fasb()
            if raise_on_failure:
                raise

    def query(self, query_type: str, solution_number=None, facet_id=None):
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
                if query_type == "impliedFacets":
                    return {
                        "type": query_type,
                        "facets": normalize_implied_facets(
                            self.service.send_command("|= %")
                        ),
                    }
                if query_type == "solutionCount":
                    if self.solution_count is None:
                        count = normalize_count(self.service.send_command("#!"))
                        if count == 0 and self.solution:
                            raise PlanpilotNoPlanError(
                                "The solver count contradicts the current plan."
                            )
                        self.solution_count = count
                    return {
                        "type": query_type,
                        "value": self.solution_count,
                    }
                if query_type == "solutionReduction":
                    if normalize_count(self.service.send_command("#?")) == 0:
                        return {"type": query_type, "facets": []}
                    return {
                        "type": query_type,
                        "facets": normalize_facets(self.service.send_command("#!!")),
                    }
                if query_type == "selectionImpact":
                    return self._selection_impact(facet_id)
                if query_type == "solution":
                    if solution_number is not None and solution_number in self.solution_cache:
                        return {
                            "type": query_type,
                            "solutions": [self.solution_cache[solution_number]],
                        }
                    if (
                        solution_number is not None
                        and self.solution_count is not None
                        and solution_number > self.solution_count
                    ):
                        return {"type": query_type, "solutions": []}
                    command = build_solution_command(solution_number)
                    solutions = normalize_solutions(self.service.send_command(command))
                    if solution_number is not None:
                        if len(solutions) < solution_number and solutions:
                            self.solution_count = len(solutions)
                        for solution in solutions:
                            label = solution.get("label", "")
                            prefix = "solution "
                            if not label.startswith(prefix):
                                continue
                            try:
                                cached_number = int(label[len(prefix):])
                            except ValueError:
                                continue
                            if cached_number > 0:
                                self.solution_cache[cached_number] = solution
                        expected_label = f"solution {solution_number}"
                        requested = self.solution_cache.get(solution_number)
                        if requested is None:
                            requested = next(
                                (
                                    solution
                                    for solution in solutions
                                    if solution["label"] == expected_label
                                ),
                                None,
                            )
                        solutions = [requested] if requested else []
                    return {
                        "type": query_type,
                        "solutions": solutions,
                    }
                raise ValueError("Unsupported PlanPilot query type.")
            except PlanpilotCapacityError:
                if query_type != "selectionImpact":
                    self._restore_solver_process()
                raise

    def _selection_impact(self, facet_id):
        if facet_id not in self.facet_timesteps:
            raise ValueError(f"Facet '{facet_id}' is not part of this session.")
        visible = next(
            (facet for facet in self.facets if facet.get("id") == facet_id),
            None,
        )
        if visible is None or visible.get("selectable") is False:
            raise ValueError(f"Facet '{facet_id}' is not selectable.")

        require_route = self._updated_selections(
            self.applied_selections,
            [{"facetId": facet_id, "selectionState": "positive"}],
        )
        forbid_route = self._updated_selections(
            self.applied_selections,
            [{"facetId": facet_id, "selectionState": "negative"}],
        )
        competing_required = any(
            active_id != facet_id
            and state == "positive"
            and self.facet_timesteps.get(active_id) is not None
            and self.facet_timesteps.get(active_id) == self.facet_timesteps.get(facet_id)
            for active_id, state in self.applied_selections.items()
        )
        comparable = (
            facet_id not in self.applied_selections
            and not competing_required
        )

        restore_error = None
        try:
            require_count = self._count_route(require_route)
            forbid_count = self._count_route(forbid_route)
            total = require_count + forbid_count if comparable else None
            if total is not None and total > 0:
                self.solution_count = total
            return {
                "type": "selectionImpact",
                "facetId": facet_id,
                "exact": True,
                "comparableToCurrent": comparable,
                "totalPlans": total,
                "require": impact_direction(require_count, total),
                "forbid": impact_direction(forbid_count, total),
            }
        except PlanpilotCapacityError:
            require_available = self._route_has_plan(require_route)
            forbid_available = self._route_has_plan(forbid_route)
            return {
                "type": "selectionImpact",
                "facetId": facet_id,
                "exact": False,
                "comparableToCurrent": comparable,
                "totalPlans": self.solution_count if comparable else None,
                "require": availability_direction(require_available),
                "forbid": availability_direction(forbid_available),
            }
        finally:
            try:
                self._restore_solver_process(
                    timeout_seconds=fasb_impact_timeout_seconds(),
                    raise_on_failure=True,
                )
            except Exception as error:
                restore_error = error
            if restore_error is not None:
                raise PlanpilotCapacityError(
                    "PlanPilot could not restore the current plan space after the preview."
                ) from restore_error

    def _count_route(self, selections):
        deadline = monotonic() + fasb_impact_timeout_seconds()
        self._restart_solver(deadline)
        for facet_id, selection_state in sorted(selections.items()):
            self.service.send_command(
                build_selection_command(facet_id, selection_state),
                no_Output=True,
                timeout_seconds=self._remaining_time(deadline),
            )
        return normalize_count(
            self.service.send_command(
                "#!",
                timeout_seconds=self._remaining_time(deadline),
            )
        )

    def _route_has_plan(self, selections):
        deadline = monotonic() + fasb_impact_timeout_seconds()
        self._restart_solver(deadline)
        for facet_id, selection_state in sorted(selections.items()):
            self.service.send_command(
                build_selection_command(facet_id, selection_state),
                no_Output=True,
                timeout_seconds=self._remaining_time(deadline),
            )
        return (
            self.service.get_representative_solution(
                required=False,
                timeout_seconds=self._remaining_time(deadline),
            )
            is not None
        )

    def _restart_solver(self, deadline):
        timeout_seconds = self._remaining_time(deadline)
        if timeout_seconds is None:
            self.service.restart_FASB()
        else:
            self.service.restart_FASB(timeout_seconds=timeout_seconds)

    @staticmethod
    def _remaining_time(deadline):
        if deadline is None:
            return None
        remaining = deadline - monotonic()
        if remaining <= 0.1:
            raise PlanpilotCapacityError("PlanPilot impact preview timed out.")
        return remaining

    def query_response(self, query_type: str, solution_number=None, facet_id=None):
        with self.operation_lock:
            result = self.query(query_type, solution_number, facet_id)
            return {**self.to_response(), "result": result}

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
            if representative_plan is None and artifacts["horizon"] > planpilot_max_horizon():
                raise SessionHorizonError(
                    artifacts["horizon"],
                    planpilot_max_horizon(),
                )
            if representative_plan is None and artifacts["horizon"] > configuration.horizon:
                configuration = SessionConfiguration(
                    artifacts["horizon"],
                    configuration.encoding,
                    configuration.abstract_time_steps,
                )

            service = PlanpilotService()
            try:
                initial_facets = service.run_planpilot_service(
                    artifacts["sasFile"],
                    configuration.horizon,
                    configuration.encoding,
                    configuration.abstract_time_steps,
                )
                solver_solution = service.get_representative_solution(required=True)
                initial_solution = solver_solution
                if configuration.abstract_time_steps:
                    known_ids = {facet.get("id") for facet in initial_facets}
                    for implied in service.send_command("|= %") or []:
                        if implied.get("id") in known_ids or not is_abstract_facet(implied):
                            continue
                        implied_facet = dict(implied)
                        implied_facet["selectionState"] = "neutral"
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
            if session and not session.solver_available:
                self._sessions.pop(session_id, None)
                raise SessionNotFoundError(session_id)
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
            if session_id != exclude_session_id
            and (session.is_expired() or not session.solver_available)
        ]
        for session_id in expired_ids:
            session = self._sessions.pop(session_id)
            session.stop()


def impact_direction(remaining, total):
    reduction = None
    if total:
        reduction = (total - remaining) / total
    return {
        "available": remaining > 0,
        "plansRemaining": remaining,
        "planReduction": reduction,
    }


def availability_direction(available):
    return {
        "available": available,
        "plansRemaining": None,
        "planReduction": None,
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
