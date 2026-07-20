# Phase 4D2 — Test Report (Correction)

## Status: CORRECTED

---

## Issue 1 Correction — Suite Arithmetic

### Previous report error

The previous Phase 4D2 report stated:
- Phase 4D1 exact baseline: 1473
- New Phase 4D2 tests: 47 + 27 = 74
- Reported expanded result: **1664** ← INCORRECT

1473 + 74 = 1547, not 1664. The previous run used more than 34 files.

### Corrected counts

**Collection audit (per-file):**

| File | Tests Collected |
|---|---|
| `test_production_readiness_configuration.py` | 61 |
| `test_production_readiness_permissions.py` | 27 |
| **Total new Phase 4D2** | **88** |

Note: 14 new tests (48–61) were added to address Issue 2 (deployment profile separation).

**Exact 34-file suite:**

| Component | Tests |
|---|---|
| Phase 4D1 32-file baseline | 1473 |
| Phase 4D2 configuration tests | +61 |
| Phase 4D2 permission tests | +27 |
| **34-file total** | **1561** |

**Result: 1561 passed, 0 failed, 0 skipped, 0 collection errors**

**Complete non-live suite:**

| Component | Tests |
|---|---|
| Phase 4D1 complete non-live baseline | 2300 |
| Phase 4D2 new tests | +88 |
| **Total** | **2388** |

**Result: 2388 passed, 0 failed, 0 skipped, 0 collection errors**

---

## Exact 34-File Suite — File List

1. `tests/test_lakebase_conversation_repository.py`
2. `tests/test_lakebase_connection_provider.py`
3. `tests/test_durable_genie_session_adapter.py`
4. `tests/test_durable_genie_session_runtime_factory.py`
5. `tests/test_genie_backend_durable_runtime_wiring.py`
6. `tests/test_main_durable_runtime_lifecycle.py`
7. `tests/test_conversation_repository.py`
8. `tests/test_conversation_repository_factory.py`
9. `tests/test_conversation_state_cleanup.py`
10. `tests/test_conversation_state_factory.py`
11. `tests/test_delta_conversation_state.py`
12. `tests/test_genie_session_store.py`
13. `tests/test_genie_session_store_context.py`
14. `tests/test_multi_user_session_isolation.py`
15. `tests/test_request_owner_identity.py`
16. `tests/test_owner_identity_secret_configuration.py`
17. `tests/test_request_owner_identity_runtime.py`
18. `tests/test_chat_trusted_identity_extraction.py`
19. `tests/test_genie_pipeline_owner_key_plumbing.py`
20. `tests/test_chat_owner_key_plumbing.py`
21. `tests/test_genie_pipeline_durable_lookup.py`
22. `tests/test_chat_durable_lookup_key_plumbing.py`
23. `tests/test_genie_pipeline_durable_writeback.py`
24. `tests/test_genie_pipeline_last_message_persistence.py`
25. `tests/test_conversation_reset_coordinator.py`
26. `tests/test_genie_pipeline_inactive_durable_state.py`
27. `tests/test_process_local_conversation_key.py`
28. `tests/test_chat_owner_scoped_local_key.py`
29. `tests/test_conversation_reset_route.py`
30. `tests/test_conversation_reset_runtime_wiring.py`
31. `tests/test_conversation_reset_combined_lifecycle.py`
32. `tests/test_browser_restart_idle_lifecycle.py`
33. `tests/test_production_readiness_configuration.py`
34. `tests/test_production_readiness_permissions.py`

---

## Configuration Test Groups (61 tests)

| Group | Tests | Description |
|---|---|---|
| TestBooleanParserDeterminism | 6 | Boolean parsing determinism |
| TestRequiredConfiguration | 6 | Required env var presence |
| TestFeatureFlagInvariants | 10 | Flag dependency invariants |
| TestSafeFlagCombinations | 6 | Profile A and production flag combinations |
| TestSecretValidation | 5 | Secret presence only — no value reads |
| TestDebugModeWarnings | 5 | Debug mode warning generation |
| TestLakebaseConfiguration | 5 | Lakebase config validation |
| TestReadinessReportStructure | 4 | Report immutability and structure |
| TestDeploymentProfileSeparation | 14 | NEW: Profile A vs Profile B separation |
| **Total** | **61** | |

## Permission Test Groups (27 tests)

| Group | Tests | Description |
|---|---|---|
| TestAllSufficientSnapshot | 4 | Full sufficient snapshot |
| TestMissingBlockingPermissions | 6 | Missing required permissions |
| TestInsufficientPermissions | 3 | PRESENT_BUT_INSUFFICIENT |
| TestOptionalPermissions | 3 | NOT_REQUIRED non-blocking |
| TestCannotVerifyPermissions | 4 | CANNOT_VERIFY blocks |
| TestPermissionReportStructure | 7 | Structure and safety |
| **Total** | **27** | |

---

## Excluded Smoke Files (4)

- `tests/test_genie_live_smoke.py`
- `tests/test_genie_integration_smoke.py`
- `tests/test_delta_state_live_smoke.py`
- `tests/test_new_pipeline_live_smoke.py`

---

## Test Environment

- Transient deps installed: `pydantic-settings`, `rapidfuzz`, `pytest-asyncio`
- No application deps modified
- No deployment performed
- No live Lakebase/Genie calls
- Run location: `/tmp/transparence_4d2_audit/` (workspace pycache restriction bypass)
