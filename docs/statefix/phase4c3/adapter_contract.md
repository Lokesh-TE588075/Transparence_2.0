# Adapter Contract for Phase 4C3

## `adapter.update_last_genie_message`

```
update_last_genie_message(
    key: DurableGenieSessionKey,
    last_genie_message_id: str,
    *,
    expected_version: int,
    now: Optional[datetime] = None,
) -> ConversationRecord
```

### Preconditions

- `last_genie_message_id` must be a non-empty string.  
  Validation: `_require_non_empty_value` raises `ValueError` on empty/non-str.
- Record must exist in the repository.  
  `_require_existing_record` reads from repo directly (not from `adapter.load`).
  On missing record: raises `DurableGenieSessionNotFoundError`.
- `expected_version` must match the current record version.  
  On mismatch: raises `DurableGenieSessionVersionConflictError`.

### Postconditions

- Returns updated `ConversationRecord` with:
  - `last_genie_message_id` set to the new value
  - `version` incremented

### Error Translation

| Repository exception | Adapter exception |
|---|---|
| `ConversationVersionConflictError` | `DurableGenieSessionVersionConflictError` |
| `ConversationRepositoryUnavailableError` | `DurableGenieSessionUnavailableError` |
| `ConversationNotFoundError` | `DurableGenieSessionNotFoundError` |

### Phase 4C3 Handling

`_persist_durable_last_message` catches:
- `DurableGenieSessionVersionConflictError` → reload once, check idempotency or raise
- All other exceptions → raise `_DurableLastMessageError` immediately
