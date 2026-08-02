# Vendored planning software

The solver toolchain is included in this repository so the Docker image can be
built without cloning additional repositories.

| Directory | Project | Upstream revision | License |
| --- | --- | --- | --- |
| `lib/planpilot` | [PlanPilot](https://github.com/abcorrea/planpilot) | `51c6b792b6d602fecccdfda94a442e51dd96be0d` | `lib/planpilot/LICENSE.md` (GPL-3.0) |
| `lib/downward` | [Fast Downward](https://github.com/aibasel/downward) | `a6b98adb939b9fb91eb8f0b5e74eb68a323d65ae` | `lib/downward/LICENSE.md` (GPL-3.0) |
| `lib/planpilot/bin/fasb-x86_64-unknown-linux-gnu` | [FASB](https://github.com/drwadu/fasb) 0.1.2 | distributed with the PlanPilot revision above | `LICENSE` in the same directory (MIT) |

The PlanPilot snapshot matches the listed revision except for the local
`action-per-time-step.lp` update and the added `state-facets.lp` encoding. The
remaining vendored PlanPilot files and the complete Fast Downward directory
match their listed revisions.

The bundled FASB executable has this SHA-256 checksum:

```text
6fe0a70e5187c42f4ce35715739b5a880f89adf866f818841b6a81bf899cba86
```

The vendored directories retain their upstream README and license documents.
