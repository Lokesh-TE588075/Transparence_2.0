# Phase 3C — Disabled-by-Default Flag Policy

## Feature Flag

| Variable                              | app.yaml default | Meaning                              |
|---------------------------------------|------------------|--------------------------------------|
| `ENABLE_DURABLE_GENIE_SESSION_ADAPTER`| `"false"`        | Master switch for Phase 3C/3D        |
| `CONVERSATION_REPOSITORY_BACKEND`     | `"memory"`       | Backend selection for repo factory   |
| `ENABLE_LAKEBASE_CONVERSATION_REPOSITORY` | `"false"`    | Gate for Lakebase backend (Phase 3A) |

All three flags are present in `app.yaml` and default to safe, in-memory values.

## Disabled Path Guarantees

When `ENABLE_DURABLE_GENIE_SESSION_ADAPTER` is `false` (or any falsey value):

1. `DurableGenieSessionRuntimeSettings.from_environment()` parses only that one
   variable and returns `enabled=False`.
2. `DurableGenieSessionRuntimeFactory.create()` returns a disabled bundle
   immediately.
3. `ConversationRepositoryFactory` is **never instantiated**.
4. `CONVERSATION_REPOSITORY_BACKEND` and
   `ENABLE_LAKEBASE_CONVERSATION_REPOSITORY` are **never read**.
5. No Lakebase settings are loaded.
6. No `WorkspaceClient` is created.
7. No connection pool is opened.
8. No credential is generated.
9. No SQL is executed.
10. `DurableGenieSessionAdapter` is **never constructed**.

## Supported Boolean Values

| True  | False |
|-------|-------|
| true  | false |
| 1     | 0     |
| yes   | no    |
| on    | off   |
|       | (empty) |

Parsing is case-insensitive and strips surrounding whitespace.  Unknown values
raise `DurableGenieSessionRuntimeConfigurationError` with a sanitized message
that does not echo the raw value.

## Phase 3D Gate

Do not change any of these three flags from their defaults until Phase 3D
integration into `genie_backend_factory.py` has been completed and verified.
Enabling `ENABLE_DURABLE_GENIE_SESSION_ADAPTER=true` without Phase 3D wiring
would have no effect on the active request path because the factory is not yet
called by any runtime module.
