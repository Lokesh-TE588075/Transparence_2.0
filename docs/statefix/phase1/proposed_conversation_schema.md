# Proposed Conversation Schema — Phase 1 Design

**Date:** 2026-07-18  
**Branch:** feature/genie-state-persistence  
**Baseline commit:** 7d9977ff5965b8ee015bda1cc1662a317ae8a0d0  
**Status:** DESIGN ONLY — do not create until Phase 2

---

## 1. First Table: `app_conversation`

This is the only table to be created in Phase 2. It maps the frontend conversation UUID (and its server-side namespace) to the Genie conversation ID, enabling state to survive container restarts.

### DDL

```sql
CREATE TABLE app_conversation (
    -- Primary key
    conversation_id         UUID            NOT NULL DEFAULT gen_random_uuid(),

    -- Ownership: HMAC-SHA256(configured server secret, X-Forwarded-User), hex, lowercase.
    -- Never store the raw X-Forwarded-User value. HMAC provides stable, privacy-safe identity
    -- that is cryptographically bound to a server secret. Fall back to hashing X-Forwarded-Email
    -- only when X-Forwarded-User is absent. Do not hardcode the HMAC secret.
    owner_user_id_hash      TEXT            NOT NULL,

    -- The UUID sent by the React frontend (crypto.randomUUID()).
    -- This is NOT unique globally; it is only unique within an owner's session.
    frontend_conversation_id TEXT           NOT NULL,

    -- Genie-side conversation ID from POST /genie/spaces/{id}/start-conversation
    -- NULL until the first Genie turn has been completed.
    genie_conversation_id   TEXT            NULL,

    -- Last Genie message ID from the most recent completed turn.
    -- Used for multi-turn follow-up routing.
    last_genie_message_id   TEXT            NULL,

    -- Lifecycle status: active | expired | reset
    status                  TEXT            NOT NULL DEFAULT 'active',

    -- Optimistic concurrency: incremented on every UPDATE.
    -- Phase 2 implementation must check: WHERE version = $known_version
    version                 INTEGER         NOT NULL DEFAULT 1,

    -- Timestamps (all UTC)
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT now(),
    last_active_at          TIMESTAMPTZ     NOT NULL DEFAULT now(),

    -- Constraints
    PRIMARY KEY (conversation_id),
    CONSTRAINT uq_owner_frontend UNIQUE (owner_user_id_hash, frontend_conversation_id)
);
```

### Field Notes

| Field | Type | Nullable | Notes |
|---|---|---|---|
| `conversation_id` | UUID | No | Server-generated PK. Not the same as `frontend_conversation_id`. |
| `owner_user_id_hash` | TEXT | No | HMAC-SHA256(configured server secret, X-Forwarded-User), hex. Identifies the Databricks user without storing PII. Falls back to HMAC-SHA256(secret, X-Forwarded-Email) when X-Forwarded-User is absent. |
| `frontend_conversation_id` | TEXT | No | UUID from `crypto.randomUUID()` in React. Matches `body.conversation_id` in POST /api/chat. |
| `genie_conversation_id` | TEXT | Yes | NULL on new rows; set after first Genie `start-conversation` call. |
| `last_genie_message_id` | TEXT | Yes | NULL until first completed turn. |
| `status` | TEXT | No | `active` = current; `expired` = TTL elapsed; `reset` = manually cleared. |
| `version` | INTEGER | No | Starts at 1; incremented on every UPDATE. Used for optimistic concurrency. |
| `created_at` | TIMESTAMPTZ | No | Row creation time. |
| `updated_at` | TIMESTAMPTZ | No | Last row modification time. |
| `last_active_at` | TIMESTAMPTZ | No | Last time this conversation was referenced in a chat request. |

### Indexes

