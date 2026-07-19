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

### `tests/test_genie_pipeline_inactive_durable_state.py` (new, 73 tests (pytest authoritative))

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

## Commits

| Commit | SHA | Description |
|---|---|---|
| Implementation | `de522cea9099f73336a730a2c6313d711d028492` | Block inactive durable conversations in pipeline |
| Validation correction | `6bedf7ffb1aad5728644df19696eec68a0ae617d` | Correct Phase 4C4B2 validation and owner-scope assessment |

## Test Results Summary (authoritative — corrected)

All counts require `rapidfuzz` and `pytest-asyncio` as transient validation-environment
prerequisites. Neither is committed to `requirements.txt`.

| Suite | Files | Tests | Result |
|---|---|---|---|
| Focused 6-file | inactive+lookup+writeback+last_msg+coordinator+session_store | 312 | ✅ pass |
| Phase 4C2A/4C2B/4C3 regression | lookup+chat_plumbing+writeback+last_msg | 168 | ✅ pass |
| Exact 26-file combined (4C4B1 + new file) | 25+1 | 1233 | ✅ pass |
| Complete non-live | all − 4 live files | 2060 | ✅ pass |

**Failed: 0. Skipped: 0. Collection errors: 0.**

Per-file breakdown (authoritative pytest --collect-only):

| File | Tests |
|---|---|
| `test_genie_pipeline_inactive_durable_state.py` | 73 |
| `test_genie_pipeline_durable_lookup.py` | 36 |
| `test_genie_pipeline_durable_writeback.py` | 50 |
| `test_genie_pipeline_last_message_persistence.py` | 57 |
| `test_conversation_reset_coordinator.py` | 49 |
| `test_genie_session_store.py` | 47 |

## Owner / Session Isolation Analysis

### Current process-local key construction

```python
# chat.py
session_id = request.state.session_id             # random httponly cookie
app_conversation_id = f"{session_id}:{frontend_conversation_id}"
```

### Durable key construction

```python
# genie_pipeline.py → _durable_session_lookup
DurableGenieSessionKey(
    owner_user_id_hash=owner_key,           # from X-Forwarded-User
    frontend_conversation_id=frontend_conversation_id,
)
```

### Accurate isolation guarantees

| Scenario | Isolation |
|---|---|
| Separate browser sessions (different cookies) → separate process-local keys | Guaranteed |
| Same cookie + same frontend ID → same process-local key | By design |
| Durable records isolated by `owner_user_id_hash` | Guaranteed |
| Same cookie + **different Databricks principal** (owner change without cookie rotation) | **NOT isolated at process-local layer** |

### Same-cookie/different-owner risk

The session cookie (`SESSION_COOKIE_NAME`) is:
- A random `secrets.token_urlsafe(32)` value, NOT bound to `owner_user_id_hash`.
- Never rotated when `X-Forwarded-User` changes between requests.
- Never cleared or deleted (no logout endpoint exists; `app/main.py` has no
  `delete_cookie` or rotation logic).

Consequence: if user A’s browser cookie persists into user B’s session (e.g., shared machine,
same browser profile without explicit logout, or session reuse), the `app_conversation_id` for
both users would be identical for the same `frontend_conversation_id`. User B would read
User A’s process-local `GenieSessionStore` state (Genie conversation ID, intent, entities).

The durable layer remains correctly isolated (different `owner_user_id_hash` → different durable
key), so no durable record is misrouted. The `_validate_owner_key` guard in the pipeline also
rejects mismatched owners at the point of writeback. However, the process-local cache read and
the initial pipeline dispatch are not owner-checked.

### Existing tests do NOT prove same-cookie/different-owner isolation

`test_multi_user_session_isolation.py` tests different session IDs and different frontend IDs
but contains no test for same-cookie/different-owner. `test_request_owner_identity.py` and
related files test identity extraction but not cookie-to-owner binding.

## Phase 4C4B3 Prerequisite: Owner-Bound Process-Local Key

The reset/chat route must construct a process-local key that includes the trusted owner hash,
conceptually:

```
owner_user_id_hash + ":" + session_id + ":" + frontend_conversation_id
```

or a domain-separated server-side digest of the same components. The owner hash must never be
exposed in HTTP responses, logs, or error messages.

Until Phase 4C4B3 is implemented, Phase 4C4B2’s remove_session and tombstone-race eviction
correctly clean up the process-local state, but that cleanup assumes the process-local key
unambiguously identifies a single owner. If the cookie is shared, cleanup for owner A’s
conversation may also evict owner B’s in-progress state.

## Exit Criteria

- [x] All INACTIVE paths blocked before Genie execution
- [x] Tombstone TOCTOU race detected and handled at writeback
- [x] Degraded lookup fails closed (no ACTIVE snapshot accepted without confirmation)
- [x] Static inactive response exposes no owner hash, frontend ID, Genie IDs (`conversation_id: None`)
- [x] Durable records remain owner-isolated through `owner_user_id_hash`
- [x] No durable record deleted or modified on INACTIVE detection
- [x] `fallback_recommended=False` on all INACTIVE/tombstone paths
- [x] 73 new tests (pytest authoritative) covering all INACTIVE and tombstone-race scenarios
- [x] No regression in existing passing tests (1233 / 2060 pass)
- [x] No live Lakebase connections used
- [x] No HTTP routes added or modified
- [x] No deployment or restart performed
- [!] **CONDITION**: Same-cookie/different-owner process-local isolation gap documented;
       owner-bound key implementation deferred to Phase 4C4B3

## Phase 4C4B2 Status: COMPLETE WITH CONDITIONS

All production contracts are correctly implemented. Validation arithmetic is corrected.
Phase 4C4B3 must implement an owner-bound process-local key before the same-cookie/
different-owner scenario is fully mitigated at the process-local layer.
