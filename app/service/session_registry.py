import atexit
import os
from collections import OrderedDict
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
    max_query_timeout_seconds,
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


class SessionJobNotFoundError(KeyError):
    pass


class SessionJobConflictError(RuntimeError):
    pass


@dataclass
class SessionQueryJob:
    job_id: str
    query_type: str
    selection_revision: int
    facet_id: Optional[str] = None
    solution_number: Optional[int] = None
    solution_start: Optional[int] = None
    timeout_seconds: Optional[int] = None
    status: str = "queued"
    result: Optional[Dict] = None
    error: Optional[Dict] = None
    created_at: datetime = field(default_factory=lambda: utc_now())
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    cancel_event: Event = field(default_factory=Event, repr=False)
    finished_event: Event = field(default_factory=Event, repr=False)

    def to_response(self):
        response = {
            "jobId": self.job_id,
            "type": self.query_type,
            "status": self.status,
            "selectionRevision": self.selection_revision,
            "createdAt": to_iso(self.created_at),
        }
        if self.facet_id is not None:
            response["facetId"] = self.facet_id
        if self.solution_number is not None:
            response["solutionNumber"] = self.solution_number
        if self.solution_start is not None:
            response["solutionStart"] = self.solution_start
        if self.timeout_seconds is not None:
            response["timeoutSeconds"] = self.timeout_seconds
        if self.started_at is not None:
            response["startedAt"] = to_iso(self.started_at)
        if self.completed_at is not None:
            response["completedAt"] = to_iso(self.completed_at)
        if self.result is not None:
            response["result"] = self.result
        if self.error is not None:
            response["error"] = self.error
        return response


