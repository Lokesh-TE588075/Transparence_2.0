# Phase 4D1 — Test Report

## Status: PASS WITH BUILD BLOCKER

---

## Frontend JavaScript Tests

### Persistence tests (new)

**Command**: `node --test tests/test_frontend_conversation_persistence.mjs`
**Result**: 38 passed, 0 failed, 0 skipped, 0 cancelled, 0 unhandled rejections

| Group | Tests | Description |
|---|---|---|
| exports | 3 | STORAGE_KEY, SUPPORTED_VERSION, function exports |
| isValidConversationId | 10 | UUID, timestamp ID, empty, null, undefined, non-string, @, control char, over-length, exact-max |
| loadLifecycleState — empty/invalid | 6 | Empty, corrupt JSON, unsupported version, missing ID, invalid ID, getItem throws |
| saveLifecycleState / loadLifecycleState round-trip | 8 | Save/restore ID, version, conversations array, extra-field strip, overwrite, invalid ID, 60-conv truncation, setItem throws |
| clearLifecycleState | 4 | Removes state, no-op when empty, removeItem throws, only STORAGE_KEY removed |
| removeConversationFromLifecycleState | 4 | Remove by ID, no-op if absent, no-op on empty, active ID unchanged for other convs |
| title sanitisation | 2 | Long title truncated, non-string title not stored as raw number |
| storage-size guard | 1 | Payload over 16 KiB not stored |

### Reset tests (existing)

**Command**: `node --test tests/test_frontend_conversation_reset.mjs`
**Result**: 49 passed, 0 failed, 0 skipped, 0 cancelled

### Combined run

**Command**: `node --test tests/test_frontend_conversation_persistence.mjs tests/test_frontend_conversation_reset.mjs`
**Result**: 87 passed (38 + 49), 0 failed, 0 skipped, 0 cancelled, 0 unhandled rejections

---

## Backend Tests

### New lifecycle file

**File**: `tests/test_browser_restart_idle_lifecycle.py`
**Command**: `pytest tests/test_browser_restart_idle_lifecycle.py -q`
**Result**: 42 passed, 0 failed, 0 collection errors

| Group | Tests | Coverage |
|---|---|---|
| A: Process-Restart Recovery | 10 | send_message used; start_conversation not called; store repopulated; last-msg restored; different-owner isolation; no stale export; multiple restarts idempotent; unbound record; disabled mode; no owner_key |
| B: Local Idle-Expiry Recovery | 10 | remove_session recovery; cleanup+TTL recovery; 3 expiry cycles; cross-owner isolation; last-msg after recovery; no stale export after recovery; send_message on recovery; same frontend ID maps to same record; Genie ID unchanged; inactive blocks recovery |
| C: Inactive State After Restart/Expiry | 8 | RESET blocks restart; RESET blocks expiry; STALE blocks restart; EXPIRED blocks restart; static response; no new Genie conv; no durable write; fallback_recommended=False |
| D: Owner Isolation | 5 | Same fe-ID different owner; Owner B cannot recover A's conv; same owner different fe-IDs; session non-mixing after restart; Owner A reset does not affect Owner B |
| E: Durable Failure Policy | 5 | Unavailable repo fails closed; unavailable lookup no start_conv; fallback_recommended=False; no new conv; record validity |
| F: Race / Concurrency Safety | 4 | Idempotent multiple restarts; inactive tombstone wins over expiry; concurrent expiry+durable idempotent; no raw identifiers logged |

### Focused lifecycle suite (9 files)

| File | Tests |
|---|---|
| test_browser_restart_idle_lifecycle.py | 42 |
| test_main_durable_runtime_lifecycle.py | varies (async skip in env) |
| test_genie_pipeline_durable_lookup.py | — |
| test_genie_pipeline_inactive_durable_state.py | — |
| test_genie_pipeline_durable_writeback.py | — |
| test_genie_pipeline_last_message_persistence.py | — |
| test_genie_session_store.py | — |
| test_genie_session_store_context.py | — |
| test_conversation_reset_combined_lifecycle.py | — |

**Result**: 394 passed, 0 failed, 36 skipped (async tests, pre-existing), 0 collection errors

### Exact 32-file suite

**Baseline (Phase 4C4B5 31-file)**: 1431 passed
**New file**: `test_browser_restart_idle_lifecycle.py` (+42 tests)
**Expected**: 1431 + 42 = 1473
**Result**: 1473 passed, 0 failed, 0 skipped, 0 collection errors

### Complete non-live suite

**Excluded**:
- `tests/test_genie_live_smoke.py`
- `tests/test_genie_integration_smoke.py`
- `tests/test_delta_state_live_smoke.py`
- `tests/test_new_pipeline_live_smoke.py`

**Baseline (Phase 4C4B5)**: 2258 passed
**New tests**: +42
**Expected**: 2258 + 42 = 2300
**Result**: 2300 passed, 0 failed, 0 skipped, 0 collection errors

---

## Frontend Production Build

**Status**: `FRONTEND BUILD BLOCKED — external exact-SHA build required`

| Item | Result |
|---|---|
| npm on PATH | NOT FOUND |
| npx on PATH | NOT FOUND |
| node version | v22.9.0 |
| frontend/node_modules | ABSENT |
| vite binary | ABSENT |

The Phase 4C4B5 build (index-CyFuLl5J.js, 688 KB) was produced at commit
`38051da7ed0c2c4bf7c56ef4336be9061f96dc3b` and cannot be used as proof for the
modified Phase 4D1 frontend source. A new build is required at the Phase 4D1
commit using `npm ci && npm run build` in a local clone.

---

## Test Environment Notes

- Transient dependencies installed: `pydantic-settings`, `rapidfuzz`, `pytest-asyncio`
- No application dependencies were modified
- No deployment was performed
- No live Lakebase or Genie connections were used
