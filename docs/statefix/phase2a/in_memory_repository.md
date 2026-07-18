# InMemoryConversationRepository

Phase 2A — Reference Implementation

---

## WARNING: NOT DURABLE

`InMemoryConversationRepository` stores all state in Python dicts.  All records are
permanently lost when the process exits, restarts, or is suspended by the container
scheduler (e.g. Databricks Apps cold-start after inactivity).

**This class MUST NOT be used as the authoritative enterprise production repository.**

The enterprise production implementation is `LakebaseConversationRepository` (Phase 2B),
which persists records in a Lakebase Postgres table with compare-and-swap semantics
that mirror this contract exactly.

---

## Purpose

`InMemoryConversationRepository` exists to:

1. Provide a complete, runnable implementation of `ConversationRepository` for unit tests.
2. Allow local development and CI runs without a Lakebase database connection.
3. Serve as the specification reference for Phase 2B: every behaviour tested here must
   be replicated identically in `LakebaseConversationRepository`.

---

## Implementation Details

### Thread Safety

A single `threading.RLock` guards all state mutations.  `RLock` (re-entrant) is used
so that the same thread can acquire the lock recursively without deadlocking.  This
simplifies future refactoring where one guarded method calls another.

All reads and writes to `_by_id` and `_by_owner_frontend` happen inside the lock.
The lock is released before returning to the caller.

### Internal Indexes

```
_by_id: Dict[str, ConversationRecord]
    conversation_id -> ConversationRecord

_by_owner_frontend: Dict[Tuple[str, str], str]
    (owner_user_id_hash, frontend_conversation_id) -> conversation_id
```

Both indexes are updated atomically via `_store()` and `_remove()`.  Neither is
exposed to callers.  The lock must be held by the caller of both methods.

### Immutable Records

All `ConversationRecord` instances are `frozen=True` dataclasses.  Mutations produce
new instances via `dataclasses.replace()` (aliased as `_dc_replace`).  The `__post_init__`
validation in `ConversationRecord` runs on every produced instance, preventing
storage of invalid records even after a bug in mutation logic.

### Optimistic Concurrency

The `_check_version(record, expected_version)` helper is called before any mutation.
If the versions differ, `ConversationVersionConflictError` is raised immediately
(before any dict modification) and the indexes remain untouched.

This models the `WHERE version = $expected` CAS pattern that Phase 2B will use in SQL.

### Idempotent Create

The `create_conversation` method checks `_by_owner_frontend` first.  If the logical key
exists, the existing record is returned immediately without incrementing version or
modifying any field.  Concurrent duplicate creates are serialised by the lock, so
exactly one record is created regardless of thread count.

### Defensive Return Values

Because `ConversationRecord` is `frozen=True`, every returned record is inherently
immutable.  Callers cannot modify any field; they must use repository operations to
produce updated records.

---

## What Is NOT Implemented

- No background expiry worker.
- No TTL-based eviction.
- No persistence to any storage medium.
- No serialisation/deserialisation (the `serialize_session` / `deserialize_session`
  pattern used in `GenieSessionStore` was intentionally not repeated here; Delta
  persistence is the Phase 2B concern).

---

## Phase 2B Migration Path

The `LakebaseConversationRepository` in Phase 2B must:

1. Implement the same `ConversationRepository` Protocol.
2. Replicate all exception semantics (cross-owner → `ConversationNotFoundError`, etc.).
3. Use a single `conversations` table with columns matching `ConversationRecord` fields.
4. Use `WHERE version = $expected` CAS in every UPDATE.
5. Use `INSERT ... ON CONFLICT (owner_hash, frontend_id) DO NOTHING` for idempotent create.
6. Return `ConversationVersionConflictError` when UPDATE affects zero rows due to version
   mismatch (distinguished from “not found” by first checking row existence).

The Phase 2A unit tests in `tests/test_conversation_repository.py` must also pass
against `LakebaseConversationRepository` without modification.
