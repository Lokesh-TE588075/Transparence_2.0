# Phase 4C2A: Read-Only Durable Conversation Lookup

## Purpose
Enables GeniePipeline to recover an existing Genie conversation from the
durable session repository when the application's in-memory GenieSessionStore
no longer contains the mapping (e.g., after a container restart).

## Key Constraints
- **Read-only**: Only `adapter.load()` is called. No create, bind, update,
  delete, or touch operations are invoked.
- **Fail closed**: If the durable repository is unavailable and no confirmed
  snapshot exists, the pipeline returns an error response with
  `fallback_recommended=False`.
- **Owner-scoped**: Lookup requires both `owner_user_id_hash` and
  `frontend_conversation_id` to construct the `DurableGenieSessionKey`.
- **Status filtering**: Only `ACTIVE` records are recovered. STALE, RESET,
  and EXPIRED records are treated as "not found".
- **Degraded reads rejected**: Results with `degraded=True` are treated as
  unavailable (fail closed).

## Files Changed
- `app/services/genie_pipeline.py` — Added `_durable_session_lookup()` method,
  `_DurableLookupUnavailableError`, `_build_durable_lookup_error_response()`,
  and `frontend_conversation_id` parameter to `run()`.
- `app/routes/chat.py` — Passes `frontend_conversation_id` to pipeline.

## Files Created
- `tests/test_genie_pipeline_durable_lookup.py` — 47 tests
- `tests/test_chat_durable_lookup_key_plumbing.py` — 18 tests

## Boundary Tests Updated
- `tests/test_durable_genie_session_adapter.py` — Allowlisted `genie_pipeline.py`
  as approved importer.
- `tests/test_genie_backend_durable_runtime_wiring.py` — Removed `genie_pipeline.py`
  from "no bundle access" assertion.

## Test Results
1825 non-live tests pass, 1 skipped, 0 failures.
