# Phase 4C4B2 — Inactive Lookup Contract

## Overview

`_durable_session_lookup` now returns one of four outcomes:

| Outcome     | Condition                                         | Action |
|-------------|---------------------------------------------------|--------|
| `DISABLED`  | Durable bundle not enabled or missing             | Proceed normally (`_run_inner`) |
| `MISS`      | No record found, OR ACTIVE record with no Genie ID | New-conversation flow (`_run_inner`) |
| `RECOVERED` | ACTIVE record with bound Genie conversation ID    | Restore mapping, follow-up flow |
| `INACTIVE`  | Non-ACTIVE record (RESET, STALE, EXPIRED)         | **Block immediately** |

## INACTIVE Classification (Phase 4C4B2 addition)

Prior to this phase, non-ACTIVE records (RESET, STALE, EXPIRED) were silently
reclassified as MISS, allowing new Genie conversations to start even when the
repository held an authoritative tombstone. This violated the reset lifecycle
contract.

### Correct classification

When `adapter.load(key)` returns a record with `status != ACTIVE`:

```python
if record.status != ConversationStatus.ACTIVE:
    return _DurableRequestContext(
        outcome=_DurableLookupOutcome.INACTIVE,
        key=durable_key,
        record=None,   # record is never returned for INACTIVE
    )
```

The INACTIVE outcome propagates to `run()` where it is handled before
`_run_inner()` is called.

## Degraded-Read Policy (Phase 4C4B2 addition)

When the repository was temporarily unavailable and `adapter.load()` returns
a degraded result (i.e., from the adapter-local confirmed snapshot), the pipeline
now **fails closed** by raising `_DurableLookupUnavailableError`:

```python
if result.degraded:
    raise _DurableLookupUnavailableError(
        "Durable session lookup returned a degraded result; consistency required."
    )
```

**Rationale**: A RESET tombstone could have been committed to the authoritative
repository while it was down. Accepting a degraded snapshot could allow Genie
execution to continue on a conversation that has been reset. Fail closed is
the only safe choice.

Note: Prior phases (4C3) accepted degraded ACTIVE snapshots for recovery.
Phase 4C4B2 tightens this to always fail closed on degraded results.

## INACTIVE Block in run()

```
run()
  → _durable_session_lookup() → INACTIVE
  → self._store.remove_session(app_conversation_id)  [silent]
  → return self._build_inactive_response(...)
  (no _run_inner call, no Genie execution, no writeback)
```

### Static response contract

`_build_inactive_response()` returns:
- `status: "inactive"` (distinct from `"error"`, `"success"`)
- `message: _MSG_INACTIVE_CONVERSATION` — deterministic, no PII
- `fallback_recommended: False` — custom pipeline must not serve this conversation
- `conversation_id: None` — session-scoped `app_conversation_id` (contains frontend ID as suffix) is suppressed
- `genie_conversation_id: None`, `genie_message_id: None` — no internal IDs exposed

## Owner Isolation

`GenieSessionStore.remove_session(app_conversation_id)` where
`app_conversation_id = f"{session_id}:{frontend_conversation_id}"` is scoped
to the current user's server-set session cookie. Different users with the same
`frontend_conversation_id` have different `session_id` values and therefore
different `app_conversation_id` keys. The remove is safe and does not affect
other users' in-memory state.
