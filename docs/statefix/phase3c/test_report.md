# Phase 3C — Test Report

## Test Suites

| Suite | Command | Result |
|-------|---------|--------|
| Focused Phase 3C | `pytest tests/test_durable_genie_session_runtime_factory.py` | **99 passed** |
| Combined persistence | 6-file combined suite | **466 passed** |
| Complete non-live | All tests, 4 live files excluded | **1377 passed** |

All runs: zero failures, zero collection errors.

## Focused Phase 3C Test File

`tests/test_durable_genie_session_runtime_factory.py` — 99 tests.

Covers (per specification):

| Group | Count |
|-------|-------|
| Settings (1–10) | 10 |
| Disabled path (11–25) | 15 |
| Enabled composition (26–39) | 14 |
| Lifecycle (40–47) | 8 |
| Failure handling (48–53) | 6 |
| Security (54–61) | 8 |
| Integration boundaries (62–70) | 9 |
| Focused additional cases | 9 |
| **Total** | **99** |

## Isolation Test Adjustments

Two existing isolation tests were narrowly updated to allow only the new
approved module `app/services/durable_genie_session_runtime_factory.py`:

1. `test_no_existing_runtime_module_imports_the_factory` in
   `tests/test_conversation_repository_factory.py` — added `_APPROVED_IMPORTER`
   skip.
2. `test_adapter_not_imported_by_existing_runtime_modules` in
   `tests/test_durable_genie_session_adapter.py` — added `_APPROVED_IMPORTER`
   skip.

All other application modules are still rejected by both tests.

## Pre-Existing Test Kwarg Fix

`test_conversation_repository_factory.py` contained 20 tests using stale
kwarg names (`lakebase_settings_factory`, `lakebase_connection_provider_factory`,
`lakebase_repository_factory`) that did not match the current
`ConversationRepositoryFactory.__init__` parameter names (`durable_settings_factory`,
`durable_connection_factory`, `durable_repository_factory`).

These 20 tests were failing at baseline.  The kwarg names were corrected
with three replace-all patches to restore the combined suite to zero failures.

## Dependencies

`rapidfuzz` is not installed by default in the test execution environment.
It was installed transiently for the non-live suite run only, as in Phase 3B.
`requirements.txt` was not modified.
