# Active Deployment Baseline

## Phase 0 — TransparencE Statefix Refactoring

Recorded: 2026-07-18

---

## Deployment Details

| Field | Value |
|---|---|
| App name | transparence |
| Active deployment ID | 01f181110277111f8f8d22379e477ecc |
| Deployment status | SUCCEEDED |
| Application status | RUNNING |
| Compute status | ACTIVE |
| Deployment mode | SNAPSHOT |
| Deployment created | 2026-07-16T12:22:28Z |
| Deployment updated | 2026-07-16T12:23:17Z |
| Deployment message | App started successfully |
| Active snapshot path | /Workspace/Users/488a0acb-5804-42f0-98b1-a02cc13c4573/src/01f181110277111f8f8d22379e477ecc |
| Production workspace source | /Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app |
| Application URL | https://transparence-4310366453016539.aws.databricksapps.com |
| Service principal name | app-31pcl9 transparence |
| Service principal ID | 78664835752275 |
| Workspace ID | 4310366453016539 |

## Confirmation

- The deployment ID `01f181110277111f8f8d22379e477ecc` matches the previously verified active deployment.
- The app was not restarted, stopped, or redeployed during Phase 0.
- The active deployed snapshot is the authoritative source of truth for the development baseline.

## Production Workspace vs Snapshot Delta

Two files differ between the production workspace and the active deployed snapshot.
These are intentional post-deployment edits made to the production workspace after the last deploy.
The development copy uses the **snapshot versions** of these files.

| File | Snapshot size | Production size | Nature of change |
|---|---|---|---|
| app/services/diagnostic_trace.py | 638 lines / 31,701 bytes | 852 lines / 31,211 bytes | Phase 1B rewrite: append-only event model, HMAC-SHA256, DiagnosticTraceEvent dataclass |
| app/services/diagnostic_trace_store.py | 297 lines / 10,213 bytes | 464 lines / 17,271 bytes | Phase 1B rewrite: ABC-based DiagnosticTraceStore, queue-backed writes, 12-column schema |

The Phase 1B versions in the production workspace are **NOT** present in the development copy.
They must be carried forward as a separate sub-task before any Phase 1 implementation that depends on them.
