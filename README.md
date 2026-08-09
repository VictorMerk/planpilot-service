# PlanPilot service

This repository provides a small HTTP service around PlanPilot for IPEXCO. A
session prepares a PDDL task, starts FASB and keeps the selected facets until
the session is stopped or expires.

## Docker

The recommended setup is the Compose configuration in
`IPEXCO-frontend/setup/planpilot-local`.

To run only this service:

```bash
docker build -t planpilot-service:local .
docker run --rm -p 5000:5000 \
  -e API_KEY=development-only \
  planpilot-service:local
```

The bundled FASB executable is built for Linux x86_64. ARM systems need Docker
amd64 emulation.

## API

- `GET /api/health` checks whether the service is running.
- `GET /api/ready` checks whether a new session can be created.
- `GET /api/capabilities` returns the supported options and limits.
- Session routes are available below `/api/sessions`.

Requests, responses, asynchronous jobs and error codes are documented in
[`API.md`](API.md).

The test suite includes route and lifecycle tests. `scripts/smoke_sessions.py`
runs a small real session against a service at `http://127.0.0.1:5000` and is
also executed by CI.

```bash
pip install -r requirements-dev.txt
python -m pytest -q tests lib/planpilot/translate/tests
```

Except for the health and readiness endpoints, requests require:

```text
Authorization: Bearer <API_KEY>
```

## Configuration

The most important environment variables are:

- `API_KEY`
- `PLANPILOT_MAX_HORIZON` (default `100`)
- `PLANPILOT_MAX_ACTIVE_SESSIONS` (default `4`)
- `PLANPILOT_SESSION_TTL_SECONDS` (default `3600`)
- `PLANPILOT_FAST_DOWNWARD_TIMEOUT_SECONDS` (default `120`)
- `PLANPILOT_FASB_RESPONSE_TIMEOUT_SECONDS` (default `30`)
- `PLANPILOT_MAX_QUERY_TIMEOUT_SECONDS` (default `300`)

## Vendored software

PlanPilot, Fast Downward and the FASB binary are included under `lib/`. Their
licenses, upstream repositories and exact revisions are listed in
`THIRD_PARTY.md`. The service repository is distributed under GPL-3.0; see
`LICENSE.md`.
