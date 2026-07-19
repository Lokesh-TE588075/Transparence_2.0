# Test Report

## Test Execution Summary

| Suite | Collected | Passed | Failed | Skipped |
|-------|-----------|--------|--------|---------|
| test_genie_pipeline_durable_lookup.py | 36 | 36 | 0 | 0 |
| test_chat_durable_lookup_key_plumbing.py | 17 | 17 | 0 | 0 |
| Phase 4C1 + 4C2A focused (7 files) | 305 | 305 | 0 | 0 |
| Complete non-live suite (43 files) | 1813 | 1813 | 0 | 0 |

## Excluded Live Suites (exactly 4)

- tests/test_genie_live_smoke.py
- tests/test_genie_integration_smoke.py
- tests/test_delta_state_live_smoke.py
- tests/test_new_pipeline_live_smoke.py

## Pipeline Durable Lookup Tests (36 tests)

### TestDisabledMode (5 tests)
- Disabled with no bundle, disabled bundle attached, owner/frontend optional, adapter never accessed.

### TestEnabledPrerequisites (4 tests)
- Missing owner_key, missing frontend_id, empty frontend_id, invalid owner_key — all error with zero Genie calls.

### TestLookupHitRecovery (6 tests)
- Recovery uses send_message on recovered conv.
- Current message sent unchanged.
- Mapping restored in store.
- No durable mutations.
- Response has no owner hash.
- fallback_recommended=False.

### TestEmptyMemoryRestart (2 tests)
- True empty-store restart: send_message called exactly once on recovered conv, start_conversation zero, no preloaded context.
- adapter.load called exactly once.

### TestStandaloneMessageRecovery (3 tests)
- "Show shipments from Germany" → send_message on recovered conv, start_conversation not called.
- Proves recovery is authoritative regardless of local classification.

### TestLookupMiss (3 tests)
- start_conversation called exactly once, send_message zero, no durable mutations.

### TestLookupFailure (3 tests)
- Unavailable fails closed, unexpected exception fails closed, sanitized response.

### TestConfirmedDegradedRecovery (2 tests)
- Confirmed degraded snapshot → send_message on recovered conv, mapping restored.

### TestOwnership (2 tests)
- Different owner gets miss (isolation), email as owner_key rejected.

### TestShapeRetryRecovery (1 test)
- Shape retry does not start new conversation on recovered request.

### TestNonActiveStatus (3 parametrized tests)
- STALE/RESET/EXPIRED → start_conversation (treated as miss).

### TestNoWriteEnforcement (2 tests)
- Miss: all mutation methods never called.
- Hit: all mutation methods never called.

## Chat Durable Lookup Tests (17 tests)

- Pipeline signature verification (frontend_conversation_id kwarg, keyword-only, default None).
- Source inspection: frontend_id passed exactly once, owner_key passed exactly once.
- app_conversation_id format verification.
- ChatRequest has no owner_key field.
- ChatResponse has required fields.
- No durable adapter/repository/lakebase imports in chat.py.
- Owner key derived from trusted identity.
- Frontend ID from body or generated.
- No legacy email/Authorization in pipeline call.
- Chat does not call adapter directly.

## No Deployment

No application deployment or restart was performed.