```sql
-- Index 1: Uniqueness enforcement (also covers owner+frontend lookup)
CREATE UNIQUE INDEX uq_owner_frontend
    ON app_conversation (owner_user_id_hash, frontend_conversation_id);

-- Index 2: Owner + recency (for listing recent conversations per user)
CREATE INDEX idx_owner_updated
    ON app_conversation (owner_user_id_hash, updated_at DESC);

-- Index 3: Genie conversation ID lookup (for debugging and correlation)
CREATE INDEX idx_genie_conv
    ON app_conversation (genie_conversation_id)
    WHERE genie_conversation_id IS NOT NULL;

-- Index 4: Cleanup / TTL maintenance
CREATE INDEX idx_status_last_active
    ON app_conversation (status, last_active_at);
```

### Ownership Model

- The Postgres schema is created and owned by the **app service principal**.
- All INSERT/UPDATE/SELECT operations are performed by the app SP using SDK-minted OAuth credentials.
- The interactive user (`lokesh.choraria@te.com`) accesses the schema through the Unity Catalog federation catalog (read-only, for debugging).
- End users of the chatbot never interact with the database directly.

### Optimistic Concurrency

The `version` field implements optimistic locking for multi-turn updates:

```sql
-- Safe update pattern (Python pseudocode):
result = conn.execute(
    "UPDATE app_conversation "
    "SET genie_conversation_id = $1, last_genie_message_id = $2, "
    "    updated_at = now(), last_active_at = now(), version = version + 1 "
    "WHERE conversation_id = $3 AND version = $4",
    [new_genie_id, new_message_id, conv_id, known_version]
)
if result.rowcount == 0:
    # Version mismatch — retry with fresh read
    raise ConcurrentModificationError(...)
```

If `rowcount == 0`, the row was modified by another request between the read and the write. The adapter must re-read and retry.

### Retention Assumptions

- Sessions are retained for 24 hours of inactivity (`last_active_at` + 24h).
- After 24h, `status` is set to `expired` (soft delete).
- Hard deletion of expired rows can be deferred to a nightly maintenance job or triggered lazily on access.
- No user data (messages, queries) is stored in this table — only the mapping. PII risk is limited to `owner_user_id_hash` (a hash, not the email).

### Migration Strategy

- Phase 2 creates this table in the new Lakebase schema on first startup (CREATE TABLE IF NOT EXISTS).
- The table is app-owned (SP creates it); no manual DDL execution by the operator is required.
- No migration from existing Delta state is needed — existing in-memory `GenieSessionStore` sessions are short-lived and can be discarded.
- If the Lakebase connection is unavailable at startup, the app must not silently fall back to creating new Genie conversations as a substitute for persistence. See `identity_and_authorization.md` Section 7 and `manual_infrastructure_action.md` for the required failure policy.

---

## 2. Deferred Table: `conversation_turn`

**Status: DESIGN ONLY — defer to Phase 3**

This table stores per-turn metadata for audit and debugging. It is not required for the core restart-survival fix.

```sql
CREATE TABLE conversation_turn (
    turn_id                 UUID            NOT NULL DEFAULT gen_random_uuid(),
    conversation_id         UUID            NOT NULL REFERENCES app_conversation(conversation_id),
    turn_number             INTEGER         NOT NULL,
    user_prompt_excerpt     TEXT            NULL,  -- first 200 chars only, no PII
    intent                  TEXT            NULL,
    genie_message_id        TEXT            NULL,
    genie_status            TEXT            NULL,
    export_id               TEXT            NULL,
    export_status           TEXT            NULL,
    row_count               INTEGER         NULL,
    execution_time_ms       INTEGER         NULL,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT now(),

    PRIMARY KEY (turn_id)
);

CREATE INDEX idx_turn_conversation ON conversation_turn (conversation_id, turn_number);
```

**Rationale for deferral:** The `app_conversation` table alone solves the idle-restart bug. Adding `conversation_turn` in Phase 2 increases complexity without resolving the P0 issue. It can be added once the core persistence is proven in production.

---

## 3. Excluded from Phase 2

- **Export state persistence** — ExportJobManager and CSV file persistence are deferred. The primary fix is Genie session survival.
- **Feedback persistence changes** — `chatbot_feedback` Delta table already persists across restarts.
- **Audit log changes** — `query_audit_log` Delta table already persists across restarts.
