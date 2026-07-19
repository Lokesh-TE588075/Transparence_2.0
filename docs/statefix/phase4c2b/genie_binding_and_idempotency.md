# Phase 4C2B — Genie Binding and Idempotency

## Binding Semantics

`adapter.bind_genie_conversation(key, genie_conversation_id, expected_version=v)`
performs a compare-and-swap (CAS) against the durable record version.

The version passed to bind is always `record.version` obtained from the
immediately preceding `get_or_create` call.  No stale version is ever used.

## Idempotency

The idempotency check fires in two places:

### 1. At get_or_create time

If `get_or_create` returns a record that is already bound to the same
`final_genie_conv_id`, the method returns immediately without calling bind.
This handles the case where a previous request already persisted the binding.

### 2. After a version conflict + reload

If `bind_genie_conversation` raises `DurableGenieSessionVersionConflictError`,
the method performs **exactly one** `adapter.load(key)` reload.  If the
reloaded record shows the same `genie_conversation_id`, the method treats
this as an idempotent success (a concurrent caller already bound the same ID).

## Conflict Handling

| After conflict reload result | Action |
|---|---|
| Same `genie_conversation_id` | Return (idempotent success) |
| Different `genie_conversation_id` | `_DurableWritebackError` (fail closed) |
| `genie_conversation_id == None` | `_DurableWritebackError` (fail closed) |
| `reloaded is None` | `_DurableWritebackError` (record disappeared) |
| `adapter.load` raises | `_DurableWritebackError` (reload failed) |

There is no retry loop.  A single reload is the maximum.

## Never Overwrites Different Bindings

If the durable record is already bound to a **different** Genie conversation
ID (at get_or_create time or after conflict reload), the method raises
`_DurableWritebackError` and returns an error response.  The existing binding
is preserved in the repository.
