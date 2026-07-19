# Phase 4C2A Constraints Verification

## C1: No durable writes
VERIFIED: TestNoWriteEnforcement (tests 47-52) installs
`side_effect=AssertionError` on get_or_create, bind_genie_conversation,
update_last_genie_message, set_status, delete, touch. All assertions pass.

## C2: Only adapter.load() called
VERIFIED: test_15 uses MagicMock on adapter.load and asserts call_count==1.
No other adapter methods are called.

## C3: fail-closed on unavailability
VERIFIED: tests 30-36 confirm error response with fallback_recommended=False
when DurableGenieSessionUnavailableError is raised.

## C4: Degraded reads rejected
VERIFIED: test_31 passes a GenieSessionLookupResult with degraded=True and
confirms error response.

## C5: ACTIVE-only recovery
VERIFIED: _durable_session_lookup checks record.status != ACTIVE and returns
(not restores). TestLookupHit tests confirm only ACTIVE records are restored.

## C6: Owner-scoped isolation
VERIFIED: tests 37-40 confirm different owners with same frontend_id get
independent results.

## C7: No PII in logs or responses
VERIFIED: tests 22-24 confirm frontend_id not in durable log messages,
owner_key not in logs, and responses don't expose ownership fields.

## C8: No fallback to custom pipeline
VERIFIED: All error paths set fallback_recommended=False.

## C9: Session store hydration uses app_conversation_id
VERIFIED: test_20 confirms context stored under app_conversation_id, not
frontend_conversation_id.

## C10: Existing tests unbroken
VERIFIED: 1825 non-live tests pass (up from 1760 baseline + 65 new tests).
