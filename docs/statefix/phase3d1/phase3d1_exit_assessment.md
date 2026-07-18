# Phase 3D1 exit assessment

## Verdict

Phase 3D1 is complete.

The durable runtime has been wired into backend composition without changing the public backend contract and without integrating the durable adapter into request execution.

## What changed

* `genie_backend_factory.py` now composes `DurableGenieSessionRuntimeFactory`
* the produced runtime bundle is retained privately on the existing `GeniePipeline`
* `reset_genie_pipeline()` now closes the attached runtime bundle safely
* Phase 3C boundary tests were updated narrowly to allow only the approved importer
* a dedicated Phase 3D1 wiring suite was added

## What did not change

* `app/main.py`
* `app/routes/chat.py`
* `app/services/genie_pipeline.py`
* `app/services/genie_session_store.py`
* request-path conversation handling
* Genie conversation creation and routing
* Lakebase connectivity behaviour at runtime construction time

## Safety assessment

Verified in tests and code review:

* no second `GenieSessionStore` was introduced
* no separate global runtime bundle or repository object was introduced
* no direct adapter invocation was added to request processing
* no direct import of repository or Lakebase implementation modules was added to the backend factory
* no network, pool, credential, or SQL work was required by the Phase 3D1 tests

## Phase 3D2 readiness

Phase 3D2 FastAPI lifecycle closure is safe to begin.

The recommended next step is to call the existing `reset_genie_pipeline()` path from FastAPI shutdown so that the attached durable runtime bundle is closed during application teardown.
