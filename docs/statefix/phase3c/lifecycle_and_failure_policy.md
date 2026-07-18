# Phase 3C — Lifecycle and Failure Policy

## Bundle Lifecycle Ownership

```
DurableGenieSessionRuntimeBundle
  owns → DurableGenieSessionAdapter
             owns → ConversationRepositoryBundle
                        owns → connection_provider.close()
```

- `DurableGenieSessionRuntimeBundle.close()` calls `adapter.close()` **exactly
  once** (idempotent, thread-safe via `threading.Lock`).
- `DurableGenieSessionAdapter.close()` calls `repository_bundle.close()`
  (already idempotent).
- The runtime bundle must **not** call `repository_bundle.close()` directly,
  to avoid a double-close.

## Disabled Bundle

- `adapter` is `None`.
- `close()` is a safe no-op.
- Context manager also safe.

## Enabled Bundle

- `adapter` is the `DurableGenieSessionAdapter` instance.
- `close()` closes the adapter once.
- Repeated `close()` calls are safe (idempotent).
- Context manager support: `__exit__` calls `close()`.

## Failure During create()

### Repository-factory construction fails

- Raises `DurableGenieSessionRuntimeInitializationError` with a sanitized
  message.
- The raw exception is chained via `from exc` but not surfaced in the
  human-readable message.
- No repository bundle was created, so nothing to close.
- The disabled bundle is **not** returned silently.

### repository_factory.create() fails

- Raises `DurableGenieSessionRuntimeInitializationError`.
- No repository bundle was created, so nothing to close.
- The disabled bundle is **not** returned silently.

### DurableGenieSessionAdapter construction fails (after bundle created)

- `_safe_close(repo_bundle)` is called **exactly once** before re-raising.
- Raises `DurableGenieSessionRuntimeInitializationError`.
- The disabled bundle is **not** returned silently.
- There is no silent fallback to the memory backend.

## No-Silent-Fallback Policy

On the enabled path, if any construction step fails, the error is always raised.
A disabled bundle (or any non-failure result) is never returned after explicit
enablement was requested and failed.  No step silently downgrades to the memory
backend when an error occurs during the Lakebase path.

## Repr Safety

Repr of `DurableGenieSessionRuntimeBundle` exposes only:
- `enabled` (bool)
- `backend` (enum `.value` string, e.g., `"memory"` or `"lakebase"`)
- `durable` (bool)
- `closed` (bool)

Host, endpoint, owner identifiers, Genie identifiers, tokens, and credentials
are never present in repr or in error messages.
