# Phase 4C4B3A — Test Report

## Focused Suite (11 Phase 4C4B3A files)

| File | Tests |
|---|---|
| `test_process_local_conversation_key.py` | 47 |
| `test_conversation_reset_coordinator.py` | 63 |
| `test_chat_owner_scoped_local_key.py` | 21 |
| `test_chat_trusted_identity_extraction.py` | 42 |
| `test_chat_owner_key_plumbing.py` | 39 |
| `test_chat_durable_lookup_key_plumbing.py` | 25 |
| `test_genie_pipeline_owner_key_plumbing.py` | 52 |
| `test_genie_pipeline_inactive_durable_state.py` | 73 |
| `test_genie_pipeline_durable_lookup.py` | 36 |
| `test_genie_pipeline_durable_writeback.py` | 50 |
| `test_genie_pipeline_last_message_persistence.py` | 57 |
| **Total** | **505 passed, 0 failed, 0 skipped** |

## Combined Suite (26-file Phase 4C4B2 baseline + 2 new files)

**1315 passed, 0 failed, 0 skipped**

Arithmetic: Phase 4C4B2 exact 26-file baseline 1233
  + test_process_local_conversation_key.py 47 tests
  + test_chat_owner_scoped_local_key.py 21 tests
  + test_conversation_reset_coordinator.py net +14 tests
  = 1315

## Full Non-Live Suite

**2142 passed, 0 failed, 0 skipped, 1 warning (pre-existing Pydantic V2 deprecation)**

Excluded (live smoke only):
- `tests/test_genie_live_smoke.py`
- `tests/test_genie_integration_smoke.py`
- `tests/test_delta_state_live_smoke.py`
- `tests/test_new_pipeline_live_smoke.py`

Included (not excluded):
- `tests/test_lakebase_connection_provider.py` — 59 tests, all passing
- `tests/test_lakebase_conversation_repository.py` — all passing
- `tests/test_main_durable_runtime_lifecycle.py` — all passing

Arithmetic: Phase 4C4B2 non-live baseline 2060
  + test_process_local_conversation_key.py net +47 tests
  + test_chat_owner_scoped_local_key.py +21 tests
  + coordinator net additions +14 tests
  = 2142

## Key Test Changes

### test_process_local_conversation_key.py (47 tests)

- 38 original tests covering format, determinism, input-sensitivity,
  raw-input absence, validation rejection, exception safety, logging,
  thread safety, and is_valid_process_local_key boundary.
- 9 new `TestFrontendIDValidatorParity` tests (validation-correction commit):
  - plain ID accepted
  - leading whitespace accepted (with note on raw-vs-stripped digest)
  - trailing whitespace accepted
  - empty rejected (parity confirmation)
  - whitespace-only rejected (parity confirmation)
  - @ sign rejected (parity confirmation)
  - Unicode accepted
  - max representative valid accepted (no length cap)
  - control char in frontend_conversation_id accepted (neither validator restricts this)

### test_conversation_reset_coordinator.py (63 tests)

- All 60 `coord.reset()` calls updated to include `process_local_conversation_key=_VALID_LOCAL_KEY`
- Tests 14, 23, 24, 25, 26: session setup and assertions updated from
  `_VALID_FRONTEND_ID` to `_VALID_LOCAL_KEY`
- Tests 36, 38, 39 (raw store tests): `_VALID_FRONTEND_ID` unchanged —
  correct, these test the store directly, not via coordinator
- Added 14 new `TestProcessLocalKeyContract` tests (plk_01 – plk_14)

### test_chat_owner_key_plumbing.py (39 tests)

- Test 16: asserts opaque `plc_v1_` format in enabled path
- Test 19: assertion updated from literal `"s1:conv-99"` to `plc_v1_` format

### test_chat_durable_lookup_key_plumbing.py (25 tests)

- `TestExplicitFrontendId.test_app_conversation_id_is_opaque_owner_scoped_key`:
  asserts opaque key format
- `TestGeneratedFrontendId.test_app_conversation_id_uses_generated` (L247):
  uses `build_process_local_conversation_key` and `is_valid_process_local_key`
- `TestGeneratedFrontendId.test_same_generated_id_used_consistently` (L268):
  updated to `startswith("plc_v1_")` format check
- `TestDisabledPath.test_generated_id_still_works` (L381): **unchanged** —
  legacy `session:id` format in disabled path

## Regression Notes

One pre-existing isolation test (`test_adapter_not_imported_by_existing_runtime_modules`)
required a fix to `process_local_conversation_key.py`: the local import of
`DurableGenieSessionKey` was replaced by inlined validation to preserve
adapter isolation.
