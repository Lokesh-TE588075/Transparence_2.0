# Phase 4A Test Report

## New focused suite

Command:

`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python -m pytest -q tests/test_request_owner_identity.py`

Result:

* 84 passed
* 0 failed
* 0 collection errors

Coverage includes configuration loading, canonicalization, header matching, HMAC derivation, safe repr, sanitized errors, legacy-fallback rejection, and request-path boundary checks.

## Combined persistence, composition, lifecycle, and identity suite

Command included:

* `tests/test_conversation_repository.py`
* `tests/test_lakebase_connection_provider.py`
* `tests/test_lakebase_conversation_repository.py`
* `tests/test_conversation_repository_factory.py`
* `tests/test_durable_genie_session_adapter.py`
* `tests/test_durable_genie_session_runtime_factory.py`
* `tests/test_genie_backend_durable_runtime_wiring.py`
* `tests/test_main_durable_runtime_lifecycle.py`
* `tests/test_request_owner_identity.py`

Result:

* 592 passed
* 36 skipped
* 0 failed
* 0 collection errors

The skipped tests were pre-existing async lifecycle tests in `test_main_durable_runtime_lifecycle.py` because the temporary runner did not include an async pytest plugin.
This Phase 4A change did not introduce those skips.

## Full non-live suite

Command excluded exactly:

* `tests/test_genie_live_smoke.py`
* `tests/test_genie_integration_smoke.py`
* `tests/test_delta_state_live_smoke.py`
* `tests/test_new_pipeline_live_smoke.py`

Result:

* 1503 passed
* 36 skipped
* 0 failed
* 0 collection errors

## Scope validation

The new Phase 4A tests also assert that:

* `chat.py` does not import the identity module
* `main.py` does not import the identity module
* `genie_pipeline.py` does not import the identity module
* `durable_genie_session_adapter.py` does not import the identity module
* the new module does not fall back to legacy email or anonymous identities
