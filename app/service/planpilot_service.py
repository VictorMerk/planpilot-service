import os
import re
import time
import subprocess
import threading
import logging
import tempfile
from typing import List, Dict
from ..persistence.models import FastDownwardRequest
from ..utils.parsing import (
    parse_facet_output,
    parse_solution_output,
)


logger = logging.getLogger(__name__)
_lp_locks = {}
_lp_locks_guard = threading.Lock()


class PlanpilotNoPlanError(RuntimeError):
    pass


class PlanpilotCapacityError(RuntimeError):
    pass


class PlanpilotService:
    def __init__(self):
        self.process = None
        self.lock = threading.RLock()
        self.output_buffer = []
        self.reader_thread = None
        self.horizon = 0

        self.last_sas_file_path = None
        self.last_hash_value = None
        self.last_encoding = None
        self.last_abs_steps = None

    def run_planpilot_service(
        self, sas_file: str, horizon: int, encoding: str, abstract_time_steps: bool
    ) -> List[Dict]:
        if type(horizon) is not int or horizon <= 0:
            raise ValueError("PlanPilot horizon must be a positive integer.")
        if horizon > planpilot_max_horizon():
            raise ValueError(
                f"PlanPilot horizon must not exceed {planpilot_max_horizon()}."
            )
        if encoding not in {"exact", "bounded"}:
            raise ValueError("PlanPilot encoding must be exact or bounded.")
        if type(abstract_time_steps) is not bool:
            raise ValueError("abstract_time_steps must be a boolean.")

        self.horizon = 0

        request_data = FastDownwardRequest.query.filter_by(
            sas_file_path=sas_file
        ).first()
        if not request_data:
            raise ValueError("SAS file not found in the database.")

        hash_value = request_data.hash_value
        sas_file_path = request_data.sas_file_path

        current_directory = os.getcwd()
        lp_file_path = self._lp_file_path(
            current_directory,
            hash_value,
            encoding,
            abstract_time_steps,
        )
        self._ensure_lp_file(
            sas_file_path,
            lp_file_path,
            encoding,
            abstract_time_steps,
        )

        self._stop_current_process()

        fasb_binary = os.path.join(
            current_directory,
            "lib",
            "planpilot",
            "bin",
            "fasb-x86_64-unknown-linux-gnu",
            "fasb",
        )
        fasb_command = [
            "stdbuf",
            "-oL",
            fasb_binary,
            lp_file_path,
            "-c",
            f"horizon={horizon}",
            "0",
        ]

        with self.lock:
            self.output_buffer = []
            self.process = subprocess.Popen(
                fasb_command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            self.reader_thread = threading.Thread(
                target=self._read_stdout,
                args=(self.process, self.output_buffer),
                daemon=True,
            )
            self.reader_thread.start()

        self.horizon = horizon

        self.last_sas_file_path = sas_file_path
        self.last_hash_value = hash_value
        self.last_encoding = encoding
        self.last_abs_steps = abstract_time_steps

        self._wait_for_fasb_ready()
        output = self.send_command("?")
        return output

    def send_command(
        self,
        command: str,
        no_Output: bool = False,
        timeout_seconds: float = None,
    ) -> str:
        if not self.process:
            raise RuntimeError("FASB process not running")

        with self.lock:
            try:
                # FASB prints no prompt when stdout is piped. The count query
                # marks the end of the response.
                self.output_buffer.clear()
                normalized_command = command.strip()
                expected_numeric_lines = fasb_expected_numeric_lines(
                    normalized_command
                )
                self.process.stdin.write(command + "\n#?\n")
                self.process.stdin.flush()
                response_timeout = (
                    fasb_response_timeout_seconds()
                    if timeout_seconds is None
                    else max(float(timeout_seconds), 0.1)
                )
                response_started_at = time.monotonic()

                while count_numeric_response_lines(self.output_buffer) < expected_numeric_lines:
                    poll = getattr(self.process, "poll", None)
                    if poll is not None and poll() is not None:
                        diagnostic = "".join(self.output_buffer[-20:]).strip()
                        self._stop_current_process()
                        raise RuntimeError(
                            "FASB stopped before answering"
                            + (f": {diagnostic}" if diagnostic else ".")
                        )
                    if time.monotonic() - response_started_at >= response_timeout:
                        raise TimeoutError(
                            f"FASB did not answer '{command}' within {response_timeout:g} seconds."
                        )
                    time.sleep(0.05)

                response_lines = list(self.output_buffer)
                marker_index = last_numeric_response_index(response_lines)
                command_output = response_lines[:marker_index]
                output_str = "".join(command_output)

                if normalized_command.startswith(("+", "-")):
                    if no_Output:
                        return None
                    return self.send_command("?")

                if command.startswith(("?", "#??", "#!!", "|= %", "|=")):
                    return parse_facet_output(output_str, command)

                if re.match(r"!\s*\d*$", command.strip()):
                    return parse_solution_output(output_str)

                return output_str

            except PlanpilotCapacityError:
                self._terminate_process(self.process)
                self.process = None
                raise
            except TimeoutError as e:
                self._terminate_process(self.process)
                self.process = None
                raise PlanpilotCapacityError(str(e))
            except BrokenPipeError:
                self._stop_current_process()
                raise RuntimeError("FASB process closed the pipe unexpectedly.")
            except Exception as e:
                raise RuntimeError(
                    f"Unexpected error communicating with FASB or parsing output: {e}"
                )

    def get_representative_solution(self, required=False, timeout_seconds=None):
        solutions = (
            self.send_command("! 1")
            if timeout_seconds is None
            else self.send_command("! 1", timeout_seconds=timeout_seconds)
        )
        solution = self._validate_representative_solution(solutions)
        if solution is None and required:
            raise PlanpilotNoPlanError(
                "PlanPilot found no non-empty plan for the requested horizon and encoding."
            )
        return solution

    def _validate_representative_solution(self, solutions):
        if not solutions:
            return None

        concrete_facets = [
            facet
            for facet in solutions[0].get("facets", [])
            if facet.get("id", "").startswith("occurs(action(")
        ]
        if not concrete_facets:
            return None

        timesteps = [facet.get("timestep") for facet in concrete_facets]
        if any(type(timestep) is not int or not 1 <= timestep <= self.horizon for timestep in timesteps):
            raise RuntimeError("PlanPilot returned a plan action outside the configured horizon.")
        if len(set(timesteps)) != len(timesteps):
            raise RuntimeError("PlanPilot returned more than one action at a sequential timestep.")
        if self.last_encoding == "exact" and set(timesteps) != set(
            range(1, self.horizon + 1)
        ):
            raise RuntimeError(
                "PlanPilot's exact encoding returned an incomplete horizon."
            )
        return {
            "label": solutions[0].get("label", "solution 1"),
            "facets": sorted(concrete_facets, key=lambda facet: facet["timestep"]),
        }

    def restart_FASB(self, timeout_seconds=None):
        if (
            self.last_sas_file_path is None
            or self.last_hash_value is None
            or self.last_encoding is None
            or self.last_abs_steps is None
            or self.horizon is None
        ):
            raise RuntimeError("Cannot restart solver: missing cached metadata.")

        current_directory = os.getcwd()
        lp_file_path = self._lp_file_path(
            current_directory,
            self.last_hash_value,
            self.last_encoding,
            self.last_abs_steps,
        )
        self._ensure_lp_file(
            self.last_sas_file_path,
            lp_file_path,
            self.last_encoding,
            self.last_abs_steps,
        )

        self._stop_current_process()

        fasb_binary = os.path.join(
            current_directory,
            "lib",
            "planpilot",
            "bin",
            "fasb-x86_64-unknown-linux-gnu",
            "fasb",
        )

        fasb_command = [
            "stdbuf",
            "-oL",
            fasb_binary,
            lp_file_path,
            "-c",
            f"horizon={self.horizon}",
            "0",
        ]

        with self.lock:
            self.output_buffer = []
            self.process = subprocess.Popen(
                fasb_command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            self.reader_thread = threading.Thread(
                target=self._read_stdout,
                args=(self.process, self.output_buffer),
                daemon=True,
            )
            self.reader_thread.start()

        self._wait_for_fasb_ready(timeout_seconds)

        return self.process

    def stop_fasb(self):
        self._stop_current_process()

    def _stop_current_process(self):
        process = self.process
        reader_thread = self.reader_thread
        self.process = None
        self.reader_thread = None

        if process:
            self._terminate_process(process)
        if reader_thread and reader_thread is not threading.current_thread():
            reader_thread.join(timeout=1)

    @staticmethod
    def _read_stdout(process, output_buffer):
        """Read one process so an old reader cannot consume output after a restart."""
        while process.stdout:
            line = process.stdout.readline()
            if not line:
                break
            output_buffer.append(line)

    def _wait_for_fasb_ready(self, timeout: float = None) -> None:
        timeout = fasb_response_timeout_seconds() if timeout is None else timeout
        timeout = max(float(timeout), 0.1)
        start_time = time.time()
        while time.time() - start_time < timeout:
            with self.lock:
                for line in self.output_buffer:
                    if "fasb v" in line:
                        return
            time.sleep(0.1)
        self._stop_current_process()
        raise PlanpilotCapacityError("FASB did not become ready in time.")

    def _generate_lp_with_plasp(
        self,
        sas_or_pddl_path: str,
        lp_output_path: str,
        encoding_type: str = "exact",
        is_pddl_instance: bool = False,
        domain_file: str = None,
        abstract_time_steps: bool = False,
    ):
        current_directory = os.getcwd()
        plasp_binary = os.path.join(
            current_directory, "lib", "planpilot", "bin", "plasp"
        )

        encoding_dir = os.path.join(current_directory, "lib", "planpilot", "encodings")
        encoding_file = os.path.join(
            encoding_dir,
            (
                "exact-sequential-horizon.lp"
                if encoding_type == "exact"
                else "bounded-sequential-horizon.lp"
            ),
        )
        time_file = os.path.join(
            encoding_dir,
            (
                "abstract-time-steps.lp"
                if abstract_time_steps
                else "action-per-time-step.lp"
            ),
        )

        command = [plasp_binary, "translate"]
        if is_pddl_instance:
            if not domain_file:
                raise ValueError("Domain file is required for PDDL input.")
            command.extend([domain_file, sas_or_pddl_path])
        else:
            command.append(sas_or_pddl_path)

        with open(lp_output_path, "w") as lp_file:
            with open(encoding_file, "r") as ef:
                lp_file.write(ef.read())
            with open(time_file, "r") as tf:
                lp_file.write(tf.read())

            try:
                result = subprocess.run(
                    command,
                    stdout=lp_file,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=plasp_timeout_seconds(),
                )
            except subprocess.TimeoutExpired as error:
                raise PlanpilotCapacityError(
                    "plasp timed out while preparing PlanPilot."
                ) from error

        if result.returncode != 0:
            raise RuntimeError(f"plasp failed:\n{result.stderr}")

    def _ensure_lp_file(
        self,
        sas_file_path: str,
        lp_file_path: str,
        encoding: str,
        abstract_time_steps: bool,
    ) -> None:
        """Build an LP cache file without publishing partial output."""
        os.makedirs(os.path.dirname(lp_file_path), exist_ok=True)
        with _lp_locks_guard:
            path_lock = _lp_locks.setdefault(lp_file_path, threading.Lock())

        with path_lock:
            if os.path.isfile(lp_file_path) and os.path.getsize(lp_file_path) > 0:
                return

            temporary = tempfile.NamedTemporaryFile(
                prefix=".planpilot-",
                suffix=".lp",
                dir=os.path.dirname(lp_file_path),
                delete=False,
            )
            temporary_path = temporary.name
            temporary.close()
            try:
                self._generate_lp_with_plasp(
                    sas_or_pddl_path=sas_file_path,
                    lp_output_path=temporary_path,
                    encoding_type=encoding,
                    abstract_time_steps=abstract_time_steps,
                    is_pddl_instance=False,
                )
                if os.path.getsize(temporary_path) == 0:
                    raise RuntimeError("plasp produced an empty program.")
                os.replace(temporary_path, lp_file_path)
            finally:
                if os.path.exists(temporary_path):
                    os.remove(temporary_path)

    @staticmethod
    def _lp_file_path(
        current_directory: str,
        hash_value: str,
        encoding: str,
        abstract_time_steps: bool,
    ) -> str:
        time_mode = "abstract" if abstract_time_steps else "concrete"
        return os.path.join(
            current_directory,
            "temp",
            hash_value,
            f"output-{encoding}-{time_mode}.lp",
        )

    def _terminate_process(self, proc: subprocess.Popen):
        if proc.poll() is None:  # process is still running
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)  # wait for graceful exit
                except subprocess.TimeoutExpired:
                    proc.kill()  # force kill if it doesn’t exit
                    proc.wait()
            except Exception as e:
                logger.warning("Failed to terminate FASB process: %s", e)


def normalized_numeric_response(line):
    stripped = line.strip()
    while stripped.startswith("::"):
        stripped = stripped[2:].strip()
    return stripped if stripped.isdecimal() else None


def fasb_expected_numeric_lines(command):
    """Count numeric lines from the command and response marker."""
    if command in {"#!", "#?"}:
        return 2
    if (
        command in {"?", "#??", "#!!", "|= %", "!"}
        or re.fullmatch(r"!\s+\d+", command)
        or command.startswith(("+ ", "- "))
    ):
        return 1
    raise ValueError(f"Unsupported FASB command: {command!r}")


def count_numeric_response_lines(lines):
    return sum(normalized_numeric_response(line) is not None for line in lines)


def last_numeric_response_index(lines):
    for index in range(len(lines) - 1, -1, -1):
        if normalized_numeric_response(lines[index]) is not None:
            return index
    raise RuntimeError("FASB response marker was not found.")


def fasb_response_timeout_seconds():
    # Keep this below the backend request timeout.
    raw_value = os.environ.get("PLANPILOT_FASB_RESPONSE_TIMEOUT_SECONDS", "30")
    try:
        return min(max(float(raw_value), 1.0), 150.0)
    except ValueError:
        return 30.0


def fasb_impact_timeout_seconds():
    raw_value = os.environ.get("PLANPILOT_FASB_IMPACT_TIMEOUT_SECONDS", "5")
    try:
        return min(
            max(float(raw_value), 0.5),
            fasb_response_timeout_seconds(),
        )
    except ValueError:
        return min(5.0, fasb_response_timeout_seconds())


def planpilot_max_horizon():
    raw_value = os.environ.get("PLANPILOT_MAX_HORIZON", "100")
    try:
        return min(max(int(raw_value), 1), 100)
    except ValueError:
        return 100


def plasp_timeout_seconds():
    raw_value = os.environ.get("PLANPILOT_PLASP_TIMEOUT_SECONDS", "120")
    try:
        return max(int(raw_value), 1)
    except ValueError:
        return 120
