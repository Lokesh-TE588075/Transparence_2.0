# Phase 4C4B2 — Exit Assessment

## Phase Summary

Phase 4C4B2 adds Pipeline Inactive-State Blocking and Reset-Tombstone Writeback
Protection to the Genie pipeline. It is the second sub-phase of Phase 4C4B
(Pipeline Durable Session Protection).

## Changes Made

### `app/services/genie_pipeline.py`

1. **`_MSG_INACTIVE_CONVERSATION`** (line 167): user-facing message for blocked conversations.
2. **`_DurableInactiveConversationError`** (line 216): distinct exception for TOCTOU tombstone races, separate from `_DurableWritebackError`.
3. **`_DurableLookupOutcome.INACTIVE`** (line 244): fourth lookup outcome for authoritative non-ACTIVE records.
4. **`_DurableRequestContext` docstring** (lines 255–261): documents INACTIVE outcome and key/record semantics.
5. **`_durable_session_lookup` — degraded policy** (line ~1509): degraded results now raise `_DurableLookupUnavailableError` (fail closed).
6. **`_durable_session_lookup` — INACTIVE classification** (line ~1518): non-ACTIVE records return `INACTIVE` outcome instead of `MISS`.
7. **INACTIVE block in `run()`** (lines 452–460): before `_run_inner`, blocks INACTIVE requests with `remove_session` + `_build_inactive_response`.
8. **`_build_inactive_response()`** (line 1582): static sanitized response, `conversation_id: None`, no identifier exposure.
9. **TOCTOU gate in `_persist_new_durable_conversation`** (lines 1860–1875): rejects degraded and non-ACTIVE `get_or_create` results.
10. **`_DurableInactiveConversationError` handler** (lines ~1706–1715): tombstone-race path uses `remove_session` + `_build_inactive_response`.

### `tests/test_genie_pipeline_inactive_durable_state.py` (new, 75 tests)

Comprehensive test coverage for all INACTIVE and tombstone-race paths.

### `tests/test_genie_pipeline_durable_lookup.py` (3 narrow updates)

Updated 3 tests to reflect fail-closed behaviour for degraded and non-ACTIVE lookups.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Non-ACTIVE classification | INACTIVE (not MISS) | MISS allows new Genie execution; tombstones must block |
| Degraded-read policy | Fail closed | RESET tombstone could exist while repo was down |
| INACTIVE block position | Before `_run_inner` | Zero Genie execution on INACTIVE |
| Process-local cleanup (Boundary 1) | `remove_session` | Full eviction; no context is valid for reset conversation |
| Process-local cleanup (Boundary 2) | `remove_session` | Tombstone race; no context is valid |
| Ordinary writeback error cleanup | `reset_genie_mapping` | Preserve context for potential retry |
| `conversation_id` in inactive response | `None` | `app_conversation_id` contains `frontend_conversation_id` as suffix |
| `_DurableInactiveConversationError` order | Before `_DurableWritebackError` | Tombstone race has different eviction semantics |

## Files Not Changed

- `main.py` — no HTTP reset route
- Frontend code — no changes
- `chat.py` — no changes (feature flag wiring already correct)
- `durable_genie_session_adapter.py` — no changes
- `conversation_repository.py` — no changes
- `conversation_reset_coordinator.py` — no changes

## Test Results Summary

- 159 tests pass in 3 core Phase 4C4B2 test files
- 153 tests pass in 3 supporting Phase 4C4B1 test files
- 1847 tests pass in full non-live suite
- 3 pre-existing `rapidfuzz` failures unrelated to this phase

## Exit Criteria

- [x] All INACTIVE paths blocked before Genie execution
- [x] Tombstone TOCTOU race detected and handled at writeback
- [x] Degraded lookup fails closed (no ACTIVE snapshot accepted without confirmation)
- [x] Static inactive response exposes no owner hash, frontend ID, Genie IDs
- [x] Owner isolation preserved (remove_session scoped to current user's session cookie)
- [x] No durable record deleted or modified on INACTIVE detection
- [x] `fallback_recommended=False` on all INACTIVE/tombstone paths
- [x] 75 new tests covering all INACTIVE and tombstone-race scenarios
- [x] No regression in existing passing tests
- [x] No live Lakebase connections used
- [x] No HTTP routes added or modified
- [x] No deployment or restart performed

## Phase 4C4B2 Status: COMPLETE
