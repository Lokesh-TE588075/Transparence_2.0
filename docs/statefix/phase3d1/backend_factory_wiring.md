# Phase 3D1 backend factory wiring

## Existing backend contract

`app/services/genie_backend_factory.py` continues to expose exactly these call signatures:

* `get_genie_pipeline(user_token: Optional[str] = None) -> GeniePipeline`
* `reset_genie_pipeline() -> None`
* `_build_pipeline(user_token: Optional[str] = None) -> GeniePipeline`

The module still returns a direct `GeniePipeline` instance. It does not return a wrapper, tuple, dataclass, dictionary, or other container.

## Wiring approach

Phase 3D1 attaches the durable runtime bundle to the existing pipeline instance through a private attribute:

* `_durable_session_runtime_bundle`

Construction order inside `_build_pipeline()` is:

1. Create `GenieSessionStore` exactly once.
2. Create `GeniePipeline` exactly as before, using that store.
3. Create `DurableGenieSessionRuntimeFactory` with the environment mapping from `settings.model_dump()`.
4. Call `runtime_factory.create(cache_store=store)`.
5. Attach the returned bundle to the pipeline instance.
6. Return the same `GeniePipeline` instance.

## Dependency boundaries

`genie_backend_factory.py` imports only the approved Phase 3C composition layer:

* `app.services.durable_genie_session_runtime_factory`

It does not import:

* `ConversationRepositoryFactory`
* `DurableGenieSessionAdapter`
* `lakebase_connection_provider`
* `lakebase_conversation_repository`

## Dependency injection

Phase 3D1 keeps the public contract unchanged. Test injection is provided only through the private builder:

* `_build_pipeline(..., runtime_factory_cls=...)`

No public caller is required to pass this argument.

## Request-path boundary

The runtime bundle is retained only as a private resource on `GeniePipeline`. No request-path code reads that attribute, and `GeniePipeline` does not call the attached durable adapter in this phase.
