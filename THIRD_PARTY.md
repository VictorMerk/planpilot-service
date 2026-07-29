# Vendored planning software

The service currently vendors its solver toolchain so the Docker image can be
built without cloning additional repositories.

| Directory | Project | Upstream | License in this checkout |
| --- | --- | --- | --- |
| `lib/planpilot` | PlanPilot | https://github.com/abcorrea/planpilot | `lib/planpilot/LICENSE.md` (GPL-3.0 text) |
| `lib/downward` | Fast Downward | https://github.com/aibasel/downward | `lib/downward/LICENSE.md` (GPL-3.0 text) |
| `lib/planpilot/bin/fasb-x86_64-unknown-linux-gnu` | FASB | https://github.com/drwadu/fasb | `lib/planpilot/bin/fasb-x86_64-unknown-linux-gnu/LICENSE` (MIT) |

The vendored directories retain their upstream README and license documents.
This repository does not currently record the exact upstream commit for the
PlanPilot and Fast Downward copies. Record those revisions before updating or
redistributing the vendor snapshot; directory names alone are not sufficient
provenance.
