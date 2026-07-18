# Phase 2C Connection Provider Design

Phase: Implement the Lakebase OAuth connection provider for TransparencE conversation-state persistence.
Date: 2026-07-18
Status: COMPLETE

## Goal

Provide a callable connection provider compatible with `LakebaseConversationRepository` while preserving the Phase 2B2B2 constraints:

* Authenticate to Lakebase as the Databricks App service principal.
* Mint a fresh OAuth database credential for each new physical PostgreSQL connection.
* Force `search_path=transparence_state,public` so the repository can continue using bare table name `app_conversation`.
* Perform no network I/O during module import, settings creation, or provider construction.
* Avoid logging or exposing passwords, OAuth tokens, endpoint resource names, DSNs, hostnames, or service-principal identifiers.

## Implemented design

File: `app/services/lakebase_connection_provider.py`

The provider is composed of two main parts:

1. `LakebaseConnectionSettings`
   * Immutable dataclass.
   * Reads environment only in `from_environment()`.
   * Validates `PGHOST`, `PGDATABASE`, `PGPORT`, `PGUSER`, `PGSSLMODE`, and `LAKEBASE_ENDPOINT_NAME`.
   * Rejects insecure SSL modes and malformed endpoint resource names.
   * Hard-locks `search_path` to `transparence_state,public`.

2. `LakebaseConnectionProvider`
   * Callable context-provider compatible with the repository contract.
   * Lazily creates a `psycopg_pool.ConnectionPool` on first real use.
   * Uses a private OAuth-aware connection subclass built by `_build_oauth_connection_class(...)`.
   * Requests credentials through `WorkspaceClient().postgres.generate_database_credential(endpoint=...)` only when a physical connection is opened.
   * Injects only `credential.token` as PostgreSQL `password` for the duration of the single connect call.
   * Reuses pooled connections but never globally caches OAuth credentials.

## Pool behavior

Static pool/runtime choices implemented in Phase 2C:

* `max_size=5`
* acquisition timeout = 10 seconds
* connect timeout = 10 seconds
* `min_size=0`
* `open=False` with explicit lazy `open(wait=False)`
* `max_lifetime=1800` seconds
* `max_idle=300` seconds

These settings keep first-use behavior lazy, bound resource usage, and recycle physical connections well inside the 1-hour token lifetime window.

## Security posture

The implementation intentionally keeps sensitive material out of public surfaces:

* No stored password in settings or provider state.
* No token cached on the provider or connection subclass.
* Sanitized public exceptions for credential and pool failures.
* Restricted `repr()` output for settings and provider.
* Runtime configuration passed via structured kwargs instead of embedding secrets in a DSN string.

## Compatibility outcome

The provider returns a context manager yielding a psycopg-style connection object with the methods expected by `LakebaseConversationRepository` (`cursor`, `commit`, `rollback`). No repository code changes were required in Phase 2C.
