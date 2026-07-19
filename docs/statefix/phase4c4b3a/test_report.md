# Phase 4C4B3A — Test Report

## Focused Suite (11 Phase 4C4B3A files)

| File | Tests |
|---|---|
| `test_process_local_conversation_key.py` | 54 |
| `test_chat_owner_scoped_local_key.py` | 21 |
| `test_conversation_reset_coordinator.py` | 63 |
| `test_chat_trusted_identity_extraction.py` | 42 |
| `test_chat_owner_key_plumbing.py` | 39 |
| `test_chat_durable_lookup_key_plumbing.py` | 25 |
| `test_genie_pipeline_owner_key_plumbing.py` | 52 |
| `test_genie_pipeline_inactive_durable_state.py` | 73 |
| `test_genie_pipeline_durable_lookup.py` | 36 |
| `test_genie_pipeline_durable_writeback.py` | 50 |
| `test_genie_pipeline_last_message_persistence.py` | 57 |
| **Total** | **512 passed, 0 failed, 0 skipped** |

## Exact 28-File Combined Suite

Files 1-28 as specified in the Phase 4C4B3A exit criteria:

1. `test_lakebase_conversation_repository.py`
2. `test_lakebase_connection_provider.py`
3. `test_durable_genie_session_adapter.py`
4. `test_durable_genie_session_runtime_factory.py`
5. `test_genie_backend_durable_runtime_wiring.py`
6. `test_main_durable_runtime_lifecycle.py`
7. `test_conversation_repository.py`
8. `test_conversation_repository_factory.py`
9. `test_conversation_state_cleanup.py`
10. `test_conversation_state_factory.py`
11. `test_delta_conversation_state.py`
12. `test_genie_session_store.py`
13. `test_genie_session_store_context.py`
14. `test_multi_user_session_isolation.py`
15. `test_request_owner_identity.py`
16. `test_owner_identity_secret_configuration.py`
17. `test_request_owner_identity_runtime.py`
18. `test_chat_trusted_identity_extraction.py`
19. `test_genie_pipeline_owner_key_plumbing.py`
20. `test_chat_owner_key_plumbing.py`
21. `test_genie_pipeline_durable_lookup.py`
22. `test_chat_durable_lookup_key_plumbing.py`
23. `test_genie_pipeline_durable_writeback.py`
24. `test_genie_pipeline_last_message_persistence.py`
25. `test_conversation_reset_coordinator.py`
26. `test_genie_pipeline_inactive_durable_state.py`
27. `test_process_local_conversation_key.py`
28. `test_chat_owner_scoped_local_key.py`

**Arithmetic:**

| Component | Tests |
|---|---|
| Phase 4C4B2 exact 26-file baseline | 1233 |
| `test_process_local_conversation_key.py` | +54 |
| `test_chat_owner_scoped_local_key.py` | +21 |
| Coordinator net additions | +14 |
| **Total** | **1322** |

**Result: 1322 passed, 0 failed, 0 skipped**

## Full Non-Live Suite

**2149 passed, 0 failed, 0 skipped, 1 warning (pre-existing Pydantic V2 deprecation)**

Excluded (live smoke only):
- `tests/test_genie_live_smoke.py`
- `tests/test_genie_integration_smoke.py`
- `tests/test_delta_state_live_smoke.py`
- `tests/test_new_pipeline_live_smoke.py`

Arithmetic: Phase 4C4B2 non-live baseline 2060
  + `test_process_local_conversation_key.py` net +54 tests
  + `test_chat_owner_scoped_local_key.py` +21 tests
  + coordinator net additions +14 tests
  = 2149

## Key Test Changes (Canonicalization Correction)

### test_process_local_conversation_key.py (54 tests)

**Original 38 tests**: format, determinism, input-sensitivity, raw-input absence,
validation rejection, exception safety, logging, thread safety, is_valid boundary.

**+9 TestFrontendIDValidatorParity** (validation-correction commit, 638d899):
- plain ID accepted; leading/trailing whitespace accepted; empty/whitespace-only/@ rejected;
  Unicode accepted; max length accepted; control char in frontend ID accepted by both.
- Docstrings updated to reflect aligned canonicalization contract.

**+7 TestFrontendIDCanonicalization** (canonicalization-correction commit):
- `test_canonical_leading_whitespace_same_key` — `"  conv-1"` == `"conv-1"`
- `test_canonical_trailing_whitespace_same_key` — `"conv-1  "` == `"conv-1"`
- `test_canonical_both_ends_whitespace_same_key` — `"  conv-1  "` == `"conv-1"`
- `test_canonical_digest_matches_durable_key_logical` — padded == plain proves alignment
- `test_canonical_different_canonical_ids_different_keys` — different stripped IDs differ
- `test_canonical_unicode_with_whitespace_same_key` — Unicode + whitespace strips correctly
- `test_canonical_existing_vector_unchanged` — plain-ID digest vector unchanged by fix

### test_conversation_reset_coordinator.py (63 tests, unchanged by this correction)

All 60 `coord.reset()` calls include mandatory `process_local_conversation_key=_VALID_LOCAL_KEY`.
Tests 36, 38, 39 (raw store tests): `_VALID_FRONTEND_ID` correct — these test the store directly.
+14 new `TestProcessLocalKeyContract` tests verify mandatory key contract and opaque key behavior.

## Per-File Test Count Reference (all non-live files)

```
 142  test_pre_genie_router.py
  99  test_durable_genie_session_runtime_factory.py
  90  test_genie_pipeline.py
  89  test_genie_prompt_enricher.py
  87  test_conversation_repository.py
  84  test_request_owner_identity.py
  82  test_e5_enterprise_contracts.py
  79  test_lakebase_conversation_repository.py
  75  test_durable_genie_session_adapter.py
  73  test_genie_pipeline_inactive_durable_state.py
  67  test_conversation_repository_factory.py
  64  test_request_owner_identity_runtime.py
  63  test_conversation_reset_coordinator.py
  59  test_lakebase_connection_provider.py
  58  test_main_durable_runtime_lifecycle.py
  58  test_shape_validator_p1_regression.py
  57  test_genie_pipeline_last_message_persistence.py
  54  test_process_local_conversation_key.py
  52  test_genie_pipeline_owner_key_plumbing.py
  50  test_genie_pipeline_durable_writeback.py
  47  test_genie_session_store.py
  46  test_genie_response_mapper.py
  43  test_genie_backend_feature_flag.py
  42  test_chat_trusted_identity_extraction.py
  40  test_input_normalizer.py
  39  test_chat_owner_key_plumbing.py
  36  test_genie_pipeline_durable_lookup.py
  32  test_genie_client.py
  28  test_genie_table_summarizer.py
  27  test_delivery_date_normalizer.py
  27  test_sql_templates.py
  25  test_chat_durable_lookup_key_plumbing.py
  24  test_owner_identity_secret_configuration.py
  21  test_chat_owner_scoped_local_key.py
  21  test_conversation_followups.py
  21  test_query_understanding.py
  20  test_genie_backend_durable_runtime_wiring.py
  16  test_chat_pipeline.py
  16  test_grounded_summarizer.py
  16  test_multi_user_session_isolation.py
  12  test_pipeline_feature_flag.py
  11  test_deterministic_followup.py
  11  test_response_formatter.py
  10  test_delta_conversation_state.py
  10  test_empty_result_handling.py
   8  test_conversation_state_cleanup.py
   7  test_genie_session_store_context.py
   6  test_conversation_state_factory.py
   5  test_export_job_manager.py
TOTAL: 2149
```
