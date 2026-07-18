# Lakebase Repository Adapter

Phase 2B1 — TransparencE Genie State Persistence

---

## Overview

`LakebaseConversationRepository` is a PostgreSQL-backed implementation of the
`ConversationRepository` Protocol defined in Phase 2A. It stores conversation
state in a Lakebase Postgres `app_conversation` table using parameterized SQL
and an injected connection provider.

## Architecture

```
Caller (chat route / pipeline)
    │
    ▼
LakebaseConversationRepository
    │  (uses injected connection_provider)
    ▼
ConnectionContextProvider() → ContextManager[ConnectionLike]
    │
    ▼
ConnectionLike → CursorLike → PostgreSQL (Lakebase)
```

## Connection Abstraction

The repository never opens or manages connections directly. Instead:

1. **CursorLike** — structural Protocol: `execute()`, `fetchone()`, `fetchall()`,
   `rowcount`, context-manager support.
2. **ConnectionLike** — structural Protocol: `cursor()`, `commit()`, `rollback()`.
3. **ConnectionContextProvider** — `Callable[[], ContextManager[ConnectionLike]]`.

The repository constructor accepts a `connection_provider`:

```python
LakebaseConversationRepository(connection_provider=my_provider)
```

No connection is opened during construction. Each operation independently
requests a connection through the provider.

## Safety Constraints

- No psycopg or database driver imported at module level.
- No environment variables read.
- No connection pools created.
- No DDL executed.
- No network access.
- No threads spawned.
- All SQL is parameterized (%s placeholders).
- No user values interpolated into SQL strings.
- Exception messages never expose SQL, credentials, or PII.

## Implemented Methods (10)

| Method | Operation Type |
|--------|---------------|
| `get_by_id` | Read |
| `get_by_frontend_id` | Read |
| `create_conversation` | Create (idempotent) |
| `bind_genie_conversation` | Mutation (versioned) |
| `update_last_genie_message` | Mutation (versioned) |
| `touch` | Mutation (versioned) |
| `set_status` | Mutation (versioned) |
| `compare_and_update` | Mutation (versioned, multi-field) |
| `list_for_owner` | Read (filtered, ordered) |
| `delete_conversation` | Delete |

## Phase 2B1 Limitations

- No real database connection in this phase.
- Table `app_conversation` is not created (deferred to Phase 2B2).
- All tests use in-memory fakes.
- The adapter is not wired into the application.
