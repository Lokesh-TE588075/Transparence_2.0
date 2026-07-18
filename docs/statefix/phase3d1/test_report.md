# Phase 3D1 test report

## Test files changed

* `tests/test_durable_genie_session_runtime_factory.py`
* `tests/test_genie_backend_durable_runtime_wiring.py`

## New coverage added

The new Phase 3D1 wiring suite validates:

* direct `GeniePipeline` return contract is preserved
* singleton identity remains unchanged
* exactly one `GenieSessionStore` is created
* the same store is passed to both `GeniePipeline` and runtime creation
* disabled and enabled bundles are attached privately to the pipeline
* adapter methods are not called during backend construction
* reset closes the attached bundle once and repeated reset is safe
* legacy pipelines without the private attribute still reset safely
* runtime initialization failures are surfaced and sanitized
* no separate global runtime bundle exists
* no separate global repository object exists
* request-path files remain unchanged with respect to durable bundle access
* `genie_backend_factory.py` does not import repository or Lakebase implementation modules directly

## Executed suites

* Focused Phase 3D1 suite:
  * `tests/test_genie_backend_durable_runtime_wiring.py`
  * Result: 20 passed
* Phase 3C + Phase 3D1 suites:
  * `tests/test_durable_genie_session_runtime_factory.py`
  * `tests/test_genie_backend_durable_runtime_wiring.py`
  * Result: 119 passed
* Combined persistence and composition suites:
  * `tests/test_conversation_repository.py`
  * `tests/test_conversation_repository_factory.py`
  * `tests/test_lakebase_connection_provider.py`
  * `tests/test_lakebase_conversation_repository.py`
  * `tests/test_durable_genie_session_adapter.py`
  * `tests/test_durable_genie_session_runtime_factory.py`
  * `tests/test_genie_backend_durable_runtime_wiring.py`
  * Result: 486 passed
* Complete non-live suite excluding the four approved live suites:
  * Result: 1397 passed

## Environment note

The complete non-live suite initially failed collection because `rapidfuzz` was not installed in the temporary serverless environment. Per the phase instruction, `rapidfuzz` was installed only in the temporary environment and `requirements.txt` was not modified.
