# Phase 3C Exit Assessment

## Phase Name

Disabled Durable Session Runtime Composition Factory

## Verdict

**COMPLETE — Phase 3D gate is open.**

---

## Files Created

| File | Purpose |
|------|---------|
| `app/services/durable_genie_session_runtime_factory.py` | Production composition factory |
| `tests/test_durable_genie_session_runtime_factory.py` | 99 focused tests |
| `docs/statefix/phase3c/runtime_composition_factory.md` | Factory design doc |
| `docs/statefix/phase3c/disabled_flag_policy.md` | Flag policy and disabled-path guarantees |
| `docs/statefix/phase3c/lifecycle_and_failure_policy.md` | Lifecycle ownership and failure handling |
| `docs/statefix/phase3c/test_report.md` | Test results |
| `docs/statefix/phase3c/phase3c_exit_assessment.md` | This document |

## Files Modified

| File | Change |
|------|--------|
| `tests/test_conversation_repository_factory.py` | (a) Isolation test: added approved-importer skip for new factory. (b) Kwarg fix: 20 pre-existing tests renamed from `lakebase_*` to `durable_*` kwargs. |
| `tests/test_durable_genie_session_adapter.py` | Isolation test: added approved-importer skip for new factory. |
| `app.yaml` | Added 3 disabled-by-default Phase 3C flags. |

## Feature-Flag Defaults

- `ENABLE_DURABLE_GENIE_SESSION_ADAPTER = false`
- `CONVERSATION_REPOSITORY_BACKEND = memory`
- `ENABLE_LAKEBASE_CONVERSATION_REPOSITORY = false`

## Test Results

- Focused Phase 3C: **99/99 passed**
- Combined persistence (6 files): **466/466 passed**
- Complete non-live suite: **1377/1377 passed**
- Zero failures, zero collection errors

## Active Runtime Integration

**None.** No existing production file was modified.  The new factory is not
called by `genie_backend_factory.py`, `main.py`, `chat.py`, or any other
runtime module.  The active Genie request path is unchanged.  The running app
was not restarted and was not deployed.

## Safety Confirmations

- No existing production Python file changed.
- No current runtime module imports the new factory.
- `GenieSessionStore` was not modified.
- `genie_backend_factory.py` was not modified.
- `main.py` was not modified.
- `chat.py` was not modified.
- No live Lakebase connection occurred.
- No credential was generated.
- No pool was opened.
- No SQL was executed.
- Nothing was deployed.
- The running app was not restarted.

## Phase 3D Gate

Phase 3D is safe to begin.  It will:

1. Modify `genie_backend_factory.py` to construct a
   `DurableGenieSessionRuntimeFactory` behind the disabled flag.
2. Inject the `GenieSessionStore` singleton as the `cache_store` when
   the factory is enabled.
3. Close the runtime bundle on application shutdown via the FastAPI
   lifespan context in `app/main.py`.
4. Enable end-to-end testing with `ENABLE_DURABLE_GENIE_SESSION_ADAPTER=true`
   and `CONVERSATION_REPOSITORY_BACKEND=lakebase` after verifying the
   Lakebase connection in the Phase 3D test environment.
