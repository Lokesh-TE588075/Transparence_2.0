# Phase 4C3 — Test Report

## Focused Phase 4C3 Result

```
tests/test_genie_pipeline_last_message_persistence.py
57 passed, 0 failed, 0 skipped, 0 collection errors
```

## Focused Phase 4C2A + 4C2B + 4C3 Result

Files:
- `tests/test_genie_pipeline_durable_lookup.py` (Phase 4C2A) — 36
- `tests/test_chat_durable_lookup_key_plumbing.py` (Phase 4C2A) — 25
- `tests/test_genie_pipeline_durable_writeback.py` (Phase 4C2B) — 50
- `tests/test_genie_pipeline_last_message_persistence.py` (Phase 4C3) — 57

Arithmetic: 36 + 25 + 50 + 57 = **168**

```
168 passed, 0 failed, 0 skipped, 0 collection errors
```

## Exact 24-File Combined Result

Phase 4C2B exact 23-file baseline: **1044**  
Phase 4C3 new file: **57**  
Arithmetic: 1044 + 57 = **1101**

```
1101 passed, 0 failed, 0 skipped, 0 collection errors
```

Per-file counts (all 24 files):

| File | Tests |
|---|---|
| `test_lakebase_conversation_repository.py` | 79 |
| `test_lakebase_connection_provider.py` | 59 |
| `test_durable_genie_session_adapter.py` | 75 |
| `test_durable_genie_session_runtime_factory.py` | 99 |
| `test_genie_backend_durable_runtime_wiring.py` | 20 |
| `test_main_durable_runtime_lifecycle.py` | 58 |
| `test_conversation_repository.py` | 87 |
| `test_conversation_repository_factory.py` | 67 |
| `test_conversation_state_cleanup.py` | 8 |
| `test_conversation_state_factory.py` | 6 |
| `test_delta_conversation_state.py` | 10 |
| `test_genie_session_store.py` | 37 |
| `test_genie_session_store_context.py` | 7 |
| `test_multi_user_session_isolation.py` | 16 |
| `test_request_owner_identity.py` | 84 |
| `test_owner_identity_secret_configuration.py` | 24 |
| `test_request_owner_identity_runtime.py` | 64 |
| `test_chat_trusted_identity_extraction.py` | 42 |
| `test_genie_pipeline_owner_key_plumbing.py` | 52 |
| `test_chat_owner_key_plumbing.py` | 39 |
| `test_genie_pipeline_durable_lookup.py` | 36 |
| `test_chat_durable_lookup_key_plumbing.py` | 25 |
| `test_genie_pipeline_durable_writeback.py` | 50 |
| `test_genie_pipeline_last_message_persistence.py` | 57 |
| **Total** | **1101** |

## Complete Non-Live Result

Phase 4C2B complete non-live baseline: **1871**  
Phase 4C3 new file: **57**  
Arithmetic: 1871 + 57 = **1928**

Exclusions:
- `tests/test_genie_live_smoke.py`
- `tests/test_genie_integration_smoke.py`
- `tests/test_delta_state_live_smoke.py`
- `tests/test_new_pipeline_live_smoke.py`

```
1928 passed, 0 failed, 0 skipped, 0 collection errors
```

## New Test File

`tests/test_genie_pipeline_last_message_persistence.py`  
- 57 test methods in 12 test classes  
- 1017 lines

### Test Class Summary

| Class | Tests | Covers |
|---|---|---|
| `TestDisabledLocalNoMessageUpdate` | 4 | DISABLED bundle, local responses, error results |
| `TestMISS_PersistenceOrdering` | 4 | bind before update, get_or_create before update, run_inner before update, exactly once |
| `TestMISS_PersistenceSuccess` | 7 | correct ID stored, post-bind version, success result, fallback=False, mapping retained, no owner exposure |
| `TestMISS_PersistenceFailure` | 6 | null msg_id, mapping cleared, adapter down, shape_retry_exhausted, record not deleted |
| `TestRECOVERED_PersistenceOrdering` | 4 | no get_or_create, no bind, send before update, exactly once |
| `TestRECOVERED_PersistenceSuccess` | 6 | correct ID stored, lookup version used, success result, fallback=False, no conv_id passthrough, mapping retained |
| `TestRECOVERED_PersistenceFailure` | 6 | null msg_id, mapping cleared, adapter down, shape_retry_exhausted, record not deleted |
| `TestSameMessageIdempotency` | 3 | skip update, success result, mapping retained |
| `TestVersionConflictHandling` | 3 | no retry loop, same conv+msg success, different message fail closed |
| `TestProhibitedMutations4C3` | 7 | no touch/set_status/delete on MISS or RECOVERED, no direct repo attribute |
| `TestCrossOwnerIsolation` | 3 | separate owners independent, context not stored, failure non-contagious |
| `TestFinalMessageSelection` | 4 | start_conversation ID, send_message ID, called once, prior replaced |

## Existing Test Files Modified

### `tests/test_genie_pipeline_durable_lookup.py`

**Change 1 — `wait_for_message_completion` fake return value**

```python
# Before (incorrect fake):
def wait_for_message_completion(self, space_id, conv_id, msg_id, **kwargs):
    return MagicMock(query_attachments=None)

# After (correct fake):
def wait_for_message_completion(self, space_id, conv_id, msg_id, **kwargs):
    return MagicMock(query_attachments=None, message_id=msg_id)
```

Reason: `getattr(MagicMock(), "message_id", None)` returns a new truthy
`MagicMock`, not a string. The real Genie completion contract provides a
string `message_id`. The adapter's `_require_non_empty_value` rejected the
`MagicMock` value, causing 21 test failures.

**Change 2 — Removed `update_last_genie_message` trap from
`test_recovery_no_durable_mutations`**

Phase 4C3 makes `update_last_genie_message` an approved operation on the
RECOVERED path. Trapping it caused a spurious `AssertionError`.

**Change 3 — Removed `update_last_genie_message` trap and
`assert_not_called()` from `test_miss_no_lifecycle_mutation`**

Same reason — `update_last_genie_message` is now an approved MISS
writeback operation. The test still traps `delete`, `touch`, and
`set_status`, which remain prohibited.

**Change 4 — `test_hit_no_mutations` switched from `_make_guarded_pipeline`
to `_build_recovered_pipeline`**

A real pre-seeded `InMemoryConversationRepository` is now used so the
approved `update_last_genie_message` call completes without error. The
`update_last_genie_message` trap was removed. The test still traps
`get_or_create`, `bind_genie_conversation`, `set_status`, `delete`, and
`touch`.

### `tests/test_genie_pipeline_durable_writeback.py`

**Change 1 — `wait_for_message_completion` fake return value**

Same fix as in the lookup file: `message_id=msg_id` added.

**Change 2 — Renamed `test_update_last_genie_message_not_called` →
`test_update_last_genie_message_called_once`**

The assertion changed from `len(...) == 0` to `len(...) == 1`.  
Phase 4C3 requires exactly one `update_last_genie_message` call after MISS
binding. The old assertion was the root cause of the final pre-4C3 test
failure.

## Phase 4C2B Baseline for Reference

The following values are historical baselines, not Phase 4C3 totals:

- Phase 4C2B exact 23-file suite: **1044** tests
- Phase 4C2B complete non-live suite: **1871** tests
- Phase 4C2B had **22 failing** tests (all fixed by Phase 4C3)
