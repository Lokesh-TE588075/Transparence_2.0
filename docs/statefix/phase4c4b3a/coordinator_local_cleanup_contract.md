# Phase 4C4B3A — Coordinator Local Cleanup Contract

## Problem

Before Phase 4C4B3A, `ConversationResetCoordinator.reset()` used
`frontend_conversation_id` both as the durable repository key component AND
as the `GenieSessionStore` removal key.  This was incorrect because the store
is now keyed by the opaque `plc_v1_` process-local key, not the raw frontend ID.

## Fix

`reset()` signature was extended with a third mandatory parameter:

```python
def reset(
    self,
    *,
    owner_user_id_hash: str,
    frontend_conversation_id: str,
    process_local_conversation_key: str,
) -> ResetResult:
```

- **Durable repository key**: `owner_user_id_hash` + `frontend_conversation_id` (unchanged)
- **Session store removal key**: `process_local_conversation_key` (new, mandatory)

## Mandatory Argument Contract

`process_local_conversation_key` is **required** — it has no default value.

- Missing argument causes a normal Python call-contract failure (`TypeError`).
- Empty or malformed key raises `ResetCoordinatorInvalidInputError`.
- No compatibility `None` default.
- No silent no-op when the key is absent.
- No fallback to `frontend_conversation_id`.
- No key derivation inside the coordinator.

## Validation

`process_local_conversation_key` is validated via `is_valid_process_local_key()`:

- Must match `^plc_v1_[0-9a-f]{64}$`
- Total length must be exactly 71 characters
- If provided and invalid → `ResetCoordinatorInvalidInputError`

## Session Removal

```python
def _remove_local_session(self, process_local_conversation_key: str) -> None:
    try:
        self._session_store.remove_session(process_local_conversation_key)
    except Exception:
        logger.warning(
            "Local session removal encountered an unexpected condition."
        )
```

Called unconditionally on all **success** paths:

- ACTIVE transitioned to RESET
- Existing RESET (already inactive)
- Existing STALE (already inactive)
- Existing EXPIRED (already inactive)
- Created RESET tombstone
- Successful conflict reload to RESET, STALE, or EXPIRED

Never called on failure paths:

- `ResetCoordinatorUnavailableError` (durable backend unavailable)
- `ResetCoordinatorConflictError` (unresolved ACTIVE after conflict reload)
- `ResetCoordinatorInternalError` (unexpected internal failure)

The `_remove_local_session` call is non-throwing: an unexpected exception from
`remove_session` does NOT reverse the authoritative durable RESET.  The local
session will be cleaned up on the next TTL expiry cycle.

## Prohibited Mutations

The coordinator is still prohibited from calling:
`adapter.delete`, `adapter.bind_genie_conversation`,
`adapter.update_last_genie_message`, `adapter.touch`,
or accessing the repository directly.
