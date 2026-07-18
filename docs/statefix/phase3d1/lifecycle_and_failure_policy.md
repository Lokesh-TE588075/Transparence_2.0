# Phase 3D1 lifecycle and failure policy

## Lifecycle ownership in Phase 3D1

`genie_backend_factory.py` still has no standalone backend container and `app/main.py` remains unchanged.

Lifecycle is therefore attached to the existing singleton reset path:

* `reset_genie_pipeline()` reads the current singleton pipeline
* it looks for `_durable_session_runtime_bundle`
* if present, it calls `bundle.close()` exactly once
* it clears the private attribute from the pipeline where safe
* it then leaves the singleton empty so the next request rebuilds it

This is safe for:

* legacy pipelines with no private attribute
* disabled bundles whose `close()` is a no-op
* repeated reset calls after the singleton has already been cleared

`genie_backend_factory.py` never closes a repository bundle directly. Runtime closure stays delegated to the runtime bundle, which closes the adapter, and the adapter owns repository lifecycle.

## Failure policy

If durable runtime initialization fails during `_build_pipeline()`:

* no partially initialized pipeline is returned
* any produced runtime bundle is closed best-effort before re-raising
* explicit sanitized runtime-factory errors are preserved
* unexpected errors are translated to the sanitized message:
  * `Failed to initialize the durable Genie session runtime.`

The backend factory must not leak:

* raw environment values
* host or endpoint details
* service-principal identifiers
* credentials or tokens
* raw SDK or database exceptions

## Phase 3D2 gate

Phase 3D2 can safely reuse the current `reset_genie_pipeline()` cleanup path from FastAPI shutdown. No `main.py` change was made in Phase 3D1.
