# Version Control Status

## Phase 0 — TransparencE Statefix Refactoring

Recorded: 2026-07-18

---

## Status: PASS WITH CONDITION

No Git credentials are configured for this workspace user.
No Databricks Git Folder (Repo) is connected to the development copy or its parent directories.
No `.git` directory was found in any ancestor path up to 6 levels.

Git-based change control **cannot be initialized** without remediation.

## What Was Done Instead

In lieu of a Git baseline commit, the following equivalent controls were established:

1. **`.gitignore`** — created at the root of the development copy.
   Covers: `__pycache__/`, `*.pyc`, `frontend/node_modules/`, `.pytest_cache/`,
   `.env`, secrets, recovery_backups, baseline_backups, `.databricks/`.

2. **`SOURCE_INTEGRITY_BASELINE.json`** — created at the root of the development copy.
   Contains SHA-256 hash and byte size of every file in the development copy as at Phase 0.
   This is the equivalent of a baseline commit: any file that differs from this manifest
   has been modified since Phase 0.

## Blocking Condition

Git-based change control **must be established before Phase 1 implementation begins**.
This is a blocking condition for Phase 1.

### Steps required to unblock:

1. **Create a Databricks Git Folder** connected to an approved Git provider
   (GitHub, GitLab, Azure DevOps, Bitbucket).
   - Path: `/Workspace/Users/lokesh.choraria@te.com/Transparence/transparence_app_statefix`
   - Or: clone into a new path and populate from the active snapshot.

2. **Configure a personal access token** for the Git provider via:
   - Databricks workspace → Settings → Developer → Git credentials.

3. **Create the feature branch**:
   - Branch name: `feature/genie-state-persistence`

4. **Create the baseline commit**:
   - Commit message: `Baseline: active TransparencE deployment before durable Genie state fix`

5. **Record the commit SHA** in this document.

## Interim Procedure (until Git is set up)

Until Git is available:
- Use `SOURCE_INTEGRITY_BASELINE.json` to verify what has changed before and after each edit.
- Use the baseline backup at `baseline_backups/statefix_phase0_20260718T073833Z/` as the
  emergency recovery source.
- Do not make more than one file change per implementation session without verifying the manifest.

## Recommended Branch Strategy

| Branch | Purpose |
|---|---|
| `main` or `master` | Mirrors production workspace at each deployment |
| `feature/genie-state-persistence` | All Phase 1–N statefix development |
| `hotfix/*` | Emergency production fixes only |

All Phase 1 implementation work must happen on `feature/genie-state-persistence`.
No direct pushes to `main` without a pull request and test verification.
