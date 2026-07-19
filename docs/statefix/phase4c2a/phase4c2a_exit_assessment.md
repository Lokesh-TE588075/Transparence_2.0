# Phase 4C2A Exit Assessment

## Status: PASS

## Acceptance Criteria Met

1. **Empty-memory restart recovery** — Container restart simulation with
   completely fresh GenieSessionStore (zero mappings, zero context, zero
   last_intent) successfully recovers Genie conversation via durable lookup.

2. **send_message on recovery** — Recovered mapping causes exactly one
   `send_message()` call on the pre-existing Genie conversation.

3. **Zero start_conversation on recovery** — No new Genie conversation is
   started when a durable record is recovered.

4. **Recovered mapping bypasses local reset** — The `_durable_recovered` flag
   prevents `_run_inner()` Step 3 from resetting the mapping, even when the
   local router does not classify the prompt as TRUE_FOLLOW_UP.

5. **Shape retry preservation** — Shape-validation retries on recovered
   requests do not reset the mapping and do not start a new conversation.

6. **Confirmed degraded snapshot recovery** — Degraded reads from adapter-local
   confirmed snapshots are accepted as valid recovery sources.

7. **No durable writes** — Only `adapter.load()` is called. No create, bind,
   update, touch, status change, or delete occurs.

8. **Fail-closed on unavailable** — DurableGenieSessionUnavailableError
   produces error response with fallback_recommended=False.

## Production Files Modified

- `app/services/genie_pipeline.py` — Added `_DurableLookupOutcome` enum,
  refactored `_durable_session_lookup()` to return outcome, added
  `_durable_recovered` flag to `_run_inner()`, protected recovered mapping
  in Step 3 and shape retry.
- `app/routes/chat.py` — Passes `frontend_conversation_id=frontend_conversation_id`
  to `_genie_pl.run()`.

## What Was NOT Changed

- `app/services/durable_genie_session_adapter.py` — Untouched.
- `app/services/conversation_repository.py` — Untouched.
- `app/services/durable_genie_session_runtime_factory.py` — Untouched.
- `app.yaml` — Untouched.
- `requirements.txt` — Untouched.

## Test Results

- Pipeline durable tests: 36 passed
- Chat plumbing tests: 17 passed
- Complete non-live suite: 1813 passed, 0 failed, 0 skipped

## Phase 4C2B Status

Phase 4C2B (write-back after Genie interaction) remains disabled.
No durable write method exists in the production diff.
