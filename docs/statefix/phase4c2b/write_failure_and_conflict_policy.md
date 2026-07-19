# Phase 4C2B — Write Failure and Conflict Policy

## Failure Classification

`_persist_new_durable_conversation` converts all persistence exceptions to
`_DurableWritebackError` and lets them propagate to the caller
(`_maybe_persist_durable_writeback`).

Exception mapping:

| Source exception | Wrapped as |
|---|---|
| `DurableGenieSessionUnavailableError` | `_DurableWritebackError` |
| `DurableGenieSessionVersionConflictError` (non-idempotent) | `_DurableWritebackError` |
| `DurableGenieSessionAdapterError` | `_DurableWritebackError` |
| Any other `Exception` | `_DurableWritebackError` |

## On Writeback Failure

`_maybe_persist_durable_writeback` catches `_DurableWritebackError` and any
unexpected `Exception`:

1. **Clears the newly established in-memory Genie mapping** by calling
   `self._store.reset_genie_mapping(app_conversation_id)`.
   This prevents the request from continuing as an unpersisted in-memory-only
   conversation.  An existing durable record is never deleted.

2. **Returns a sanitized error response** from `_build_durable_writeback_error_response`.

## Error Response Contract

The writeback error response has:

| Field | Value |
|---|---|
| `status` | `"error"` |
| `message` | Static sanitized string (no exception details) |
| `fallback_recommended` | `False` |
| `genie_conversation_id` | `None` |
| `source` | `"genie"` |

`fallback_recommended=False` ensures the request is **never** routed to the
custom pipeline after a durable persistence failure.

## What Is Not Cleared

- Existing durable records in the repository are never deleted.
- Business context in the session store (`last_intent`, `last_entities`) is
  preserved by `reset_genie_mapping` (it only clears Genie IDs).
- No status mutation, touch, or update_last_genie_message is ever called.

## Failure Transparency

No ownership values (owner_key, frontend_conversation_id, conversation_id
from the repository) appear in the error response or in logged exception
messages.  The static message `_MSG_DURABLE_WRITEBACK_FAILED` is always used.
