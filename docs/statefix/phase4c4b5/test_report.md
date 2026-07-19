# Phase 4C4B5 — Test Report

Date: 2026-07-20
Branch: feature/genie-state-persistence
Baseline HEAD: 997edf1f4c3c2fc4897552e4886b3af35e8da8f7

## Frontend Build

- **Status**: BLOCKED (carried condition)
- **Reason**: npm binary is a dead symlink (`../lib/node_modules/npm/bin/npm-cli.js` target absent); node_modules directory absent; dist directory absent; no npm cache available; registry downloads prohibited by phase rules.
- **Node**: v22.9.0 available at `/usr/local/bin/node`.
- **Evidence**: package.json present (397 bytes), package-lock.json present (120,162 bytes), source files intact.
- **Conclusion**: Frontend source integrity verified via JavaScript unit tests; production build deferred to deployment environment with npm access.

## Frontend JavaScript Tests

```
Test file: tests/test_frontend_conversation_reset.mjs
Runner: node --test
Tests: 49
Passed: 49
Failed: 0
Skipped: 0
Cancelled: 0
Unhandled rejections: 0
Duration: 128ms
```

## Combined Lifecycle Tests (NEW)

```
Test file: tests/test_conversation_reset_combined_lifecycle.py
Collected: 39
Passed: 39
Failed: 0
Skipped: 0
Collection errors: 0
Duration: 0.50s
```

### Test coverage by scenario:

| Scenario | Tests | Result |
| --- | --- | --- |
| 1. ACTIVE reset | 01-04 | PASS |
| 2. Existing RESET idempotent | 05-06 | PASS |
| 3. Existing STALE | 07-09 | PASS |
| 4. Existing EXPIRED | 10-12 | PASS |
| 5. Missing/tombstone | 13-15 | PASS |
| 6. Different owner isolation | 16-17 | PASS |
| 7. Same cookie/different owner | 18 | PASS |
| 8. Conflict 409 | 19-20 | PASS |
| 9. Durable unavailable 503 | 21-22 | PASS |
| 10. Missing identity 401 | 23-24 | PASS |
| 11. Invalid conversation 400 | 25-26 | PASS |
| 12. Old ID post-reset blocked | 27 | PASS |
| 13. New ID post-reset available | 28-29 | PASS |
| 14. Reset vs writeback race | 30 | PASS |
| 15. Repeated reset idempotent | 31-32 | PASS |
| 16. No prohibited operations | 33-36 | PASS |
| 17. No identifiers in responses | 37-39 | PASS |

## Backend Reset-Related Suite (9 files)

```
Files: 9
Passed: 406
Failed: 0
Skipped: 0
Collection errors: 0
Duration: 4.47s
```

## Expanded Persistence Suite (34 files)

```
Files: 33 baseline + 1 combined lifecycle = 34
Passed: 1559
Failed: 0
Skipped: 0
Collection errors: 0
Duration: 9.39s
```

Note: Baseline was 1371 from the original 30-file suite. Current expanded suite includes 3 additional files added in subsequent phases (Phase 4C4B3A/B) plus the new combined lifecycle file.

## Complete Non-Live Suite

```
Files: 52 (56 total - 4 live exclusions)
Passed: 2237
Failed: 0
Skipped: 0
Collection errors: 0
Duration: 11.14s

Baseline: 2198
Delta: +39 (combined lifecycle tests)
Expected: 2237
Actual: 2237 ✓
```

### Live exclusions:
- tests/test_genie_live_smoke.py
- tests/test_genie_integration_smoke.py
- tests/test_delta_state_live_smoke.py
- tests/test_new_pipeline_live_smoke.py

## Summary

- Zero failures across all suites
- Zero skipped tests
- Zero collection errors
- No production code changes
- No dependency changes
- No deployment
- No live Lakebase connections
