# Phase 4C3 — Final Message Selection

## What Is Persisted

After every successful Genie pipeline call, Phase 4C3 persists exactly one
value: the **final** `genie_message_id` returned by the completed Genie
execution.

The source of truth is the pipeline result dict:

```
result["genie_conversation_id"]   # authoritative Genie conversation ID
result["genie_message_id"]        # authoritative final Genie message ID
```

These values are populated by `genie_response_mapper.py` from the message
object returned by `wait_for_message_completion`.

## All Retries Complete First

`_run_inner` may retry shape validation internally. The writeback and
message-persistence steps run **after** `_run_inner` returns — they
see only the final result from the last retry. Intermediate message IDs
(from earlier retry attempts) are never persisted.

## Authoritative vs Intermediate

| Value | Source | Persisted? |
|---|---|---|
| `result["genie_message_id"]` | Final `wait_for_message_completion` return | YES |
| Message ID from an earlier retry | Earlier `wait_for_message_completion` return | NO |
| Message ID from `start_conversation` on a MISS | Propagated into final result | YES (via result dict) |
| Message ID from `send_message` on RECOVERED | Propagated into final result | YES (via result dict) |

## Missing Message ID — Fail Closed

When `result["genie_conversation_id"]` is present (confirming a Genie call
occurred) but `result["genie_message_id"]` is absent or empty, Phase 4C3
fails closed:

1. In-memory Genie mapping for the conversation is cleared.
2. An error response with `fallback_recommended=False` is returned.
3. The durable record is NOT deleted.

A missing message ID when a conv ID is present is treated as an incomplete
execution contract — a hard error, not a skippable warning.

## MISS Path — Final Message Selection

On a **MISS** (no durable record existed before this call):

1. `_run_inner` executes `start_conversation` → receives `message_id`.
2. `wait_for_message_completion` is called with that `message_id`.
3. The final message object is mapped; `result["genie_message_id"]` is set.
4. After `_run_inner` returns, `_maybe_persist_durable_writeback` runs.
5. It calls `_persist_new_durable_conversation` (get_or_create + bind).
6. It then calls `_persist_durable_last_message` with `result["genie_message_id"]`.

The message ID persisted is always the one from the **final** invocation of
`wait_for_message_completion`, regardless of how many retries occurred
inside `_run_inner`.

## RECOVERED Path — Final Message Selection

On a **RECOVERED** call (an existing durable record was found):

1. `_run_inner` executes `send_message` → receives a new `message_id`.
2. `wait_for_message_completion` is called with that `message_id`.
3. The final message object is mapped; `result["genie_message_id"]` is set.
4. After `_run_inner` returns, `_maybe_persist_recovered_message` runs.
5. It calls `_persist_durable_last_message` with `result["genie_message_id"]`.

No `get_or_create` or `bind_genie_conversation` is called on the RECOVERED
path — only the message update.

## Eligibility Gates (Both Paths)

Persistence is skipped (result returned unchanged) when:

- `result["status"] != "success"`
- `result["shape_retry_exhausted"]` is truthy
- `result["genie_conversation_id"]` is absent or empty

When `genie_conversation_id` is present but `genie_message_id` is absent,
the response is the fail-closed error (not the original result).

## New Types

### `_DurableLastMessageError` (Exception)
Raised by `_persist_durable_last_message` for all non-idempotent failures.
Caught by both MISS and RECOVERED paths to produce an error response.

### `_DurableRequestContext` (frozen dataclass)
Return value of `_durable_session_lookup`. Carries:
- `outcome: str` — `DISABLED`, `MISS`, or `RECOVERED`
- `key: Optional[Any]` — present when `outcome == RECOVERED`
- `record: Optional[Any]` — confirmed `ConversationRecord`, present when `outcome == RECOVERED`

## Method Overview

| Method | Purpose |
|---|---|
| `_persist_durable_last_message` | Core update; validates msg_id, checks idempotency, calls adapter, handles conflict |
| `_maybe_persist_durable_writeback` | MISS path: after bind, constructs key, calls `_persist_durable_last_message` |
| `_maybe_persist_recovered_message` | RECOVERED path: gates on success+non-exhaust+conv_id; calls `_persist_durable_last_message` |
| `_build_durable_last_msg_error_response` | Builds sanitized error dict with `fallback_recommended=False` |
