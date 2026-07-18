# Phase 3D1 disabled runtime behaviour

## Disabled-path result

When `ENABLE_DURABLE_GENIE_SESSION_ADAPTER=false`:

* backend composition still creates the normal `GeniePipeline`
* the existing `GenieSessionStore` is still created once
* the same store is passed both to `GeniePipeline` and to `runtime_factory.create(cache_store=store)`
* a disabled `DurableGenieSessionRuntimeBundle` is attached to the pipeline
* the bundle remains inert and is not used in request processing

## Guaranteed non-behaviour on the disabled path

The disabled path must not perform any durable-runtime side effects:

* no `ConversationRepositoryFactory` construction
* no repository creation
* no Lakebase provider creation
* no physical connection or pool opening
* no credential generation
* no SQL execution
* no request-path adapter invocation

## Enabled-but-unused composition state

When the flag is explicitly enabled and the runtime factory succeeds:

* the same `GenieSessionStore` instance is injected as `cache_store`
* the enabled adapter is retained inside the attached runtime bundle
* `GeniePipeline` continues to operate exactly as before
* the adapter is not called by `GeniePipeline` in Phase 3D1

This phase is intentionally composition-only. Durable state exists as an attached private resource but is not yet part of conversation execution.
