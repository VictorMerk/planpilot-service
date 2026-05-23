from flask import Blueprint, request, jsonify
from ..service.planpilot_service import PlanpilotService

planpilot_bp = Blueprint("planpilot", __name__)
planpilot_service = PlanpilotService()


@planpilot_bp.route("/run-planpilot", methods=["POST"])
def run_planpilot():
    data = request.json
    sas_file = data.get("sasFile")
    horizon = data.get("horizon")
    encoding = data.get("encoding")
    abstract_time_steps = data.get("abstractTimeStep")

    if not sas_file or not horizon or not encoding:
        return jsonify({"error": "sasFile, horizon, and encoding are required"}), 400

    try:
        # Run PlanPilot service and get the facets
        facets = planpilot_service.run_planpilot_service(
            sas_file, horizon, encoding, abstract_time_steps
        )
        return jsonify({"output": facets}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@planpilot_bp.route("/activate-plan", methods=["POST"])
def activate_plan():
    data = request.json
    plan_file = data.get("planFile")

    if not plan_file:
        return jsonify({"error": "planFile is required"}), 400

    try:
        result = planpilot_service.activate_best_plan(plan_file)
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@planpilot_bp.route("/update-plan", methods=["POST"])
def update_plan():
    data = request.json
    changed_timestep = data.get("changedTimestep")
    commands = data.get("commands")

    if changed_timestep is None or commands is None:
        return jsonify({"error": "changedTimestep and commands are required"}), 400

    try:
        result = planpilot_service.fast_update_plan_from_timestep(
            changed_timestep, commands
        )

        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@planpilot_bp.route("/refresh-timeline", methods=["GET"])
def refresh_timeline():
    try:
        result = planpilot_service.get_refreshed_timeline()
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@planpilot_bp.route("/refresh-optional-facet", methods=["GET"])
def refresh_optional_facet():
    try:
        timestep_param = request.args.get("timestep")
        if timestep_param is None:
            return jsonify({"error": "Missing 'timestep' parameter"}), 400

        try:
            timestep_number = int(timestep_param)
        except ValueError:
            return jsonify({"error": "'timestep' must be an integer"}), 400

        refreshed_answer = planpilot_service.refresh_timestep_optional_facet(
            timestep_number
        )
        if not refreshed_answer:
            return jsonify({"error": "No optional facet found for this timestep"}), 404

        return jsonify(refreshed_answer), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@planpilot_bp.route("/send-planpilot-command", methods=["POST"])
def send_command():
    data = request.json
    command = data.get("command")
    if not command:
        return jsonify({"error": "No command provided"}), 400
    try:
        output = planpilot_service.send_command(command)
        return jsonify({"output": output}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@planpilot_bp.route("/stop-planpilot", methods=["POST"])
def stop_planpilot():
    try:
        planpilot_service.stop_fasb()
        return jsonify({"status": "FASB stopped"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
