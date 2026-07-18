# Phase 4A Test Report

## Validation environment

`pytest-asyncio` (version 1.4.0) was installed transiently into the ephemeral test environment.
It is not listed in `requirements.txt` and was not added there.
Installing it allowed all existing async lifecycle tests in `test_main_durable_runtime_lifecycle.py` to execute.
No test was skipped in any suite.

## New focused suite

Command:

`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python -m pytest -q tests/test_request_owner_identity.py`

Result:

* 84 passed
* 0 skipped
* 0 failed
* 0 collection errors

Coverage includes configuration loading, canonicalization, header matching, HMAC derivation, safe repr, sanitized errors, legacy-fallback rejection, and request-path boundary checks.

## Combined persistence, composition, lifecycle, and identity suite

Command included:

* `tests/test_lakebase_conversation_repository.py`
* `tests/test_lakebase_connection_provider.py`
* `tests/test_durable_genie_session_adapter.py`
* `tests/test_durable_genie_session_runtime_factory.py`
* `tests/test_genie_backend_durable_runtime_wiring.py`
* `tests/test_main_durable_runtime_lifecycle.py`
* `tests/test_conversation_repository.py`
* `tests/test_conversation_repository_factory.py`
* `tests/test_conversation_state_cleanup.py`
* `tests/test_conversation_state_factory.py`
* `tests/test_delta_conversation_state.py`
* `tests/test_genie_session_store.py`
* `tests/test_genie_session_store_context.py`
* `tests/test_multi_user_session_isolation.py`
* `tests/test_request_owner_identity.py`

Result:

* 712 passed
* 0 skipped
* 0 failed
* 0 collection errors

All existing async lifecycle tests executed with `pytest-asyncio` present.
No test was skipped.
Previous combined baseline was 628 passed (Phase 3D2 exit); adding 84 Phase 4A tests gives 712.

## Full non-live suite

Command excluded exactly:

* `tests/test_genie_live_smoke.py`
* `tests/test_genie_integration_smoke.py`
* `tests/test_delta_state_live_smoke.py`
* `tests/test_new_pipeline_live_smoke.py`

Result:

* 1539 passed
* 0 skipped
* 0 failed
* 0 collection errors

Previous non-live baseline was 1455 passed (Phase 3D2 exit); adding 84 Phase 4A tests gives 1539.
All async lifecycle tests executed; none were skipped due to missing plugin.
`requirements.txt` was not modified.

## Scope validation

The new Phase 4A tests also assert that:

* `chat.py` does not import the identity module
* `main.py` does not import the identity module
* `genie_pipeline.py` does not import the identity module
* `durable_genie_session_adapter.py` does not import the identity module
* the new module does not fall back to legacy email or anonymous identities
