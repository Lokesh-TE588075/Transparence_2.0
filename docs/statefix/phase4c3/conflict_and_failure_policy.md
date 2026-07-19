# Phase 4C3 — Conflict and Failure Policy

## Fail-Closed Contract

Every failure in the Phase 4C3 message-persistence layer:

1. Clears the current in-memory Genie conversation mapping via
   `self._store.reset_genie_mapping(app_conversation_id)`.
2. Returns an error response with `fallback_recommended=False`.
3. Does **NOT** delete, touch, or set_status on the durable record.
4. Does **NOT** expose identifiers (Genie conv IDs, message IDs, owner
   hashes, frontend conversation IDs) in the error response or logs.

The durable record already holds the correct `genie_conversation_id` from
Phase 4C2B. A Phase 4C3 failure means only `last_genie_message_id` could not
be updated. On the next request the conversation is still recoverable.

## Version Conflict — One Reload Maximum

When `adapter.update_last_genie_message` raises
`DurableGenieSessionVersionConflictError`:

1. `adapter.load(key)` is called **exactly once**. No retry loop.
2. The reloaded record is inspected:

| Reloaded state | Outcome |
|---|---|
| `genie_conversation_id` matches AND `last_genie_message_id` matches | Idempotent success — another writer already persisted the same message |
| `genie_conversation_id` matches but `last_genie_message_id` differs | Fail closed |
| `genie_conversation_id` differs | Fail closed |
| `record is None` (not found after conflict) | Fail closed |
| `adapter.load` itself raises | Fail closed |

No second reload is ever attempted.

## Conversation-ID Mismatch — Always Fail Closed

Before any update attempt (and before the idempotency check),
`_persist_durable_last_message` validates:

```python
if confirmed_record.genie_conversation_id != final_genie_conv_id:
    raise _DurableLastMessageError(...)
```

A mismatch means the in-memory state diverged from the durable record. This
is treated as a hard error regardless of any other conditions.

## Full Failure Catalog

| Trigger | Outcome |
|---|---|
| `genie_message_id` is None or empty | Fail closed — missing message ID is a hard error |
| `genie_conversation_id` missing from result | Persistence skipped (result returned unchanged) |
| `shape_retry_exhausted=True` | Persistence skipped (signal forwarded to frontend) |
| `result["status"] != "success"` | Persistence skipped (result returned unchanged) |
| `confirmed_record` key or record is None (RECOVERED only) | Fail closed |
| Conv-ID mismatch (result vs confirmed record) | Fail closed |
| Same message ID, same conv ID (idempotent) | Success — update skipped |
| Version conflict, reload succeeds + same conv + same msg | Idempotent success |
| Version conflict, reload succeeds + different msg or conv | Fail closed |
| Version conflict, reload raises | Fail closed |
| Version conflict, reload returns None | Fail closed |
| Adapter unavailable | Fail closed |
| Record not found | Fail closed |
| Any other adapter or repository exception | Fail closed |

## Prohibited Operations

Phase 4C3 persistence code never calls:

- `adapter.touch()` — not used at any point
- `adapter.set_status()` — not used at any point
- `adapter.delete()` — not used at any point
- Any repository method directly — only `bundle.adapter` is accessed

## In-Memory Mapping Cleanup

On fail-closed, only the mapping for the **current** conversation is cleared:

```python
self._store.reset_genie_mapping(app_conversation_id)
```

Other conversations' mappings are unaffected. This ensures a cross-owner
failure on one request does not corrupt other active sessions.

## `fallback_recommended=False` Rationale

Phase 4C3 errors always set `fallback_recommended=False` because:

- The underlying Genie call succeeded — routing to a non-Genie fallback
  pipeline is misleading.
- The error is in the persistence layer, not in query execution.
- The correct user-facing copy is the generic durable error message
  (`_MSG_DURABLE_LAST_MSG_FAILED`), not a pipeline-switch prompt.

## Identifier Sanitization

The error response returned by `_build_durable_last_msg_error_response`
sets both `genie_conversation_id` and `genie_message_id` to `None`. No
owner hash, frontend conversation ID, Genie ID, or internal exception
text appears in the response or in any log line emitted during failure
handling.
