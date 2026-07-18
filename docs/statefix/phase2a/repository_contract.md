# Conversation Repository Contract

Phase 2A — TransparencE Genie State Persistence

---

## Purpose

This document describes the `ConversationRepository` interface defined in
`app/services/conversation_repository.py`.  It is the single normative reference
for all current and future implementations.

---

## Domain Model

### ConversationStatus

A `str` enum with four lifecycle values:

| Value     | Meaning |
|-----------|---------|
| `ACTIVE`  | Conversation is in normal use |
| `STALE`   | Exceeded inactivity threshold; may be refreshed or reset |
| `RESET`   | Explicitly reset by the user or system |
| `EXPIRED` | Passed the hard expiry TTL; must not be resumed |

### ConversationRecord

An immutable (`frozen=True`) dataclass representing one tracked conversation.

| Field | Type | Description |
|-------|------|-------------|
| `conversation_id` | `str` | Internal UUID. Auto-generated on create. |
| `owner_user_id_hash` | `str` | Pre-derived hash of user identity. Never raw email. |
| `frontend_conversation_id` | `str` | UUID from the browser client. |
| `genie_conversation_id` | `Optional[str]` | Genie Space conversation ID; None until bound. |
| `last_genie_message_id` | `Optional[str]` | Most recent Genie message ID; None initially. |
| `status` | `ConversationStatus` | Current lifecycle status. |
| `version` | `int` | Monotonically increasing CAS version. Starts at 1. |
| `created_at` | `datetime` | UTC aware. Set on create. Immutable. |
| `updated_at` | `datetime` | UTC aware. Updated on every mutation. |
| `last_active_at` | `datetime` | UTC aware. Updated by touch and compare_and_update. |

**Validation rules:**
- `conversation_id`, `owner_user_id_hash`, `frontend_conversation_id` must not be empty.
- `version` must be ≥ 1.
- All three timestamps must be timezone-aware UTC.
- `updated_at` ≥ `created_at`.
- `last_active_at` ≥ `created_at`.

---

## Repository Interface

`ConversationRepository` is a `@runtime_checkable Protocol` in
`app.services.conversation_repository`.

### Lookup Operations

#### `get_by_id(owner_user_id_hash, conversation_id) → Optional[ConversationRecord]`

Returns the record for `conversation_id` that is owned by `owner_user_id_hash`.
Returns `None` when:
- No such record exists.
- The record exists but is owned by a different user (cross-owner information is
  never leaked).

#### `get_by_frontend_id(owner_user_id_hash, frontend_conversation_id) → Optional[ConversationRecord]`

Returns the record for `(owner_user_id_hash, frontend_conversation_id)`, or `None`.

---

### Creation

#### `create_conversation(owner_user_id_hash, frontend_conversation_id, *, conversation_id=None, now=None) → ConversationRecord`

Creates a new conversation or returns the existing one idempotently.

**Idempotency semantics** (unique logical key: `owner_user_id_hash` + `frontend_conversation_id`):

| Condition | Behaviour |
|-----------|----------|
| Logical key does not exist | Create new record: status=ACTIVE, version=1, all timestamps=now, UUID auto-generated if no explicit `conversation_id`. |
| Logical key already exists | Return existing record unchanged. Version not incremented. |
| Explicit `conversation_id` already owned by another user | Raise `ConversationOwnershipError`. |
| Explicit `conversation_id` exists for same owner with different frontend_id | Raise `ConversationAlreadyExistsError`. |

This idempotency is required to handle duplicate browser requests safely.

---

### Mutations (all require `expected_version`)

All mutating operations except `create_conversation` and `delete_conversation`
require an `expected_version` argument.  See **Optimistic Concurrency** below.

#### `bind_genie_conversation(owner, conversation_id, genie_conversation_id, *, expected_version, now=None)`

Sets `genie_conversation_id`.  Raises `ValueError` if empty string.

#### `update_last_genie_message(owner, conversation_id, last_genie_message_id, *, expected_version, now=None)`

Sets `last_genie_message_id`.  Raises `ValueError` if empty string.

