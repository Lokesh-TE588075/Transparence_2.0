# H1: Schema and Migration

## Table: app_conversation_message

Migration: `migrations/lakebase/002_create_conversation_message.sql`

```sql
CREATE TABLE IF NOT EXISTS transparence_state.app_conversation_message (
    message_id              TEXT         NOT NULL,
    owner_user_id_hash      TEXT         NOT NULL,
    frontend_conversation_id TEXT        NOT NULL,
    message_sequence        INTEGER      NOT NULL,
    role                    TEXT         NOT NULL,
    message_text            TEXT         NOT NULL,
    response_payload_json   TEXT,
    is_active               BOOLEAN      NOT NULL,
    created_at              TIMESTAMPTZ  NOT NULL,
    updated_at              TIMESTAMPTZ  NOT NULL
);
```

### Constraints

| Name | Type | Definition |
|---|---|---|
| `pk_app_conversation_message` | PRIMARY KEY | `(message_id)` |
| `uq_conv_msg_sequence` | UNIQUE | `(owner_user_id_hash, frontend_conversation_id, message_sequence)` |
| `chk_conv_msg_role` | CHECK | `role IN ('user', 'assistant')` |
| `chk_conv_msg_sequence_positive` | CHECK | `message_sequence >= 1` |
| `chk_conv_msg_text_nonempty` | CHECK | `char_length(message_text) >= 1` |
| `chk_conv_msg_updated_at` | CHECK | `updated_at >= created_at` |

### Indexes

| Name | Partial | Purpose |
|---|---|---|
| `idx_conv_msg_history_read` | `WHERE is_active = TRUE` | Fast owner+conv read for history API |
| `idx_conv_msg_deactivate` | `WHERE is_active = TRUE` | Fast reset deactivation |
| `idx_conv_msg_created_at` | — | Chronological ordering |

## Sequence Assignment

Sequence is computed as `SELECT COALESCE(MAX(message_sequence), 0) + 1` within
the same transaction that issues the INSERT.  The UNIQUE constraint on
`(owner_user_id_hash, frontend_conversation_id, message_sequence)` provides
the concurrent-write safety net: any race causes a `MessageSequenceConflictError`
which callers may retry.

## Size Limits

| Field | Limit |
|---|---|
| `message_text` | 4,000 characters |
| `response_payload_json` | 524,288 bytes (512 KiB) |
| Table rows per history page | 100 (capped at `MAX_PAGE_SIZE`) |
| Stored table rows in payload | 100 (capped at `HISTORY_MAX_TABLE_ROWS`) |
| Suggested questions in payload | 10 |

## Grants (SP UUID: 488a0acb-5804-42f0-98b1-a02cc13c4573)

```sql
GRANT USAGE ON SCHEMA transparence_state TO "488a0acb-5804-42f0-98b1-a02cc13c4573";
GRANT SELECT, INSERT, UPDATE, DELETE
  ON TABLE transparence_state.app_conversation_message
  TO "488a0acb-5804-42f0-98b1-a02cc13c4573";
REVOKE ALL ON TABLE transparence_state.app_conversation_message FROM PUBLIC;
```

## Migration Idempotency

`CREATE TABLE IF NOT EXISTS` ensures re-running the migration does not fail.
Index and constraint names are deterministic (no UUID suffixes).
