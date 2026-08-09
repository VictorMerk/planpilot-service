# PlanPilot service API

The service exposes a JSON API below `/api`. The IPEXCO back-end is the normal
client; browsers should not call this service directly.

All routes except `/health` and `/ready` require the configured service key:

```text
Authorization: Bearer <API_KEY>
Content-Type: application/json
```

## Service status

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Process health and current capacity |
| `GET` | `/ready` | Whether a new session can be accepted |
| `GET` | `/capabilities` | Encodings, features, timeouts and limits |

## Sessions and facets

| Method | Route | Purpose |
| --- | --- | --- |
| `POST` | `/sessions` | Prepare a planning task and create a session |
| `GET` | `/sessions/{sessionId}` | Read current session metadata |
| `DELETE` | `/sessions/{sessionId}` | Stop and remove the session |
| `POST` | `/sessions/{sessionId}/facets/list` | Return current facets; body is `{}` |
| `POST` | `/sessions/{sessionId}/facets/select` | Apply one facet selection |
| `POST` | `/sessions/{sessionId}/facets/apply` | Apply several selections atomically |
| `POST` | `/sessions/{sessionId}/query` | Run a synchronous query |

Create a bounded session:

```json
{
  "task": {
    "domainPddl": "(define (domain ...))",
    "problemPddl": "(define (problem ...))"
  },
  "configuration": {
    "horizon": 10,
    "encoding": "bounded",
    "abstractTimeSteps": false,
    "stateFacets": true
  },
  "source": { "system": "IPEXCO" }
}
```

Apply two choices against the revision last read by the client:

```json
{
  "selections": [
    { "facetId": "occurs(action((\"unstack\",\"a\",\"b\")),1)", "selectionState": "positive" },
    { "facetId": "occurs(action((\"stack\",\"c\",\"b\")),6)", "selectionState": "negative" }
  ],
  "expectedSelectionRevision": 2
}
```

Selection states are `positive`, `negative` and `neutral`. A successful change
increments `selectionRevision`. If another request changed the session first,
the service returns `409 SELECTION_CONFLICT` and leaves the newer state intact.

Synchronous query types are `facets`, `facetCount`, `facetReduction`,
`impliedFacets`, `solution`, `solutionCount`, `solutionReduction` and
`selectionImpact`. Expensive queries accept `timeoutSeconds` within the range
reported by `/capabilities`.

## Asynchronous jobs

| Method | Route | Purpose |
| --- | --- | --- |
| `POST` | `/sessions/{sessionId}/jobs` | Start a cancellable query |
| `GET` | `/sessions/{sessionId}/jobs/{jobId}` | Read status and result |
| `DELETE` | `/sessions/{sessionId}/jobs/{jobId}` | Request cancellation |

Supported job types are `solution`, `solutionCount` and `selectionImpact`.
Only one job may be queued or running in a session. Job status is `queued`,
`running`, `succeeded`, `failed` or `cancelled`.

Example:

```json
{
  "type": "solutionCount",
  "expectedSelectionRevision": 2,
  "timeoutSeconds": 60
}
```

## Error responses

Errors have one shape:

```json
{
  "error": {
    "code": "SELECTION_CONFLICT",
    "message": "Session revision 1 is stale; the current revision is 2. Refresh and retry."
  }
}
```

Common status and error combinations:

| Status | Codes |
| --- | --- |
| `400` | `INVALID_REQUEST` |
| `401` | `UNAUTHORIZED` |
| `404` | `SESSION_NOT_FOUND`, `JOB_NOT_FOUND` |
| `409` | `SELECTION_CONFLICT`, `JOB_CONFLICT`, `NO_PLAN` |
| `410` | `SESSION_EXPIRED` |
| `422` | `TASK_UNSOLVABLE`, `NO_PLAN`, `HORIZON_LIMIT_EXCEEDED` |
| `503` | `PLANPILOT_BUSY`, `PLANPILOT_SESSION_LIMIT`, `PLAN_SPACE_TOO_LARGE` |
| `500` | `PLANPILOT_FAILED` |

Capacity responses include `Retry-After: 5`. Session expiry is extended when a
session is accessed. Limits and default timeouts are returned by
`GET /api/capabilities`.
