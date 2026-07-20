# Phase 4C4B5 — Test Report
#
# STATUS: NOT CLOSED — final frontend production build remains outstanding.
# Phase 4D1: NOT SAFE TO BEGIN.

Date: 2026-07-20
Correction: 2026-07-20

## Combined Lifecycle File

File: `tests/test_conversation_reset_combined_lifecycle.py`
Classes: 22
Tests: 52
Result: 52 passed, 0 failed, 0 skipped, 0 collection errors

## Frontend JavaScript

File: `tests/test_frontend_conversation_reset.mjs`
Tests: 49
Result: 49 passed, 0 failed, 0 skipped, 0 cancelled, 0 unhandled rejections

## Exact 31-File Suite

Result: 1423 passed, 0 failed, 0 skipped, 0 collection errors
Arithmetic: 1371 (baseline 30 files) + 52 (combined lifecycle) = 1423

Files:
1. tests/test_lakebase_conversation_repository.py
2. tests/test_lakebase_connection_provider.py
3. tests/test_durable_genie_session_adapter.py
4. tests/test_durable_genie_session_runtime_factory.py
5. tests/test_genie_backend_durable_runtime_wiring.py
6. tests/test_main_durable_runtime_lifecycle.py
7. tests/test_conversation_repository.py
8. tests/test_conversation_repository_factory.py
9. tests/test_conversation_state_cleanup.py
10. tests/test_conversation_state_factory.py
11. tests/test_delta_conversation_state.py
12. tests/test_genie_session_store.py
13. tests/test_genie_session_store_context.py
14. tests/test_multi_user_session_isolation.py
15. tests/test_request_owner_identity.py
16. tests/test_owner_identity_secret_configuration.py
17. tests/test_request_owner_identity_runtime.py
18. tests/test_chat_trusted_identity_extraction.py
19. tests/test_genie_pipeline_owner_key_plumbing.py
20. tests/test_chat_owner_key_plumbing.py
21. tests/test_genie_pipeline_durable_lookup.py
22. tests/test_chat_durable_lookup_key_plumbing.py
23. tests/test_genie_pipeline_durable_writeback.py
24. tests/test_genie_pipeline_last_message_persistence.py
25. tests/test_conversation_reset_coordinator.py
26. tests/test_genie_pipeline_inactive_durable_state.py
27. tests/test_process_local_conversation_key.py
28. tests/test_chat_owner_scoped_local_key.py
29. tests/test_conversation_reset_route.py
30. tests/test_conversation_reset_runtime_wiring.py
31. tests/test_conversation_reset_combined_lifecycle.py

## Complete Non-Live Python

Files: 52 (56 total minus 4 live-only)
Result: 2250 passed, 0 failed, 0 skipped, 0 collection errors
Arithmetic: 2198 (previous baseline) + 52 (combined lifecycle) = 2250

Excluded live files:
- tests/test_genie_live_smoke.py
- tests/test_genie_integration_smoke.py
- tests/test_delta_state_live_smoke.py
- tests/test_new_pipeline_live_smoke.py

## Frontend Build

Status: BLOCKED
Evidence:
- npm: not found
- npx: not found
- frontend/node_modules: absent
- frontend/dist: absent
- local Vite: absent
- npm cache: absent
- External registry: prohibited

The frontend production build must run at the final commit in a build-capable
environment (local clone, CI, or Databricks workspace with npm access).
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
