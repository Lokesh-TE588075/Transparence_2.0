# Phase 4C4B2 — Tombstone Writeback Protection

## The TOCTOU Problem

A time-of-check / time-of-use race can occur between the initial durable
session lookup and the post-execution writeback:

```
Thread A (user request):
  T1: adapter.load(key) → None (MISS)
  T2: _run_inner() → Genie starts new conversation
  T3: _persist_new_durable_conversation()
        → adapter.get_or_create(key)
        → repository.create_conversation() → returns RESET record (!)
        → (previously: would try to bind → incorrect Genie ID on reset record)

Thread B (reset coordinator, between T1 and T3):
  T1.5: repository.set_conversation_status(key, RESET)
```

Between T1 and T3, a concurrent reset committed a tombstone to the repository.
`create_conversation()` in `InMemoryConversationRepository` (and the Lakebase
equivalent) returns the EXISTING record idempotently — so `get_or_create` now
returns the RESET record, not a fresh ACTIVE one.

## Gate Added in `_persist_new_durable_conversation`

After `get_or_create` returns, two new checks fire before any binding attempt:

### 1. Degraded result (consistency unconfirmed)

```python
if lookup_result.degraded:
    raise _DurableWritebackError(
        "Durable get-or-create returned a degraded result."
    )
```

Caught by `_DurableWritebackError` handler → `reset_genie_mapping` +
`_build_durable_writeback_error_response`.

### 2. Non-ACTIVE record (tombstone detected)

```python
if lookup_result.record.status != _ConversationStatusWB.ACTIVE:
    raise _DurableInactiveConversationError(
        "Durable get-or-create returned a non-ACTIVE record; concurrent reset detected."
    )
```

Caught by the new `_DurableInactiveConversationError` handler (Boundary 2).

## Boundary 2 Handler in `_maybe_persist_durable_writeback`

The `_DurableInactiveConversationError` handler is placed **before**
`_DurableWritebackError` in the exception chain:

```python
except _DurableInactiveConversationError:
    # Tombstone race: reset occurred between MISS lookup and writeback.
    try:
        self._store.remove_session(app_conversation_id)  # full eviction
    except Exception:
        pass
    return self._build_inactive_response(
        app_conversation_id, start_time, execution_time_ms
    )
except _DurableWritebackError:
    # Infrastructure failure (not tombstone): partial eviction only
    try:
        self._store.reset_genie_mapping(app_conversation_id)
    except Exception:
        pass
    return self._build_durable_writeback_error_response(...)
```

### Key difference: `remove_session` vs `reset_genie_mapping`

| Scenario | Method | Reason |
|---|---|---|
| Tombstone race | `remove_session(app_conv_id)` | Full eviction — conversation is gone; no context is valid |
| Infrastructure failure | `reset_genie_mapping(app_conv_id)` | Preserve last_intent, last_entities for retry |

## Durable Record Contract

When a tombstone race is detected:
- The durable repository record is **never modified or deleted**.
- The RESET/STALE/EXPIRED record stays as the authoritative state.
- `bind_genie_conversation` is **never called** on a non-ACTIVE record.
- The Genie conversation started during `_run_inner` is orphaned (no server-side cleanup; Genie spaces expire stale conversations independently).

## ConversationStatus Import

`ConversationStatus` is imported locally inside `_persist_new_durable_conversation`
as `_ConversationStatusWB` to avoid polluting module-level namespace:

```python
from app.services.conversation_repository import (
    ConversationStatus as _ConversationStatusWB,
)
```

This follows the established pattern of local imports in durable-session methods.