@dataclass(frozen=True)
class SessionConfiguration:
    horizon: int
    encoding: str
    abstract_time_steps: bool
    state_facets: bool = False

    def to_response(self):
        return {
            "horizon": self.horizon,
            "encoding": self.encoding,
            "abstractTimeSteps": self.abstract_time_steps,
            "stateFacets": self.state_facets,
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
    solution_cache: OrderedDict[int, Dict] = field(default_factory=OrderedDict)
    minimum_horizon: Optional[int] = None
    applied_selections: Dict[str, str] = field(default_factory=dict)
    facet_catalog: Dict[str, Dict] = field(default_factory=dict)
    selection_revision: int = 0
    solver_available: bool = True
    created_at: datetime = field(default_factory=lambda: utc_now())
    last_access_at: datetime = field(default_factory=lambda: utc_now())
    expires_at: datetime = field(default_factory=lambda: utc_now() + session_ttl())
    operation_lock: RLock = field(default_factory=RLock, repr=False)
    jobs: Dict[str, SessionQueryJob] = field(default_factory=dict, repr=False)
    jobs_lock: RLock = field(default_factory=RLock, repr=False)
    stopping: Event = field(default_factory=Event, repr=False)

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
            "cachedSolutions": len(self.solution_cache),
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
            previous_solution_cache = OrderedDict(self.solution_cache)
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
                self.solution_cache = OrderedDict()
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
                if timestep is not None and self._is_action_facet_id(facet_id):
                    for active_id, active_state in list(updated.items()):
                        if (
                            active_id != facet_id
                            and active_state == "positive"
                            and self._is_action_facet_id(active_id)
                            and self.facet_timesteps[active_id] == timestep
                        ):
                            updated.pop(active_id)
            updated[facet_id] = state
        return updated

    @staticmethod
    def _is_action_facet_id(facet_id):
        return not facet_id.startswith("holds(")

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

    def query(
        self,
        query_type: str,
        solution_number=None,
        facet_id=None,
        solution_mode="single",
        timeout_seconds=None,
    ):
        with self.operation_lock:
            if query_type == "facets":
                return {"type": query_type, "facets": self.list_facets()}
            try:
                if query_type == "facetCount":
                    return {
                        "type": query_type,
                        "value": normalize_count(
                            self._query_command("#?", timeout_seconds)
                        ),
                    }
                if query_type == "facetReduction":
                    if normalize_count(
                        self._query_command("#?", timeout_seconds)
                    ) == 0:
                        return {"type": query_type, "facets": []}
                    return {
                        "type": query_type,
                        "facets": normalize_facets(
                            self._query_command("#??", timeout_seconds)
                        ),
                    }
                if query_type == "impliedFacets":
                    return {
                        "type": query_type,
                        "facets": normalize_implied_facets(
                            self._query_command("|= %", timeout_seconds)
                        ),
                    }
                if query_type == "solutionCount":
                    if self.solution_count is None:
                        count = normalize_count(
                            self._query_command("#!", timeout_seconds)
                        )
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
                    if normalize_count(
                        self._query_command("#?", timeout_seconds)
                    ) == 0:
                        return {"type": query_type, "facets": []}
                    return {
                        "type": query_type,
                        "facets": normalize_facets(
                            self._query_command("#!!", timeout_seconds)
                        ),
                    }
                if query_type == "selectionImpact":
                    return self._selection_impact(facet_id, timeout_seconds)
                if query_type == "solution":
                    requested_number = solution_number
                    if (
                        solution_mode == "prefix"
                        and requested_number is not None
                        and self.solution_count is not None
                    ):
                        requested_number = min(requested_number, self.solution_count)
                    if (
                        requested_number is not None
                        and self._has_cached_solutions(
                            requested_number,
                            solution_mode,
                        )
                    ):
                        return {
                            "type": query_type,
                            "solutions": self._cached_solutions(
                                requested_number,
                                solution_mode,
                            ),
                        }
                    if (
                        requested_number is not None
                        and self.solution_count is not None
                        and requested_number > self.solution_count
                    ):
                        return {"type": query_type, "solutions": []}
                    command = build_solution_command(requested_number)
                    solutions = normalize_solutions(
                        self._query_command(command, timeout_seconds)
                    )
                    if requested_number is not None:
                        if len(solutions) < requested_number and solutions:
                            self.solution_count = len(solutions)
                        requested_solutions = self._requested_solutions(
                            solutions,
                            requested_number,
                            solution_mode,
                        )
                        for solution in solutions:
                            self._cache_solution(solution)
                        solutions = requested_solutions
                    return {
                        "type": query_type,
                        "solutions": solutions,
                    }
                raise ValueError("Unsupported PlanPilot query type.")
            except PlanpilotCapacityError:
                if query_type != "selectionImpact":
                    self._restore_solver_process(timeout_seconds=timeout_seconds)
                raise

    def _query_command(self, command, timeout_seconds):
        if timeout_seconds is None:
            return self.service.send_command(command)
        return self.service.send_command(
            command,
            timeout_seconds=timeout_seconds,
        )

    def _cached_solutions(self, solution_number, solution_mode):
        if not isinstance(self.solution_cache, OrderedDict):
            self.solution_cache = OrderedDict(self.solution_cache)
        if solution_mode == "prefix":
            solutions = [
                self.solution_cache[number]
                for number in range(1, solution_number + 1)
                if number in self.solution_cache
            ]
            for number in range(1, solution_number + 1):
                if number in self.solution_cache:
                    self.solution_cache.move_to_end(number)
            return solutions
        requested = self.solution_cache.get(solution_number)
        if requested is not None:
            self.solution_cache.move_to_end(solution_number)
        return [requested] if requested else []

    def _has_cached_solutions(self, solution_number, solution_mode):
        if solution_mode == "prefix":
            return all(
                number in self.solution_cache
                for number in range(1, solution_number + 1)
            )
        return solution_number in self.solution_cache

    @staticmethod
    def _requested_solutions(solutions, solution_number, solution_mode):
        if solution_mode == "prefix":
            return solutions[:solution_number]
        expected_label = f"solution {solution_number}"
        requested = next(
            (
                solution
                for solution in solutions
                if solution.get("label") == expected_label
            ),
            None,
        )
        return [requested] if requested else []

    def _cache_solution(self, solution):
        label = solution.get("label", "")
        prefix = "solution "
        if not label.startswith(prefix):
            return
        try:
            number = int(label[len(prefix):])
        except ValueError:
            return
        if number <= 0:
            return
        if not isinstance(self.solution_cache, OrderedDict):
            self.solution_cache = OrderedDict(self.solution_cache)
        self.solution_cache[number] = solution
        self.solution_cache.move_to_end(number)
        while len(self.solution_cache) > solution_cache_limit():
            self.solution_cache.popitem(last=False)

    def _selection_impact(self, facet_id, timeout_seconds=None):
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
            and self._is_action_facet_id(active_id)
            and self._is_action_facet_id(facet_id)
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
            require_count = self._count_route(require_route, timeout_seconds)
            forbid_count = self._count_route(forbid_route, timeout_seconds)
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
            require_available = self._route_has_plan(
                require_route, timeout_seconds
            )
            forbid_available = self._route_has_plan(
                forbid_route, timeout_seconds
            )
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
                    timeout_seconds=(
                        timeout_seconds or fasb_impact_timeout_seconds()
                    ),
                    raise_on_failure=True,
                )
            except Exception as error:
                restore_error = error
            if restore_error is not None:
                raise PlanpilotCapacityError(
                    "PlanPilot could not restore the current plan space after the preview."
                ) from restore_error

    def _count_route(self, selections, timeout_seconds=None):
        deadline = monotonic() + (
            timeout_seconds or fasb_impact_timeout_seconds()
        )
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

    def _route_has_plan(self, selections, timeout_seconds=None):
        deadline = monotonic() + (
            timeout_seconds or fasb_impact_timeout_seconds()
        )
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

    def query_response(
        self,
        query_type: str,
        solution_number=None,
        facet_id=None,
        solution_mode="single",
        timeout_seconds=None,
    ):
        with self.operation_lock:
            result = self.query(
                query_type,
                solution_number,
                facet_id,
                solution_mode,
                timeout_seconds,
            )
            return {**self.to_response(), "result": result}

    def start_query_job(
        self,
        query_type,
        facet_id=None,
        expected_revision=None,
        solution_number=None,
        solution_start=None,
        timeout_seconds=None,
    ):
        if query_type not in {"solution", "solutionCount", "selectionImpact"}:
            raise ValueError("Unsupported PlanPilot job type.")
        if query_type == "selectionImpact" and not facet_id:
            raise ValueError("facetId is required for selectionImpact jobs.")
        if query_type != "selectionImpact" and facet_id is not None:
            raise ValueError("facetId is only supported for selectionImpact jobs.")
        if query_type == "solution" and (
            type(solution_number) is not int
            or solution_number <= 0
            or solution_number > 9_007_199_254_740_991
        ):
            raise ValueError(
                "solutionNumber is required for solution jobs and must be a positive safe integer."
            )
        if query_type != "solution" and solution_number is not None:
            raise ValueError("solutionNumber is only supported for solution jobs.")
        if solution_start is not None and (
            query_type != "solution"
            or type(solution_start) is not int
            or solution_start <= 0
            or solution_start > solution_number
        ):
            raise ValueError(
                "solutionStart must be a positive integer no greater than solutionNumber."
            )
        if (
            expected_revision is not None
            and expected_revision != self.selection_revision
        ):
            raise SessionRevisionConflictError(
                expected_revision,
                self.selection_revision,
            )
        if (
            timeout_seconds is not None
            and (
                type(timeout_seconds) is not int
                or timeout_seconds < 5
                or timeout_seconds > max_query_timeout_seconds()
            )
        ):
            raise ValueError(
                "timeoutSeconds must be an integer between 5 and "
                f"{max_query_timeout_seconds()}."
            )

        with self.jobs_lock:
            active = next(
                (
                    job
                    for job in self.jobs.values()
                    if job.status in {"queued", "running"}
                ),
                None,
            )
            if active is not None:
                raise SessionJobConflictError(
                    f"PlanPilot job '{active.job_id}' is already running."
                )
            self._trim_jobs_locked()
            job = SessionQueryJob(
                job_id=f"pp_job_{uuid4().hex}",
                query_type=query_type,
                selection_revision=self.selection_revision,
                facet_id=facet_id,
                solution_number=solution_number,
                solution_start=solution_start,
                timeout_seconds=timeout_seconds,
            )
            self.jobs[job.job_id] = job

        Thread(target=self._run_query_job, args=(job,), daemon=True).start()
        return job.to_response()

    def get_query_job(self, job_id):
        with self.jobs_lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise SessionJobNotFoundError(job_id)
            return job.to_response()

    def cancel_query_job(self, job_id):
        with self.jobs_lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise SessionJobNotFoundError(job_id)
            if job.status in {"succeeded", "failed", "cancelled"}:
                return job.to_response()
            job.cancel_event.set()
            if job.status == "queued":
                job.status = "cancelled"
                job.completed_at = utc_now()
                job.finished_event.set()
                return job.to_response()

        self.service.stop_fasb()
        job.finished_event.wait(timeout=2)
        with self.jobs_lock:
            return job.to_response()

    def _run_query_job(self, job):
        with self.jobs_lock:
            if job.cancel_event.is_set():
                job.status = "cancelled"
                job.completed_at = utc_now()
                job.finished_event.set()
                return
            job.status = "running"
            job.started_at = utc_now()

        with self.operation_lock:
            try:
                query_options = {
                    "solution_number": job.solution_number,
                    "facet_id": job.facet_id,
                    "solution_mode": (
                        "prefix"
                        if job.query_type == "solution"
                        and job.solution_start is not None
                        else "single"
                    ),
                }
                if job.timeout_seconds is not None:
                    query_options["timeout_seconds"] = job.timeout_seconds
                result = self.query(job.query_type, **query_options)
                if (
                    job.query_type == "solution"
                    and job.solution_start is not None
                ):
                    result = {
                        **result,
                        "solutions": result.get("solutions", [])[
                            job.solution_start - 1:
                        ],
                    }
                with self.jobs_lock:
                    if job.cancel_event.is_set():
                        job.status = "cancelled"
                    else:
                        job.status = "succeeded"
                        job.result = result
                    job.completed_at = utc_now()
            except Exception as error:
                with self.jobs_lock:
                    if job.cancel_event.is_set():
                        job.status = "cancelled"
                    else:
                        job.status = "failed"
                        job.error = query_job_error(error)
                    job.completed_at = utc_now()
            finally:
                if job.cancel_event.is_set() and not self.stopping.is_set():
                    self._restore_solver_process()
                job.finished_event.set()

    def _trim_jobs_locked(self):
        completed = sorted(
            (
                job
                for job in self.jobs.values()
                if job.status in {"succeeded", "failed", "cancelled"}
            ),
            key=lambda job: job.completed_at or job.created_at,
        )
        while len(self.jobs) >= query_job_history_limit() and completed:
            oldest = completed.pop(0)
            self.jobs.pop(oldest.job_id, None)

    def stop(self):
        self.stopping.set()
        with self.jobs_lock:
            for job in self.jobs.values():
                if job.status in {"queued", "running"}:
                    job.cancel_event.set()
                    job.status = "cancelled"
                    job.completed_at = utc_now()
        self.service.stop_fasb()
        with self.operation_lock:
            self.solver_available = False
        with self.jobs_lock:
            for job in self.jobs.values():
                if job.status == "cancelled":
                    job.finished_event.set()


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
                    configuration.state_facets,
                )

            service = PlanpilotService()
            try:
                service_arguments = (
                    artifacts["sasFile"],
                    configuration.horizon,
                    configuration.encoding,
                    configuration.abstract_time_steps,
                )
                if configuration.state_facets:
                    initial_facets = service.run_planpilot_service(
                        *service_arguments,
                        state_facets=True,
                    )
                else:
                    initial_facets = service.run_planpilot_service(
                        *service_arguments,
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


def solution_cache_limit():
    return env_positive_int("PLANPILOT_MAX_CACHED_SOLUTIONS", 100)


def query_job_history_limit():
    return env_positive_int("PLANPILOT_JOB_HISTORY_LIMIT", 20)


def query_job_error(error):
    if isinstance(error, PlanpilotCapacityError):
        return {
            "code": "PLAN_SPACE_TOO_LARGE",
            "message": "PlanPilot did not finish this operation within its processing limit.",
        }
    if isinstance(error, PlanpilotNoPlanError):
        return {
            "code": "NO_PLAN",
            "message": "No non-empty plan exists for this operation.",
        }
    if isinstance(error, ValueError):
        return {"code": "INVALID_REQUEST", "message": str(error)}
    return {
        "code": "PLANPILOT_FAILED",
        "message": "PlanPilot failed to process this operation.",
    }


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
