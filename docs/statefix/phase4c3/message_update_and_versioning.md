# Phase 4C3 — Message Update and Versioning

## `adapter.update_last_genie_message` — Signature

```python
update_last_genie_message(
    key: DurableGenieSessionKey,
    last_genie_message_id: str,
    *,
    expected_version: int,
    now: Optional[datetime] = None,
) -> ConversationRecord
```

Called at most **once per `pipeline.run()`** invocation, after the Genie
call has completed and the result has been validated.

## Preconditions

- `last_genie_message_id` must be a non-empty string.  
  Validated by `_require_non_empty_value`; raises `ValueError` on failure.
- Record must exist in the repository.  
  `_require_existing_record` reads from the repo directly (not from
  `adapter.load`). Missing record raises `DurableGenieSessionNotFoundError`.
- `expected_version` must match the current record version.  
  Mismatch raises `DurableGenieSessionVersionConflictError`.

## Postconditions

Returns an updated `ConversationRecord` with:
- `last_genie_message_id` set to the new value
- `version` incremented by 1

The returned record is post-update confirmation. `_persist_durable_last_message`
validates that the returned record's `genie_conversation_id` and
`last_genie_message_id` match the expected values before returning success.

## Error Translation

| Repository exception | Adapter exception |
|---|---|
| `ConversationVersionConflictError` | `DurableGenieSessionVersionConflictError` |
| `ConversationRepositoryUnavailableError` | `DurableGenieSessionUnavailableError` |
| `ConversationNotFoundError` | `DurableGenieSessionNotFoundError` |

## MISS Ordering — create/load → bind → message update

```
pipeline.run()
  └─ _run_inner()                        # all shape retries complete here
       └─ start_conversation()           # Genie conv started
       └─ wait_for_message_completion()  # final message returned
  └─ _maybe_persist_durable_writeback()
       └─ _persist_new_durable_conversation()
            └─ adapter.get_or_create()   # create or idempotent-load record
            └─ adapter.bind_genie_conversation()  # bind → returns bound_record
       └─ _persist_durable_last_message(
              confirmed_record=bound_record,
              expected_version=bound_record.version,  # POST-BIND version
          )
            └─ adapter.update_last_genie_message(key, msg_id,
                   expected_version=bound_record.version)
```

`expected_version` is the version from the record returned by
`bind_genie_conversation`, which reflects the state after binding. Using the
post-bind version ensures the CAS (compare-and-swap) targets the correct
snapshot.

## RECOVERED Ordering — lookup → send_message → message update

```
pipeline.run()
  └─ _durable_session_lookup()           # returns RECOVERED + key + record
  └─ _run_inner(_durable_recovered=True)
       └─ send_message()                 # continues existing Genie conv
       └─ wait_for_message_completion()  # final message returned
  └─ _maybe_persist_recovered_message(durable_ctx=ctx)
       └─ _persist_durable_last_message(
              key=ctx.key,
              confirmed_record=ctx.record,  # version from lookup
              expected_version=ctx.record.version,
          )
            └─ adapter.update_last_genie_message(key, msg_id,
                   expected_version=ctx.record.version)
```

On the RECOVERED path, `get_or_create` and `bind_genie_conversation` are
**never** called. The version used is the one from the confirmed lookup
record, which was validated at lookup time.

## Same-Message Idempotency

Before calling `adapter.update_last_genie_message`, `_persist_durable_last_message`
checks:

```python
if confirmed_record.last_genie_message_id == final_genie_message_id:
    return  # skip — already persisted
```

This path is taken silently and returns success. No adapter call is made.

Idempotency is conditional: the check only passes when both:
- `confirmed_record.last_genie_message_id == final_genie_message_id`
- `confirmed_record.genie_conversation_id == final_genie_conv_id`

The conversation-ID match check runs **before** the idempotency check.
A mismatch on `genie_conversation_id` always fails closed, even if the
message ID would otherwise match.

## Version Conflict Handling

On `DurableGenieSessionVersionConflictError` from `update_last_genie_message`:

1. `adapter.load(key)` is called **once**.
2. If the reloaded record has the same `genie_conversation_id` **and** the
   same `last_genie_message_id` → idempotent success (another writer
   already persisted the same message).
3. Any other reloaded state → `_DurableLastMessageError` raised (fail
   closed). See `conflict_and_failure_policy.md` for full details.
