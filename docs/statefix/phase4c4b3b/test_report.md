# Phase 4C4B3B — Test Report

## Remote Synchronization

The initial Phase 4C4B3B implementation was committed locally but the push failed
("Updates were rejected because the remote contains work that you do not have locally").
A `git pull` was performed to reconcile the remote state. The pull completed without
conflicts; the implementation files were present in the post-pull workspace. A correction
commit was then created to strengthen the runtime tests.

## Files Changed in Correction Commit

### Production (no changes)
All production files were already correct from the initial implementation:
- `app/services/conversation_reset_runtime.py`
- `app/routes/conversation_reset.py`
- `app/main.py`

### Tests corrected
- `tests/test_conversation_reset_route.py`
  - `_run()` helper: changed `asyncio.get_event_loop().run_until_complete()` → `asyncio.run()`
  - `test_30`: added behavioural spy on `adapter.delete` (source inspection retained as secondary)
- `tests/test_conversation_reset_runtime_wiring.py`
  - Tests 01–04: corrected patch target from `app.services.conversation_reset_runtime.get_genie_pipeline`
    to `app.services.genie_backend_factory.get_genie_pipeline`
  - Test 07: simplified from re-import pattern to pure source inspection (behavioural proof moved to test 18)
  - Added `import app.services.genie_backend_factory` to `_make_fake_pipeline()` and tests 15–17
    to ensure `pkgutil.resolve_name` can resolve the patch target in isolation
  - Added 8 new behavioural tests (11–18)

### Docs
- `docs/statefix/phase4c4b3b/reset_endpoint_contract.md` (new)
- `docs/statefix/phase4c4b3b/shared_runtime_wiring.md` (new)
- `docs/statefix/phase4c4b3b/test_report.md` (this file, new)
- `docs/statefix/phase4c4b3b/phase4c4b3b_exit_assessment.md` (new)

## Test File Counts

| File | Tests |
|---|---|
| `test_conversation_reset_route.py` | 31 |
| `test_conversation_reset_runtime_wiring.py` | 18 |
| **Phase 4C4B3B total** | **49** |

## Behavioural Test Design (Wiring)

### Identity comparisons
- test_01: `coordinator._session_store is pipeline._store`
- test_04: `coordinator._adapter is pipeline._durable_session_runtime_bundle.adapter`

### Shared-store mutation
- test_02: session inserted in shared store is removed by reset
- test_03: another owner’s session is not removed

### Call-time isolation
- test_11: `get_genie_pipeline` called exactly once
- test_12: `_build_pipeline` never called directly
- test_13: `GenieSessionStore.__init__` never called during coordinator retrieval
- test_14: `DurableGenieSessionAdapter.__init__` never called during coordinator retrieval

### Fail-closed
- test_15: missing pipeline → `ConversationResetRuntimeUnavailableError`
- test_16: missing bundle → `ConversationResetRuntimeUnavailableError`
- test_17: `adapter=None` → `ConversationResetRuntimeUnavailableError`

### Lifecycle
- test_07: source check — `reset_genie_pipeline` in lifespan finally block
- test_18: behavioural — `lifespan()` calls `reset_genie_pipeline` exactly once (via `asyncio.run`)

### Import-time safety
- test_05: source check — no `psycopg.connect` / `ConnectionPool` at app.main module level
- test_06: behavioural — `include_router(conversation_reset.router)` constructs no credentials
- test_08: behavioural — importing `conversation_reset_runtime` creates no `GenieSessionStore`
- test_09: behavioural — reset route registered exactly once in `app.routes`
- test_10: behavioural — `/api/chat` route still registered

## Suite Results

| Suite | Files | Tests | Result |
|---|---|---|---|
| Route + wiring only | 2 | 49 | 49/49 PASS |
| Focused 12-file | 12 | 567 | 567/567 PASS |
| Exact 30-file combined | 30 | 1371 | 1371/1371 PASS |
| Complete non-live | all − 4 live | 2198 | 2198/2198 PASS |

Baseline (pre-4C4B3B): 2149. Net new: +31 route +18 wiring = +49. Total: **2198**.

Zero failures. Zero skipped. Zero collection errors. 4 pre-existing deprecation warnings.

## No Deployment

No deployment occurred. No application restart. No live Lakebase connection.
