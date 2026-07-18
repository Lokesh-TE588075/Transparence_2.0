# SQL and Transaction Contract

Phase 2B1 — TransparencE Genie State Persistence

---

## Table: app_conversation

All operations target the single table `app_conversation` with 10 columns:

| Column | Usage |
|--------|-------|
| conversation_id | PK, UUID |
| owner_user_id_hash | Owner identity (HMAC hash) |
| frontend_conversation_id | Browser session UUID |
| genie_conversation_id | Genie Space conversation ID |
| last_genie_message_id | Most recent Genie message |
| status | ACTIVE / STALE / RESET / EXPIRED |
| version | Optimistic concurrency counter |
| created_at | Row creation time (UTC, immutable) |
| updated_at | Last modification time (UTC) |
| last_active_at | Last user activity time (UTC) |

## Unique Constraint

```
UNIQUE (owner_user_id_hash, frontend_conversation_id)
```

## SQL Operations

### Lookup (read-only, no commit)

```sql
SELECT {columns} FROM app_conversation
WHERE conversation_id = %s AND owner_user_id_hash = %s
```

```sql
SELECT {columns} FROM app_conversation
WHERE owner_user_id_hash = %s AND frontend_conversation_id = %s
```

### Idempotent Create

```sql
INSERT INTO app_conversation (...)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (owner_user_id_hash, frontend_conversation_id)
DO NOTHING
RETURNING {columns}
```

When INSERT returns no row (ON CONFLICT fired), a follow-up SELECT retrieves
the existing record within the same transaction.

### Versioned Updates

All mutations use the atomic pattern:

```sql
UPDATE app_conversation SET
  {field} = %s,
  version = version + 1,
  updated_at = %s
WHERE owner_user_id_hash = %s
  AND conversation_id = %s
  AND version = %s
RETURNING {columns}
```

### Listing

```sql
SELECT {columns} FROM app_conversation
WHERE owner_user_id_hash = %s [AND status IN (%s, %s, ...)]
ORDER BY updated_at DESC, conversation_id ASC
LIMIT %s
```

### Deletion

```sql
DELETE FROM app_conversation
WHERE owner_user_id_hash = %s AND conversation_id = %s
```

## Transaction Behaviour

| Operation | Commit | Rollback |
|-----------|--------|----------|
| Successful read | None | None |
| Failed read | None | Via provider cleanup |
| Successful create | Once | None |
| Duplicate create | Once (after select) | None |
| Successful update | Once | None |
| Failed update (version/not found) | None | Once |
| Successful delete | Once | None |
| Database error | None | Once |

## Idempotent Create Flow

```
1. INSERT ... ON CONFLICT DO NOTHING RETURNING *
2. If row returned → commit, return record
3. If no row returned (conflict):
   a. SELECT existing record by (owner, frontend_id)
   b. Commit, return existing record
4. If INSERT raises unique violation on PK (conversation_id conflict):
   a. Rollback
   b. SELECT by conversation_id
   c. If different owner → raise OwnershipError
   d. If same owner, different frontend_id → raise AlreadyExistsError
   e. If same owner, same frontend_id → return record
```

## Optimistic Concurrency Diagnosis

When UPDATE ... RETURNING returns no row:

```
1. SELECT version FROM app_conversation
   WHERE owner_user_id_hash = %s AND conversation_id = %s
2. If no row → raise ConversationNotFoundError
3. If row exists (version differs) → raise ConversationVersionConflictError
4. Rollback (no state was changed)
```

## Error Translation

| Database condition | Repository exception |
|--------------------|---------------------|
| Connection failure | ConversationRepositoryUnavailableError |
| SQLSTATE 23505 + different owner | ConversationOwnershipError |
| SQLSTATE 23505 + same owner, different logical key | ConversationAlreadyExistsError |
| UPDATE returns 0 rows + record not found | ConversationNotFoundError |
| UPDATE returns 0 rows + version mismatch | ConversationVersionConflictError |

## Security

- All values passed via %s parameters, never interpolated.
- Exception messages never contain SQL text, credentials, or PII.
- Owner hash never appears in SQL string literals.
- SQLSTATE inspection uses duck-typing (sqlstate/pgcode attributes).
