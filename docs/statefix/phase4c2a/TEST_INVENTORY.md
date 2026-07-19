# Phase 4C2A Test Inventory

## test_genie_pipeline_durable_lookup.py (47 tests)

### TestDisabledMode (6 tests)
- 01-06: Verify no durable access when bundle is disabled/absent.

### TestEnabledPrerequisites (6 tests)
- 07-12: Verify required inputs when durable is enabled.

### TestLookupHit (12 tests)
- 13-24: Verify successful recovery from durable record.

### TestLookupMiss (5 tests)
- 25-29: Verify correct behavior when no record found.

### TestLookupFailure (7 tests)
- 30-36: Verify fail-closed on repository unavailability/degraded reads.

### TestOwnership (4 tests)
- 37-40: Verify owner-scoped isolation.

### TestRestartSimulation (1 composite test)
- 41: End-to-end restart recovery scenario.

### TestNoWriteEnforcement (6 tests)
- 47-52: Verify no mutating adapter methods called.

## test_chat_durable_lookup_key_plumbing.py (18 tests)

### TestChatDurableLookupKeyPlumbing (18 tests)
- 01-04: Correct key plumbing to pipeline.run().
- 05-09: No unauthorized identity override vectors.
- 10-11: Optional/generated frontend_id handling.
- 12-13: Pipeline selection and response model unchanged.
- 14-16: No durable/repository/lakebase imports in chat.py.
- 17-18: No extra state attributes, signature verification.
