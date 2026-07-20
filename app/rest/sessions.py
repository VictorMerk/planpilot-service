import os

from flask import Blueprint, current_app, jsonify, request

from ..service.fastdownward_service import (
    FastDownwardCapacityError,
    FastDownwardNoPlanError,
    FastDownwardUnsolvableError,
)
from ..service.planpilot_service import (
    PlanpilotCapacityError,
    PlanpilotNoPlanError,
    planpilot_max_horizon,
)
from ..service.session_registry import (
    SUPPORTED_ENCODINGS,
    SessionConfiguration,
    SessionCapacityError,
    SessionExpiredError,
    SessionHorizonError,
    SessionNotFoundError,
    SessionRevisionConflictError,
    SessionSelectionConflictError,
    session_registry,
)
from .auth import require_service_auth


sessions_bp = Blueprint("sessions", __name__)
MAX_PDDL_BYTES = 1_000_000


@sessions_bp.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "capacity": session_registry.capacity_snapshot()}), 200


@sessions_bp.route("/ready", methods=["GET"])
def ready():
    capacity = session_registry.capacity_snapshot()
    api_key_configured = bool(os.environ.get("API_KEY"))
    is_ready = capacity["acceptingNewSessions"] and api_key_configured
    status = 200 if is_ready else 503
    if not api_key_configured:
        readiness = "misconfigured"
    elif not capacity["acceptingNewSessions"]:
        readiness = "at-capacity"
    else:
        readiness = "ready"
    response = jsonify(
        {
            "status": readiness,
            "capacity": capacity,
        }
    )
    if status == 503:
        response.headers["Retry-After"] = "5"
    return response, status


@sessions_bp.route("/sessions", methods=["POST"])
@require_service_auth
def create_session():
    payload = request.get_json(silent=True)
    validation_error = validate_create_session_payload(payload)
    if validation_error:
        return invalid_request(validation_error)

    try:
        configuration = get_configuration(payload["configuration"])
        session = session_registry.create_session(
            payload["task"]["domainPddl"],
            payload["task"]["problemPddl"],
            configuration,
            representative_plan=payload.get("representativePlan"),
        )
        return (
            jsonify(
                {
                    **session.to_response(),
                    "solutionCount": session.solution_count,
                    "facets": session.facets,
                }
            ),
            201,
        )
    except FastDownwardUnsolvableError:
        return task_unsolvable()
    except (FastDownwardNoPlanError, PlanpilotNoPlanError):
        return no_plan()
    except (FastDownwardCapacityError, PlanpilotCapacityError):
        return plan_space_too_large()
    except SessionCapacityError as error:
        return service_at_capacity(error)
    except SessionHorizonError as error:
        return (
            jsonify({
                "error": {
                    "message": str(error),
                    "code": "HORIZON_LIMIT_EXCEEDED",
                    "minimumHorizon": error.minimum_horizon,
                    "maximumHorizon": error.maximum_horizon,
                }
            }),
            422,
        )
    except Exception as error:
        return planpilot_failed(error)


@sessions_bp.route("/sessions/<session_id>", methods=["GET"])
@require_service_auth
def get_session(session_id):
    try:
        session = session_registry.get_session(session_id)
        return jsonify(session.to_response()), 200
    except SessionExpiredError:
        return session_expired()
    except SessionNotFoundError:
        return session_not_found()
    except PlanpilotCapacityError:
        return plan_space_too_large()
    except Exception as error:
        return planpilot_failed(error)


@sessions_bp.route("/sessions/<session_id>/facets/list", methods=["POST"])
@require_service_auth
def list_facets(session_id):
    payload = request.get_json(silent=True)
    if payload != {}:
        return invalid_request("Facet list body must be an empty JSON object.")

    try:
        session = session_registry.get_session(session_id)
        return jsonify(session.list_facets_response()), 200
    except SessionExpiredError:
        return session_expired()
    except SessionNotFoundError:
        return session_not_found()
    except PlanpilotCapacityError:
        return plan_space_too_large()
    except Exception as error:
        return planpilot_failed(error)


@sessions_bp.route("/sessions/<session_id>/facets/select", methods=["POST"])
@require_service_auth
def select_facet(session_id):
    payload = request.get_json(silent=True)
    validation_error = validate_select_facet_payload(payload)
    if validation_error:
        return invalid_request(validation_error)

    try:
        session = session_registry.get_session(session_id)
        response = session.select_facet(
            payload["facetId"],
            payload["selectionState"],
            payload.get("previousSelectionState"),
            payload.get("expectedSelectionRevision"),
        )
        return jsonify(response), 200
    except SessionExpiredError:
        return session_expired()
    except SessionNotFoundError:
        return session_not_found()
    except (SessionSelectionConflictError, SessionRevisionConflictError) as error:
        return selection_conflict(error)
    except ValueError as error:
        return invalid_request(str(error))
    except PlanpilotNoPlanError:
        return no_plan(status=409)
    except PlanpilotCapacityError:
        return plan_space_too_large()
    except Exception as error:
        return planpilot_failed(error)


