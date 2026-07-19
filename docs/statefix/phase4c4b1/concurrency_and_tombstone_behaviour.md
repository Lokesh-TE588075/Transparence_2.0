# Concurrency and Tombstone Behaviour

## Phase 4C4B1

### Tombstone Creation Flow

When `adapter.load(key)` returns `None`:

1. Call `adapter.get_or_create(key)` exactly once
2. Reject degraded/unconfirmed results → fail closed
3. If returned ACTIVE: CAS transition to RESET, confirm RESET
4. If returned RESET/STALE/EXPIRED: another request already made it non-active;
   idempotent success without calling set_status

The tombstone occupies the logical key permanently. Repeated resets are idempotent.

### Version Conflict Policy

When `set_status` raises `DurableGenieSessionVersionConflictError`:

1. Call `adapter.load(key)` at most ONCE (no retry loop)
2. Reject degraded/unconfirmed reloads

| Reload Status | Action |
|--------------|--------|
| RESET | Idempotent success, remove local session |
| STALE | Idempotent success, remove local session |
| EXPIRED | Idempotent success, remove local session |
| ACTIVE | Raise `ResetCoordinatorConflictError` (no retry) |
| None | Raise `ResetCoordinatorConflictError` |
| Unavailable | Raise `ResetCoordinatorUnavailableError` |

No CAS retry loop. No last-writer-wins. No delete.

### Durable-Success/Local-Removal Ordering

Required ordering:
```
authoritative durable success
  → complete process-local session removal
    → return reset success
```

When durable reset fails:
- Retain process-local session
- Return conflict/unavailable
- Do not report reset success

When durable success confirmed but `remove_session` raises unexpectedly:
- Do NOT reverse the durable RESET
- Log warning (no identifiers)
- The session will be cleaned up on next expiry cycle

`remove_session` is designed as non-throwing for valid input to structurally
prevent this edge case.

### Concurrent Reset Calls

- Multiple concurrent resets of the same conversation are safe
- Only one CAS transition wins; others observe RESET on reload → idempotent
- `remove_session` uses the store lock; concurrent remove calls produce
  exactly one True result and the rest return False
- No data corruption or partial state is possible

### get_or_create Called At Most Once

The tombstone flow calls `get_or_create` exactly once per reset request.
If it fails, the coordinator surfaces the failure immediately without retry.
