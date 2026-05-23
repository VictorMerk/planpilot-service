import os
import subprocess
from ..persistence.db import db
from ..persistence.models import FastDownwardRequest
from ..utils.hashing import compute_hash_from_files


def run_fastdownward_service(domain_file, problem_file):
    # Read file content
    domain_bytes = domain_file.read()
    problem_bytes = problem_file.read()

    # Compute hash from file contents
    hash_value = compute_hash_from_files(domain_bytes, problem_bytes)

    # Define a base directory for storing this run
    current_directory = os.getcwd()
    base_dir = os.path.join(current_directory, "temp", hash_value)
    os.makedirs(base_dir, exist_ok=True)

    # File paths
    domain_file_path = os.path.join(base_dir, "domain.pddl")
    problem_file_path = os.path.join(base_dir, "problem.pddl")
    sas_file_path = os.path.join(base_dir, "output.sas")
    plan_file_path = os.path.join(base_dir, "sas_plan")

    # Save domain/problem files if they don’t exist yet
    if not os.path.exists(domain_file_path):
        with open(domain_file_path, "wb") as f:
            f.write(domain_bytes)
    if not os.path.exists(problem_file_path):
        with open(problem_file_path, "wb") as f:
            f.write(problem_bytes)

    # Check if result already exists
    existing_request = FastDownwardRequest.query.filter_by(
        hash_value=hash_value
    ).first()
    if existing_request:
        horizon = calculate_horizon(existing_request.plan_file_path)
        return {
            "horizon": horizon,
            "sasFile": existing_request.sas_file_path,
            "planFile": existing_request.plan_file_path,
            "cached": True,
        }

    # Paths to necessary files and directories
    fast_downward_script = os.path.join(
        current_directory, "lib", "downward", "fast-downward.py"
    )

    # Command to execute fast-downward
    command = [
        "python3",
        fast_downward_script,
        "--plan-file",
        plan_file_path,
        "--sas-file",
        sas_file_path,
        "--keep-sas-file",
        domain_file_path,
        problem_file_path,
        "--search",
        "astar(lmcut())",
    ]

    # Execute the command
    result = subprocess.run(command, capture_output=True, text=True)

    if result.returncode != 0:
        raise RuntimeError(f"Fast Downward execution failed: {result.stderr}")

    # Calculate horizon
    horizon = calculate_horizon(plan_file_path)

    # Save result to DB
    try:
        new_request = FastDownwardRequest(
            hash_value=hash_value,
            domain_file_path=domain_file_path,
            problem_file_path=problem_file_path,
            sas_file_path=sas_file_path,
            plan_file_path=plan_file_path,
        )
        db.session.add(new_request)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Error saving to DB: {e}")

    return {
        "horizon": horizon,
        "sasFile": sas_file_path,
        "planFile": plan_file_path,
        "cached": False,
    }


def calculate_horizon(plan_file_path):
    with open(plan_file_path, "r") as f:
        lines = [line.strip() for line in f if line.strip()]

    # If last line starts with a semicolon, it's a comment (like "; cost = ...")
    if lines and lines[-1].startswith(";"):
        return len(lines) - 1
    return len(lines)
