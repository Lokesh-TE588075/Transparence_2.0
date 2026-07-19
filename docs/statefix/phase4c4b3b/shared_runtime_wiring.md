# Phase 4C4B3B — Shared Runtime Wiring

## Design Intent

`app/services/conversation_reset_runtime.py` bridges the reset route to the **exact** shared
objects owned by the singleton `GeniePipeline`.  It must not create new stores, adapters,
or repositories — only retrieve what the pipeline already owns.

## Object Retrieval Path

```
get_conversation_reset_coordinator()
  └─ get_genie_pipeline(user_token=None)       # deferred import from genie_backend_factory
      └─ pipeline._store                        # GenieSessionStore (attr: "_store")
      └─ pipeline._durable_session_runtime_bundle   # attr: "_durable_session_runtime_bundle"
          └─ bundle.adapter                     # DurableGenieSessionAdapter
  └─ ConversationResetCoordinator(adapter=adapter, session_store=store)
```

## Shared Object Identity Contract

| Proof | Assertion |
|---|---|
| Session store | `coordinator._session_store is pipeline._store` |
| Durable adapter | `coordinator._adapter is pipeline._durable_session_runtime_bundle.adapter` |
| No duplicate store | `GenieSessionStore.__init__` never called during coordinator retrieval |
| No duplicate adapter | `DurableGenieSessionAdapter.__init__` never called during coordinator retrieval |
| No _build_pipeline | `_build_pipeline` is never called directly by the runtime bridge |
| `get_genie_pipeline` call count | called exactly once per `get_conversation_reset_coordinator()` invocation |

## Fail-Closed Behaviour

| Condition | Result |
|---|---|
| `get_genie_pipeline()` raises | `ConversationResetRuntimeUnavailableError` |
| `pipeline._store` is `None` | `ConversationResetRuntimeUnavailableError` |
| `pipeline._durable_session_runtime_bundle` absent | `ConversationResetRuntimeUnavailableError` |
| `bundle.adapter` is `None` | `ConversationResetRuntimeUnavailableError` |
| `ConversationResetCoordinator()` raises | `ConversationResetRuntimeUnavailableError` |

The error class is opaque: no host, credentials, or internal details are exposed.

## Import-Time Safety

- All heavy imports in `conversation_reset_runtime.py` are deferred inside the function body.
- Importing the module at module level creates no stores, adapters, pools, or credentials.
- Registering `conversation_reset.router` in a FastAPI app creates no credentials or pools.

## Patch Target (for tests)

Because `get_genie_pipeline` is a deferred import, the correct patch target is:
```
"app.services.genie_backend_factory.get_genie_pipeline"
```
(not `app.services.conversation_reset_runtime.get_genie_pipeline`, which is not a module attribute)

Before calling `patch()`, `app.services.genie_backend_factory` must be imported so that
`pkgutil.resolve_name` can find it as an attribute of `app.services`.