#### `touch(owner, conversation_id, *, expected_version, now=None)`

Updates `updated_at` and `last_active_at` without changing any other logical field.

#### `set_status(owner, conversation_id, status, *, expected_version, now=None)`

Transitions `status` to the given `ConversationStatus` value.

#### `compare_and_update(owner, conversation_id, *, expected_version, genie_conversation_id=None, last_genie_message_id=None, status=None, touch_last_active=True, now=None)`

Atomically updates multiple optional fields in one CAS operation.
Version increments exactly once regardless of how many fields change.
When `touch_last_active=False`, `last_active_at` is preserved unchanged.

---

### Listing

#### `list_for_owner(owner_user_id_hash, *, statuses=None, limit=50) → list[ConversationRecord]`

Returns records for `owner_user_id_hash` sorted by `updated_at` descending.
`statuses` filters to a subset of `ConversationStatus` values (all returned if `None`).
`limit` must be ≥ 1; `ValueError` raised otherwise.

---

### Deletion

#### `delete_conversation(owner, conversation_id) → bool`

Permanently removes the record.  Returns `True` on success, `False` when
not found **or** when owned by a different user (no ownership error raised;
cross-owner records appear not found).

---

## Ownership Semantics

All operations are scoped by `owner_user_id_hash`:

| Operation type | Cross-owner behaviour |
|----------------|-----------------------|
| Lookup | Return `None` |
| Mutation | Raise `ConversationNotFoundError` |
| Delete | Return `False` |
| Create (explicit ID conflict) | Raise `ConversationOwnershipError` |

No operation leaks whether another user has a matching `frontend_conversation_id`.

Raw email addresses and Databricks user IDs must never appear in this layer.
The `owner_user_id_hash` is a pre-derived HMAC/SHA-256 hash produced by the
identity service (Phase 2C).  This module does not hash or validate identities.

---

## Optimistic Concurrency

Every successful mutation (except create and delete) increments `version` by exactly 1.

```
stored.version == expected_version  →  apply update; version += 1
stored.version != expected_version  →  raise ConversationVersionConflictError; no change
```

On conflict, all fields including `version` are left completely unchanged.
The Lakebase Phase 2B implementation achieves this with a `WHERE version = $expected`
clause in its UPDATE or CAS statement.

---

## Exceptions

| Exception | When raised |
|-----------|-------------|
| `ConversationRepositoryError` | Base class for all repository errors |
| `ConversationNotFoundError` | Record absent or cross-owner access |
| `ConversationAlreadyExistsError` | Explicit ID conflicts with different logical conversation |
| `ConversationOwnershipError` | Explicit ID already owned by another user on create |
| `ConversationVersionConflictError` | CAS version mismatch |
| `ConversationRepositoryUnavailableError` | Storage backend unavailable |

Database-specific exceptions must never escape the repository boundary.

---

## Expected Lakebase SQL Mapping

| Repository operation | SQL pattern |
|----------------------|-------------|
| `create_conversation` | `INSERT INTO ... ON CONFLICT (owner_hash, frontend_id) DO NOTHING RETURNING *` |
| `get_by_id` | `SELECT * FROM ... WHERE conversation_id = $1 AND owner_hash = $2` |
| `get_by_frontend_id` | `SELECT * FROM ... WHERE owner_hash = $1 AND frontend_id = $2` |
| `bind_genie_conversation` | `UPDATE ... SET genie_id=$1, version=version+1, updated_at=$2 WHERE id=$3 AND owner=$4 AND version=$5` |
| `compare_and_update` | Same CAS pattern with multiple SET clauses |
| `delete_conversation` | `DELETE FROM ... WHERE id=$1 AND owner=$2 RETURNING id` |
| `list_for_owner` | `SELECT * FROM ... WHERE owner=$1 [AND status=ANY($2)] ORDER BY updated_at DESC LIMIT $3` |

The `ConversationVersionConflictError` is raised when the `UPDATE` / `DELETE ... RETURNING`
affects zero rows AND the record exists (i.e., the version did not match).
