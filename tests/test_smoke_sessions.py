import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/smoke_sessions.py"
SPEC = importlib.util.spec_from_file_location("smoke_sessions", SCRIPT)
smoke_sessions = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke_sessions)


def test_readiness_check_retries_connection_resets(monkeypatch):
    responses = iter(
        [
            ConnectionResetError("worker is starting"),
            (200, {"status": "ready"}),
        ]
    )

    def request(*_args, **_kwargs):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(smoke_sessions, "request", request)
    monkeypatch.setattr(smoke_sessions.time, "sleep", lambda _seconds: None)

    smoke_sessions.wait_until_ready()
