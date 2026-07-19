# Phase 4C4B3A — Coordinator Interface Cleanup

## Problem

Before Phase 4C4B3A, `ConversationResetCoordinator.reset()` used
`frontend_conversation_id` both as the durable repository key component AND
as the `GenieSessionStore` removal key.  This was incorrect because the store
is now keyed by the opaque `plc_v1_` process-local key, not the raw frontend ID.

## Fix

`reset()` signature was extended with a third parameter:

```python
def reset(
    self,
    *,
    owner_user_id_hash: str,
    frontend_conversation_id: str,
    process_local_conversation_key: Optional[str] = None,
) -> ResetResult:
```

- **Durable repository key**: `owner_user_id_hash` + `frontend_conversation_id` (unchanged)
- **Session store removal key**: `process_local_conversation_key` (new)

## Validation

`process_local_conversation_key` is validated via `is_valid_process_local_key()`:
- Must start with `plc_v1_`
- Total length must be exactly 71 characters
- If provided and invalid → `ResetCoordinatorInvalidInputError`
- If `None`, `_remove_local_session` is a no-op

## Session Removal

```python
def _remove_local_session(self, process_local_conversation_key: str) -> None:
    if process_local_conversation_key:
        self._session_store.remove_session(process_local_conversation_key)
```

Called on all success paths (RESET, ALREADY_INACTIVE, TOMBSTONE_CREATED) and
conflict-then-success paths.  Never called on failure paths
(UNAVAILABLE, CONFLICT with ACTIVE reload).

## Prohibited Mutations

The coordinator is still prohibited from calling:
`adapter.delete`, `adapter.bind_genie_conversation`,
`adapter.update_last_genie_message`, `adapter.touch`,
or accessing the repository directly.
