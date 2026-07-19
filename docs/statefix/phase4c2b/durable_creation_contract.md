# Phase 4C2B — Durable Conversation Creation Contract

## Overview

Phase 4C2B completes the minimal durable write path in GeniePipeline.
When the Phase 4C2A read-only lookup returns MISS and Genie successfully
creates a new conversation, the pipeline persists a durable application
conversation record and binds it to the final Genie conversation ID before
returning success.

## Invocation Conditions

Writeback fires if and only if ALL of the following hold:

1. The durable runtime bundle is enabled (`bundle.enabled == True`).
2. The Phase 4C2A lookup outcome is `_DurableLookupOutcome.MISS`.
3. `_run_inner()` has returned (all shape-validation retries complete).
4. `result["status"] == "success"` (no Genie error).
5. `result.get("shape_retry_exhausted")` is falsy.
6. `result.get("genie_conversation_id")` is a non-empty string.
7. `owner_key` is a non-empty string (not an email address).
8. `frontend_conversation_id` is a non-empty string.

If any condition is False, `_maybe_persist_durable_writeback` returns the
result dict unchanged (no side effects).

## Persistence Sequence

1. `adapter.get_or_create(DurableGenieSessionKey(owner_user_id_hash, frontend_conversation_id))`
   — creates the record if it does not exist; returns the existing one if it does.
2. Inspect `record.genie_conversation_id`:
   - `== final_genie_conv_id` → idempotent success, return immediately (no bind call).
   - `is not None and != final_genie_conv_id` → fail closed immediately.
   - `is None` → proceed to bind.
3. `adapter.bind_genie_conversation(key, final_genie_conv_id, expected_version=record.version)`
4. Confirm returned record's `genie_conversation_id == final_genie_conv_id`.

## Final Genie Conversation ID Source

The `genie_conversation_id` used for writeback comes exclusively from
`result.get("genie_conversation_id")` — the FINAL return value of
`_run_inner()`.  It is NOT read from the in-memory session store, which may
contain an intermediate ID if a shape-validation retry ran and discarded the
first Genie conversation.

## Adapter Access

The adapter is accessed exclusively through the runtime bundle:

```python
bundle = getattr(self, "_durable_session_runtime_bundle", None)
adapter = getattr(bundle, "adapter", None)
```

No direct repository access occurs from GeniePipeline.  No values from the
writeback are stored on the pipeline instance.

## Outcomes That Do NOT Write

| Condition | Behaviour |
|---|---|
| `_DurableLookupOutcome.DISABLED` | No write, return `_run_inner()` result |
| `_DurableLookupOutcome.RECOVERED` | No write, return `_run_inner()` result |
| Lookup failure (load raises) | Error response before `_run_inner()` is called |
| Genie error (`result["status"] == "error"`) | No write |
| No `genie_conversation_id` in result | No write |
| `shape_retry_exhausted` set | No write |
| `owner_key` absent or invalid | No write |
| `frontend_conversation_id` absent | No write |
