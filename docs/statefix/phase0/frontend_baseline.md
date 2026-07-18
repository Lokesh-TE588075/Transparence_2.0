# Frontend Baseline

## Phase 0 — TransparencE Statefix Refactoring

Recorded: 2026-07-18

---

## Summary

| Check | Result |
|---|---|
| frontend/node_modules absent | YES |
| frontend/src files vs snapshot | 12 / 12 — PERFECT MATCH |
| frontend/src SHA differences | 0 |
| static/index.html present | YES (604 bytes) |
| Referenced JS asset present | YES |
| Referenced CSS asset present | YES |
| Static assets vs snapshot | 3 / 3 — PERFECT MATCH |
| package.json present | YES |
| package-lock.json present | YES |
| Frontend rebuilt in Phase 0 | NO |

## Referenced Assets

| Asset (from index.html) | File path | Size | SHA vs snapshot |
|---|---|---|---|
| /assets/index-CfXOIiFm.js | static/assets/index-CfXOIiFm.js | 686,231 bytes | MATCH |
| /assets/index-Dj05-6G7.css | static/assets/index-Dj05-6G7.css | 17,614 bytes | MATCH |

## Package Identity

- Name: transparence-frontend
- Version: 1.0.0
- Lock file: `frontend/package-lock.json` (120,162 bytes)

## Frontend Build Notes

The frontend was NOT rebuilt during Phase 0.
The pre-built static assets from the active deployed snapshot are used as-is.

To rebuild the frontend in a future phase:
1. Run from the development copy: `cd frontend && npm install && npm run build`
2. Verify `static/assets/` contains the correct built files.
3. Delete `frontend/node_modules/` after the build (CRITICAL — per deployment rules).
4. Confirm the new build matches any expected changes.
5. Do NOT commit generated assets unless approved.

**CRITICAL deployment rule**: Always physically delete `frontend/node_modules/` before deploying.
Running `npm install` recreates it (~50MB). `.databricksignore` alone is insufficient
for SNAPSHOT mode — the platform tries to copy node_modules files and fails.
