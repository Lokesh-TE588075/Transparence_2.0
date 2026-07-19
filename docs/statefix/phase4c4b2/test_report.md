# Phase 4C4B2 — Test Report

## New Test File

`tests/test_genie_pipeline_inactive_durable_state.py` — 75 tests

### Test Classes

| Class | Tests | Coverage |
|---|---|---|
| `TestInactiveLookupOutcome` | 5 | ACTIVE→RECOVERED, no record→MISS, RESET/STALE/EXPIRED→INACTIVE |
| `TestInactiveNoGenieExecution` | 6 | start_conversation not called, send_message not called, get_or_create not called, fallback_recommended=False |
| `TestInactiveProhibitedAdapterOperations` | 5 | bind, update_last_genie_message, touch, set_status, delete all prohibited for INACTIVE |
| `TestInactiveStaticResponse` | 2 | message=_MSG_INACTIVE, all ChatResponse fields present, no identifier leakage |
| `TestInactiveProcessLocalCleanup` | 3 | current session removed, other sessions untouched, no-entry idempotent |
| `TestDegradedLookupFailsClosed` | 5 | degraded ACTIVE/RESET fails closed, sanitized response, adapter unavailable fails closed |
| `TestTombstoneRaceWriteback` | 8 | RESET/STALE/EXPIRED tombstone after MISS, bind never called, no fallback, static response, session cleared, repo record preserved |
| `TestGetOrCreateDegradedAndUnavailable` | 3 | degraded ACTIVE/RESET fails closed, unavailable fails closed |
| `TestActiveMissPathRetained` | 2 | authoritative ACTIVE get_or_create still proceeds (regression), MISS without bundle unaffected |
| `TestNoIdentifierLeakage` | 3 | owner hash, frontend ID, Genie IDs absent from response and message |

### Constants Used

```python
_VALID_OWNER_A     = "a" * 64
_VALID_OWNER_B     = "b" * 64
_SPACE_ID          = "test-space-4c4b2"
_FRONTEND_ID       = "frontend-4c4b2-001"
_APP_CONV_ID       = "session-abc:frontend-4c4b2-001"
_APP_CONV_ID_2     = "session-xyz:frontend-4c4b2-001"  # different session, same frontend
_APP_CONV_ID_3     = "session-abc:frontend-4c4b2-002"  # same session, different frontend
_MSG_INACTIVE      = "This conversation is no longer active. Start a new chat."
```

## Updated Test Files

### `tests/test_genie_pipeline_durable_lookup.py`

3 narrowly-scoped updates to reflect the new fail-closed policies:

1. `TestNonActiveStatus.test_non_active_is_inactive_blocked` (renamed from `test_non_active_starts_new_conversation`):
   - `status == "inactive"`, `fallback_recommended is False`, `start_calls == 0`, `send_calls == 0`

2. `TestConfirmedDegradedRecovery.test_degraded_confirmed_snapshot_fails_closed` (renamed from `test_degraded_confirmed_snapshot_recovers`):
   - `status == "error"`, `fallback_recommended is False`, `start_calls == 0`, `send_calls == 0`

3. `TestConfirmedDegradedRecovery.test_degraded_mapping_not_restored` (renamed from `test_degraded_mapping_restored`):
   - `store.get_genie_conversation_id(_APP_CONV_ID) is None` (was `== _GENIE_CONV_ID`)

## Test Results

### Focused suites (6 files)

| Suite | Tests | Result |
|---|---|---|
| `test_genie_pipeline_inactive_durable_state.py` | 75 | ✅ PASS |
| `test_genie_pipeline_durable_lookup.py` | 36 | ✅ PASS |
| `test_genie_pipeline_durable_writeback.py` | 48 | ✅ PASS |
| `test_genie_pipeline_last_message_persistence.py` | 57 | ✅ PASS |
| `test_conversation_reset_coordinator.py` | 49 | ✅ PASS |
| `test_genie_session_store.py` | 47 | ✅ PASS |
| **Total** | **312** | **✅ PASS** |

### Full non-live suite (excluding rapidfuzz-dependent and live tests)

- 1847 passed, 36 skipped, 3 pre-existing failures (`test_input_normalizer.py` — rapidfuzz not installed)
- 0 new failures introduced by Phase 4C4B2

## Bugs Fixed During Testing

### Bug 1: `ConversationStatus` not in scope in `_persist_new_durable_conversation`
- `ConversationStatus` is only locally imported inside `_durable_session_lookup`
- My TOCTOU check at the end of `_persist_new_durable_conversation` referenced `ConversationStatus` without importing it
- Fix: added `from app.services.conversation_repository import ConversationStatus as _ConversationStatusWB` to the local imports block in `_persist_new_durable_conversation`

### Bug 2: `conversation_id` leaks `frontend_conversation_id` in inactive response
- `_build_inactive_response` set `conversation_id = app_conversation_id`
- `app_conversation_id = f"{session_id}:{frontend_conversation_id}"` literally contains `frontend_conversation_id` as a suffix
- The no-identifier-leak spec says `frontend_conversation_id` must not appear in the response
- Fix: set `conversation_id: None` in `_build_inactive_response`
