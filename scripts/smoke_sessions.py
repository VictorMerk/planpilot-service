#!/usr/bin/env python3
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = os.environ.get("PLANPILOT_URL", "http://127.0.0.1:5000").rstrip("/")
API_KEY = os.environ.get("API_KEY", "test-planpilot-key")


def request(method, path, body=None, authenticated=True):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if authenticated:
        headers["Authorization"] = f"Bearer {API_KEY}"
    request_data = Request(
        f"{BASE_URL}{path}",
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with urlopen(request_data, timeout=360) as response:
            text = response.read().decode("utf-8")
            return response.status, json.loads(text) if text else None
    except HTTPError as error:
        text = error.read().decode("utf-8")
        return error.code, json.loads(text) if text else None


def wait_until_ready():
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            status, _body = request("GET", "/api/ready", authenticated=False)
            if status == 200:
                return
        except URLError:
            pass
        time.sleep(1)
    raise RuntimeError("PlanPilot did not become ready within 60 seconds.")


def planning_task():
    domain = (ROOT / "lib/planpilot/benchmarks/blocks/domain.pddl").read_text()
    problem = (ROOT / "lib/planpilot/benchmarks/blocks/demo-1.pddl").read_text()
    return {
        "task": {"domainPddl": domain, "problemPddl": problem},
        "configuration": {
            "horizon": 12,
            "encoding": "exact",
            "abstractTimeSteps": False,
            "stateFacets": False,
        },
        "source": {"system": "IPEXCO"},
    }


def require_status(result, expected, label):
    status, body = result
    if status != expected:
        raise RuntimeError(f"{label}: expected HTTP {expected}, got {status}: {body}")
    return body


def main():
    wait_until_ready()
    health = require_status(request("GET", "/api/health", authenticated=False), 200, "health")
    if health.get("status") != "ok":
        raise RuntimeError(f"unexpected health response: {health}")

    unauthorized = require_status(
        request("GET", "/api/capabilities", authenticated=False),
        401,
        "authentication",
    )
    if unauthorized.get("error", {}).get("code") != "UNAUTHORIZED":
        raise RuntimeError(f"unexpected authentication error: {unauthorized}")

    capabilities = require_status(
        request("GET", "/api/capabilities"),
        200,
        "capabilities",
    )
    if not {"exact", "bounded"}.issubset(capabilities.get("encodings", [])):
        raise RuntimeError(f"missing encodings: {capabilities}")

    invalid = require_status(request("POST", "/api/sessions", {}), 400, "invalid request")
    if invalid.get("error", {}).get("code") != "INVALID_REQUEST":
        raise RuntimeError(f"unexpected validation error: {invalid}")

    created = require_status(
        request("POST", "/api/sessions", planning_task()),
        201,
        "create session",
    )
    session_id = created["sessionId"]
    try:
        facets = created.get("facets", [])
        selectable = next(
            (
                facet
                for facet in facets
                if facet.get("selectable") is True
                and facet.get("selectionState") == "neutral"
            ),
            None,
        )
        if selectable is None:
            raise RuntimeError("session returned no selectable neutral facet")

        selected = require_status(
            request(
                "POST",
                f"/api/sessions/{session_id}/facets/select",
                {
                    "facetId": selectable["id"],
                    "selectionState": "positive",
                    "expectedSelectionRevision": 0,
                },
            ),
            200,
            "select facet",
        )
        if selected.get("selectionRevision") != 1:
            raise RuntimeError(f"selection revision did not advance: {selected}")

        stale = require_status(
            request(
                "POST",
                f"/api/sessions/{session_id}/facets/select",
                {
                    "facetId": selectable["id"],
                    "selectionState": "neutral",
                    "expectedSelectionRevision": 0,
                },
            ),
            409,
            "stale selection",
        )
        if stale.get("error", {}).get("code") != "SELECTION_CONFLICT":
            raise RuntimeError(f"unexpected stale revision error: {stale}")

        count = require_status(
            request(
                "POST",
                f"/api/sessions/{session_id}/query",
                {"type": "solutionCount", "timeoutSeconds": 60},
            ),
            200,
            "solution count",
        )
        if not isinstance(count.get("result", {}).get("value"), int):
            raise RuntimeError(f"invalid solution count: {count}")
    finally:
        stopped = require_status(
            request("DELETE", f"/api/sessions/{session_id}"),
            200,
            "stop session",
        )
        if stopped.get("status") != "stopped":
            raise RuntimeError(f"session did not stop: {stopped}")

    print("PlanPilot API smoke test passed.")


if __name__ == "__main__":
    main()
