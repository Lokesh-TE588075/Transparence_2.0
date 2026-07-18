# Phase 3B Test Report

## Focused Phase 3B suite

Command:

`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python -m pytest -q tests/test_durable_genie_session_adapter.py`

Result:

* 75 passed
* 0 failures
* 0 collection errors

## Combined persistence suites

Command:

`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python -m pytest -q tests/test_conversation_repository.py tests/test_lakebase_conversation_repository.py tests/test_lakebase_connection_provider.py tests/test_conversation_repository_factory.py tests/test_durable_genie_session_adapter.py`

Result:

* 367 passed
* 0 failures
* 0 collection errors

## Complete non-live suite

Command:

`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python -m pytest -q --ignore=tests/test_genie_live_smoke.py --ignore=tests/test_genie_integration_smoke.py --ignore=tests/test_delta_state_live_smoke.py --ignore=tests/test_new_pipeline_live_smoke.py`

Result:

* 1278 passed
* 0 failures
* 0 collection errors

## Environment note

The first full-suite attempt hit collection errors because `rapidfuzz` was missing from the ephemeral test environment. `rapidfuzz` was installed transiently for test execution only, the suite was rerun successfully, and no repository source or dependency manifests were changed as part of that environment fix.
