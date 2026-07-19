# Phase 4C3 Design

## Key Invariants

1. `update_last_genie_message` is called **at most once per `pipeline.run()` call**.
2. The call uses `expected_version=confirmed_record.version` — the version of
   the record returned by the preceding `bind_genie_conversation` (MISS) or
   the confirmed lookup (RECOVERED).
3. Missing `genie_message_id` (None or empty) always fails closed — this is the
   most likely corruption signal and must not silently succeed.
4. Version conflict: at most one reload. If reload shows the same conv+msg →
   idempotent success. Otherwise → fail closed. No retry loop.
5. Conv-ID mismatch: if the confirmed record's `genie_conversation_id` differs
   from the result's `genie_conversation_id` → fail closed.
6. On any failure: clear in-memory mapping, return error with
   `fallback_recommended=False`, do NOT delete the durable record.
7. Idempotent skip: if `confirmed_record.last_genie_message_id == final_msg_id`
   → skip the update call, return success.

## New Types

### `_DurableLastMessageError` (Exception)
Raised internally by `_persist_durable_last_message` for all non-idempotent
failures. Caught by both `_maybe_persist_durable_writeback` (MISS) and
`_maybe_persist_recovered_message` (RECOVERED) to produce an error response.

### `_DurableRequestContext` (dataclass, frozen)
Replaces the previous `str` return from `_durable_session_lookup`. Carries:
- `outcome: str` — DISABLED, MISS, or RECOVERED
- `key: Optional[Any]` — present on RECOVERED
- `record: Optional[Any]` — confirmed ConversationRecord, present on RECOVERED

## Method Overview

| Method | Purpose |
|---|---|
| `_persist_durable_last_message` | Core update; validates msg_id, checks idempotency, calls adapter, handles conflict |
| `_maybe_persist_durable_writeback` | MISS path: invoked after bind; constructs key and calls `_persist_durable_last_message` |
| `_maybe_persist_recovered_message` | RECOVERED path: gates on success+non-exhaust+conv_id; calls `_persist_durable_last_message` |
| `_build_durable_last_msg_error_response` | Builds sanitized error dict with `fallback_recommended=False` |
