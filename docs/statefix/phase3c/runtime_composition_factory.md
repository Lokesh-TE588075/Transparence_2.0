# Phase 3C — Runtime Composition Factory

## Overview

`app/services/durable_genie_session_runtime_factory.py` is the single new
production file introduced in Phase 3C.  It composes
`ConversationRepositoryFactory` (Phase 3A) and `DurableGenieSessionAdapter`
(Phase 3B) behind an explicit feature flag that defaults to **false**.

No existing production file was modified.  No active request path was changed.

---

## Components

### DurableGenieSessionRuntimeSettings

Immutable frozen dataclass.  Parses a single boolean flag from the environment:

```
ENABLE_DURABLE_GENIE_SESSION_ADAPTER  (default: false)
```

`from_environment(environ)` is the only entry-point that reads the environment.
Importing the module reads nothing.

### CacheStoreProtocol

A structural contract class (not a runtime Protocol; import-compatible without
`runtime_checkable`) listing the six cache methods actually called by
`_GenieSessionCompatibilityCache` inside `DurableGenieSessionAdapter`:

- `get_genie_conversation_id`
- `get_last_message_id`
- `set_genie_conversation_id`
- `set_last_message_id`
- `reset_genie_mapping`
- `reset_session`

The caller must inject a `GenieSessionStore`-compatible instance explicitly.
`GenieSessionStore` is never auto-imported or auto-constructed.

### DurableGenieSessionRuntimeBundle

Lifecycle-managed result of `DurableGenieSessionRuntimeFactory.create()`.

| Field       | Disabled path | Enabled path                        |
|-------------|---------------|-------------------------------------|
| `enabled`   | `False`       | `True`                              |
| `adapter`   | `None`        | `DurableGenieSessionAdapter`        |
| `backend`   | `None`        | `ConversationRepositoryBackend`     |
| `durable`   | `False`       | Propagated from repo bundle         |

`close()` is idempotent.  Context-manager supported.  Repr exposes only
`enabled`, `backend.value`, `durable`, `closed` — no host, endpoint, token, or
identifier.

### DurableGenieSessionRuntimeFactory

Constructor parameters (all keyword-only, all optional):

- `environ` — environment mapping (defaults to `os.environ` via settings).
- `repository_factory_cls` — `ConversationRepositoryFactory` class or fake for
  tests.  When `None`, lazily imported inside `create()` on the enabled path.
- `adapter_cls` — `DurableGenieSessionAdapter` class or fake for tests.

Nothing is constructed during `__init__`.

### create_durable_genie_session_runtime (convenience function)

Creates a fresh factory and returns a new bundle.  No singleton.  Not called
by any existing runtime module.

---

## Module-Level Guarantees

- No environment access at import time.
- No module-level repository, pool, credential, adapter, or factory instance.
- All lazy imports of `conversation_repository_factory` and
  `durable_genie_session_adapter` occur inside method bodies on the enabled
  path only.
