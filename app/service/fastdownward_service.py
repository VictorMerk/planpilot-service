import os
import subprocess
import threading
from ..persistence.db import db
from ..persistence.models import FastDownwardRequest
from ..utils.hashing import compute_hash_from_files


_task_locks = {}
_task_locks_guard = threading.Lock()


class FastDownwardNoPlanError(RuntimeError):
    """Fast Downward completed without producing a usable plan."""


class FastDownwardUnsolvableError(FastDownwardNoPlanError):
    """Fast Downward proved that the planning task is unsatisfiable."""


class FastDownwardCapacityError(RuntimeError):
    """Task preparation exceeded the service's bounded runtime."""


def run_fastdownward_service(domain_file, problem_file, representative_plan=None):
    domain_bytes = domain_file.read()
    problem_bytes = problem_file.read()

    hash_value = compute_hash_from_files(domain_bytes, problem_bytes)

    with _task_lock(hash_value):
        return _run_fastdownward_for_hash(
            domain_bytes,
            problem_bytes,
            hash_value,
            representative_plan,
        )


def _task_lock(hash_value):
    with _task_locks_guard:
        return _task_locks.setdefault(hash_value, threading.Lock())


def _run_fastdownward_for_hash(
    domain_bytes,
    problem_bytes,
    hash_value,
    representative_plan,
):

    current_directory = os.getcwd()
    base_dir = os.path.join(current_directory, "temp", hash_value)
    os.makedirs(base_dir, exist_ok=True)

    domain_file_path = os.path.join(base_dir, "domain.pddl")
    problem_file_path = os.path.join(base_dir, "problem.pddl")
    sas_file_path = os.path.join(base_dir, "output.sas")
    plan_file_path = os.path.join(base_dir, "sas_plan")

    with open(domain_file_path, "wb") as f:
        f.write(domain_bytes)
    with open(problem_file_path, "wb") as f:
        f.write(problem_bytes)

    existing_request = FastDownwardRequest.query.filter_by(
        hash_value=hash_value
    ).first()
    if existing_request:
        try:
            if not existing_request.sas_file_path or not os.path.isfile(
                existing_request.sas_file_path
            ):
                raise FastDownwardNoPlanError(
                    "The cached Fast Downward translation is missing."
                )
            if representative_plan is not None:
                # A caller-supplied plan is request data, not a cache artifact.  In
                # particular, never overwrite the shared Fast Downward plan for
                # another session with it.
                horizon = len(representative_plan)
            else:
                if not existing_request.plan_file_path:
                    raise FastDownwardNoPlanError(
                        "The cached Fast Downward plan is missing."
                    )
                horizon = calculate_horizon(existing_request.plan_file_path)
        except (OSError, FastDownwardNoPlanError, ValueError):
            db.session.delete(existing_request)
            db.session.commit()
        else:
            return {
                "horizon": horizon,
                "sasFile": existing_request.sas_file_path,
                "planFile": existing_request.plan_file_path,
                "cached": True,
            }

    fast_downward_script = os.path.join(
        current_directory, "lib", "downward", "fast-downward.py"
    )

    command = [
        "python3",
        fast_downward_script,
        "--sas-file",
        sas_file_path,
        "--keep-sas-file",
    ]
    if representative_plan is None:
        command.extend(
            [
                "--plan-file",
                plan_file_path,
                domain_file_path,
                problem_file_path,
                "--search",
                "astar(lmcut())",
            ]
        )
    else:
        # A supplied plan only needs Fast Downward's SAS translation.
        command.extend(["--translate", domain_file_path, problem_file_path])

    # Remove artifacts from an interrupted earlier invocation.
    for artifact_path in (sas_file_path, plan_file_path):
        try:
            os.remove(artifact_path)
        except FileNotFoundError:
            pass

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=fast_downward_timeout_seconds(),
        )
    except subprocess.TimeoutExpired as error:
        raise FastDownwardCapacityError(
            "Fast Downward timed out while preparing the PlanPilot session."
        ) from error

    if result.returncode in {10, 11} or is_proven_unsolvable(result.stdout):
        raise FastDownwardUnsolvableError(
            "Fast Downward proved that the planning task is unsatisfiable."
        )
    if result.returncode == 12:
        raise FastDownwardNoPlanError(
            "Fast Downward completed without finding a plan."
        )
    if result.returncode != 0:
        diagnostic = (result.stderr or result.stdout or "no diagnostic output").strip()
        raise RuntimeError(
            f"Fast Downward execution failed with exit code {result.returncode}: "
            f"{diagnostic}"
        )

    # Translation always needs SAS. A plan file is required only when Fast
    # Downward performed the search itself.
    horizon = (
        len(representative_plan)
        if representative_plan is not None
        else calculate_horizon(plan_file_path)
    )
    if not os.path.isfile(sas_file_path):
        raise RuntimeError("Fast Downward did not produce the requested SAS file.")

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
    except Exception:
        db.session.rollback()
        raise

    return {
        "horizon": horizon,
        "sasFile": sas_file_path,
        "planFile": plan_file_path,
        "cached": False,
    }


def calculate_horizon(plan_file_path):
    try:
        with open(plan_file_path, "r", encoding="utf-8") as file:
            actions = []
            for line_number, raw_line in enumerate(file, start=1):
                line = raw_line.strip()
                if not line or line.startswith(";"):
                    continue
                if not (line.startswith("(") and line.endswith(")")):
                    raise ValueError(
                        f"Malformed Fast Downward plan action at line {line_number}."
                    )
                if not line[1:-1].strip():
                    raise ValueError(
                        f"Empty Fast Downward plan action at line {line_number}."
                    )
                actions.append(line)
    except FileNotFoundError as error:
        raise FastDownwardNoPlanError(
            "Fast Downward did not produce a plan file."
        ) from error

    if not actions:
        raise FastDownwardNoPlanError(
            "Fast Downward did not produce a non-empty plan."
        )
    return len(actions)


def fast_downward_timeout_seconds():
    raw_value = os.environ.get("PLANPILOT_FAST_DOWNWARD_TIMEOUT_SECONDS", "120")
    try:
        return max(int(raw_value), 1)
    except ValueError:
        return 120


def is_proven_unsolvable(output):
    if not output:
        return False
    return any(
        marker in output
        for marker in (
            "Generating unsolvable task",
            "Completely explored state space -- no solution!",
        )
    )
