# Phase 4C4B3A — Test Report

## Focused Suite (5 Phase 4C4B3A files)

| File | Tests |
|---|---|
| `test_process_local_conversation_key.py` | 38 |
| `test_conversation_reset_coordinator.py` | 63 |
| `test_chat_owner_scoped_local_key.py` | 21 |
| `test_chat_owner_key_plumbing.py` | 39 |
| `test_chat_durable_lookup_key_plumbing.py` | 25 |
| **Total focused** | **186** |
| Adapter isolation test | +1 |
| **Total** | **187 passed, 0 failed** |

## Full Non-Live Suite

**2038 passed, 37 skipped (pre-existing), 76 warnings (pre-existing), 0 failed**

Excluded (live/module-exit): `test_lakebase_connection_provider.py`,
`test_genie_integration_smoke.py`, `test_genie_live_smoke.py`.

## Key Test Changes

### test_conversation_reset_coordinator.py
- All 60 `coord.reset()` calls updated to include `process_local_conversation_key=_VALID_LOCAL_KEY`
- Tests 14, 23, 24, 25, 26: session setup and assertions updated from `_VALID_FRONTEND_ID` to `_VALID_LOCAL_KEY`
- Tests 36, 38, 39 (raw store tests): `_VALID_FRONTEND_ID` unchanged — correct, these test the store directly
- Added 14 new `TestProcessLocalKeyContract` tests (plk_01 – plk_14)

### test_chat_owner_key_plumbing.py
- Test 16: already updated (pre-existing)
- Test 19 (`test_pipeline_receives_previous_args_unchanged`): assertion updated from literal `"s1:conv-99"` to `plc_v1_` format check

### test_chat_durable_lookup_key_plumbing.py
- `TestExplicitFrontendId.test_app_conversation_id_is_opaque_owner_scoped_key`: already correct
- `TestGeneratedFrontendId.test_app_conversation_id_uses_generated` (L247): updated to use `build_process_local_conversation_key` and `is_valid_process_local_key`
- `TestGeneratedFrontendId.test_same_generated_id_used_consistently` (L268): updated from `in` containment check to `startswith("plc_v1_")` format check
- `TestDisabledPath.test_generated_id_still_works` (L381): **unchanged** — legacy `session:id` format in disabled path

## Regression Notes

One pre-existing isolation test (`test_adapter_not_imported_by_existing_runtime_modules`)
required a fix to `process_local_conversation_key.py`: the local import of
`DurableGenieSessionKey` was replaced by inlined validation to preserve
adapter isolation.