@sessions_bp.route("/sessions/<session_id>/facets/apply", methods=["POST"])
@require_service_auth
def apply_facets(session_id):
    payload = request.get_json(silent=True)
    validation_error = validate_apply_facets_payload(payload)
    if validation_error:
        return invalid_request(validation_error)

    try:
        session = session_registry.get_session(session_id)
        response = session.apply_selections(
            payload["selections"],
            payload.get("expectedSelectionRevision"),
        )
        return jsonify(response), 200
    except SessionExpiredError:
        return session_expired()
    except SessionNotFoundError:
        return session_not_found()
    except (SessionSelectionConflictError, SessionRevisionConflictError) as error:
        return selection_conflict(error)
    except ValueError as error:
        return invalid_request(str(error))
    except PlanpilotNoPlanError:
        return no_plan(status=409)
    except PlanpilotCapacityError:
        return plan_space_too_large()
    except Exception as error:
        return planpilot_failed(error)


@sessions_bp.route("/sessions/<session_id>/query", methods=["POST"])
@require_service_auth
def query_session(session_id):
    payload = request.get_json(silent=True)
    validation_error = validate_query_payload(payload)
    if validation_error:
        return invalid_request(validation_error)

    try:
        session = session_registry.get_session(session_id)
        response = session.query_response(
            payload["type"],
            payload.get("solutionNumber"),
            payload.get("facetId"),
        )
        return jsonify(response), 200
    except SessionExpiredError:
        return session_expired()
    except SessionNotFoundError:
        return session_not_found()
    except PlanpilotCapacityError:
        return plan_space_too_large()
    except PlanpilotNoPlanError:
        return no_plan()
    except ValueError as error:
        return invalid_request(str(error))
    except Exception as error:
        return planpilot_failed(error)


@sessions_bp.route("/sessions/<session_id>", methods=["DELETE"])
@require_service_auth
def stop_session(session_id):
    try:
        session = session_registry.stop_session(session_id)
        return jsonify({"sessionId": session.session_id, "status": "stopped"}), 200
    except SessionNotFoundError:
        return session_not_found()
    except Exception as error:
        return planpilot_failed(error)


def validate_create_session_payload(payload):
    if not isinstance(payload, dict):
        return "Request body must be a JSON object."

    task = payload.get("task")
    if not isinstance(task, dict):
        return "task is required."

    if not is_non_empty_string(task.get("domainPddl")):
        return "task.domainPddl is required."

    if not is_non_empty_string(task.get("problemPddl")):
        return "task.problemPddl is required."

    if len(task["domainPddl"].encode("utf-8")) > MAX_PDDL_BYTES:
        return "task.domainPddl is too large."

    if len(task["problemPddl"].encode("utf-8")) > MAX_PDDL_BYTES:
        return "task.problemPddl is too large."

    configuration = payload.get("configuration")
    if not isinstance(configuration, dict):
        return "configuration is required."

    horizon = configuration.get("horizon")
    if type(horizon) is not int or horizon <= 0:
        return "configuration.horizon must be a positive integer."
    if horizon > planpilot_max_horizon():
        return (
            "configuration.horizon must not exceed "
            f"{planpilot_max_horizon()}."
        )

    if configuration.get("encoding") not in SUPPORTED_ENCODINGS:
        return "configuration.encoding must be exact or bounded."

    if type(configuration.get("abstractTimeSteps")) is not bool:
        return "configuration.abstractTimeSteps must be a boolean."

    source = payload.get("source")
    if not isinstance(source, dict) or source.get("system") != "IPEXCO":
        return "source.system must be IPEXCO."

    representative_plan = payload.get("representativePlan")
    if representative_plan is not None:
        if not isinstance(representative_plan, list) or not representative_plan:
            return "representativePlan must be a non-empty array when provided."
        if len(representative_plan) > horizon:
            return "representativePlan must not contain more actions than the configured horizon."
        for index, action in enumerate(representative_plan):
            if not isinstance(action, dict):
                return f"representativePlan[{index}] must be an object."
            if set(action) != {"name", "params"}:
                return f"representativePlan[{index}] must contain only name and params."
            if not is_pddl_token(action.get("name")):
                return f"representativePlan[{index}].name must be a PDDL token."
            params = action.get("params")
            if not isinstance(params, list) or not all(is_pddl_token(value) for value in params):
                return f"representativePlan[{index}].params must contain only PDDL tokens."
        if (
            configuration.get("encoding") == "exact"
            and len(representative_plan) != horizon
        ):
            return "representativePlan must fill the configured horizon in exact mode."

    return None


