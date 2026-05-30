from flask import Blueprint, jsonify, request

from ..service.session_registry import (
    SUPPORTED_ENCODINGS,
    SessionConfiguration,
    SessionNotFoundError,
    session_registry,
)
from .auth import require_service_auth


sessions_bp = Blueprint("sessions", __name__)


@sessions_bp.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


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
        )
        return (
            jsonify(
                {
                    "sessionId": session.session_id,
                    "status": "ready",
                    "configuration": session.configuration.to_response(),
                    "facets": session.facets,
                }
            ),
            201,
        )
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
        return (
            jsonify({"sessionId": session.session_id, "facets": session.list_facets()}),
            200,
        )
    except SessionNotFoundError:
        return session_not_found()
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
        facets = session.select_facet(
            payload["facetId"],
            payload["selectionState"],
            payload.get("previousSelectionState"),
        )
        return (
            jsonify({"sessionId": session.session_id, "facets": facets}),
            200,
        )
    except SessionNotFoundError:
        return session_not_found()
    except ValueError as error:
        return invalid_request(str(error))
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
        result = session.query(payload["type"], payload.get("solutionNumber"))
        return jsonify({"sessionId": session.session_id, "result": result}), 200
    except SessionNotFoundError:
        return session_not_found()
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

    configuration = payload.get("configuration")
    if not isinstance(configuration, dict):
        return "configuration is required."

    horizon = configuration.get("horizon")
    if type(horizon) is not int or horizon <= 0:
        return "configuration.horizon must be a positive integer."

    if configuration.get("encoding") not in SUPPORTED_ENCODINGS:
        return "configuration.encoding must be exact or bounded."

    if type(configuration.get("abstractTimeSteps")) is not bool:
        return "configuration.abstractTimeSteps must be a boolean."

    source = payload.get("source")
    if not isinstance(source, dict) or source.get("system") != "IPEXCO":
        return "source.system must be IPEXCO."

    return None


def validate_select_facet_payload(payload):
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

    return None


def validate_query_payload(payload):
    if not isinstance(payload, dict):
        return "Request body must be a JSON object."

    if payload.get("type") not in {
        "facets",
        "facetCount",
        "facetReduction",
        "solution",
        "solutionCount",
        "solutionReduction",
    }:
        return "type must be facets, facetCount, facetReduction, solution, solutionCount, or solutionReduction."

    solution_number = payload.get("solutionNumber")
    if solution_number is not None and (
        type(solution_number) is not int or solution_number <= 0
    ):
        return "solutionNumber must be a positive integer."

    if payload.get("type") != "solution" and solution_number is not None:
        return "solutionNumber is only supported for solution queries."

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


def planpilot_failed(error):
    print(f"PlanPilot session request failed: {error}")
    return error_response(
        "PLANPILOT_FAILED", "PlanPilot failed to process the session request.", 500
    )


def error_response(code, message, status):
    return jsonify({"error": {"code": code, "message": message}}), status