def is_pddl_token(value):
    return (
        isinstance(value, str)
        and bool(value)
        and not any(character.isspace() or character in "()" for character in value)
    )


def validate_select_facet_payload(payload):
    return validate_facet_selection(payload, allow_expected_revision=True)


def validate_facet_selection(payload, allow_expected_revision=False):
    if not isinstance(payload, dict):
        return "Request body must be a JSON object."

    if not is_non_empty_string(payload.get("facetId")):
        return "facetId is required."

    if payload.get("selectionState") not in {
        "neutral",
        "positive",
        "negative",
    }:
        return "selectionState must be neutral, positive, or negative."

    previous_state = payload.get("previousSelectionState")
    if previous_state is not None and previous_state not in {
        "neutral",
        "positive",
        "negative",
    }:
        return "previousSelectionState must be neutral, positive, or negative."

    if allow_expected_revision:
        revision_error = validate_expected_selection_revision(payload)
        if revision_error:
            return revision_error

    return None


def validate_expected_selection_revision(payload):
    expected_revision = payload.get("expectedSelectionRevision")
    if expected_revision is not None and (
        type(expected_revision) is not int or expected_revision < 0
    ):
        return "expectedSelectionRevision must be a non-negative integer."
    return None


def validate_apply_facets_payload(payload):
    if not isinstance(payload, dict):
        return "Request body must be a JSON object."
    selections = payload.get("selections")
    if not isinstance(selections, list) or not selections:
        return "selections must be a non-empty array."
    if len(selections) > 50:
        return "At most 50 facet selections can be applied at once."
    revision_error = validate_expected_selection_revision(payload)
    if revision_error:
        return revision_error
    seen_ids = set()
    for selection in selections:
        error = validate_facet_selection(selection)
        if error:
            return error
        facet_id = selection["facetId"]
        if facet_id in seen_ids:
            return "Each facetId may occur only once."
        seen_ids.add(facet_id)
    return None


def validate_query_payload(payload):
    if not isinstance(payload, dict):
        return "Request body must be a JSON object."

    if payload.get("type") not in {
        "facets",
        "facetCount",
        "facetReduction",
        "impliedFacets",
        "solution",
        "solutionCount",
        "solutionReduction",
        "selectionImpact",
    }:
        return "type must be facets, facetCount, facetReduction, impliedFacets, solution, solutionCount, solutionReduction, or selectionImpact."

    solution_number = payload.get("solutionNumber")
    if solution_number is not None and (
        type(solution_number) is not int
        or solution_number <= 0
        or solution_number > 9_007_199_254_740_991
    ):
        return "solutionNumber must be a positive safe integer."

    if payload.get("type") == "solution" and solution_number is None:
        return "solutionNumber is required for solution queries."

    if payload.get("type") != "solution" and solution_number is not None:
        return "solutionNumber is only supported for solution queries."

    facet_id = payload.get("facetId")
    if payload.get("type") == "selectionImpact":
        if not is_non_empty_string(facet_id):
            return "facetId is required for selectionImpact queries."
    elif facet_id is not None:
        return "facetId is only supported for selectionImpact queries."

    return None


def get_configuration(configuration):
    return SessionConfiguration(
        horizon=configuration["horizon"],
        encoding=configuration["encoding"],
        abstract_time_steps=configuration["abstractTimeSteps"],
    )


def is_non_empty_string(value):
    return isinstance(value, str) and bool(value.strip())


def invalid_request(message):
    return error_response("INVALID_REQUEST", message, 400)


def session_not_found():
    return error_response("SESSION_NOT_FOUND", "PlanPilot session was not found.", 404)


def session_expired():
    return error_response("SESSION_EXPIRED", "PlanPilot session has expired.", 410)


def selection_conflict(error):
    return error_response("SELECTION_CONFLICT", str(error), 409)


def task_unsolvable():
    return error_response(
        "TASK_UNSOLVABLE",
        "Fast Downward proved that the planning task is unsatisfiable.",
        422,
    )


def no_plan(status=422):
    return error_response(
        "NO_PLAN",
        "No non-empty plan exists for the requested PlanPilot configuration.",
        status,
    )


def plan_space_too_large():
    return error_response(
        "PLAN_SPACE_TOO_LARGE",
        "PlanPilot did not finish this operation within its processing limit.",
        503,
    )


def service_at_capacity(error):
    response, status = error_response(error.code, str(error), 503)
    response.headers["Retry-After"] = "5"
    return response, status


def planpilot_failed(error):
    current_app.logger.exception("PlanPilot session request failed: %s", error)
    return error_response(
        "PLANPILOT_FAILED", "PlanPilot failed to process the session request.", 500
    )


def error_response(code, message, status):
    return jsonify({"error": {"code": code, "message": message}}), status
